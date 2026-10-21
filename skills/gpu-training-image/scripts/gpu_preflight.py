#!/usr/bin/env python3
"""GPU architecture coverage and fail-fast job-start preflight. No GPU table is maintained here.

Offline (any machine, no torch needed): does an image's recorded architecture list cover a GPU?
  python3 gpu_preflight.py coverage --arch-list "sm_80 sm_86 sm_90 compute_90" --capability 8.9

On the allocated GPU node, inside the image, before data download or training:
  python3 gpu_preflight.py probe --expected-gpus 2 --expected-platform linux/amd64 \
      [--require-native] [--smoke module:function ...]

Coverage rules (NVIDIA CUDA compatibility guides): SASS sm_XY runs on the same major
architecture with minor >= Y; arch-specific sm_XYa runs only on exactly X.Y; family sm_XYf is
treated like plain SASS within the major (verify against the pinned CUDA docs); PTX compute_XY
JIT-compiles on any capability >= X.Y (slower first start). With PyTorch installed the probe
compares each device's capability with torch.cuda.get_arch_list(); for any other framework
(--framework none) it reads devices from nvidia-smi, checks coverage against the image lock's
--arch-list, and runs the project's own --smoke checks (e.g. a JAX all-reduce). New GPUs need no edit.
The image platform must match the host CPU (Grace-based GH200/GB200/GB300 hosts are
linux/arm64; most others linux/amd64); a mismatched image normally fails before Python starts,
so check the image manifest against the instance type before launch, and this probe records it.
Exit codes: 0 pass (warnings possible), 1 failed check, 2 usage error.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time

PLATFORM_MACHINES = {'linux/amd64': {'x86_64', 'amd64'}, 'linux/arm64': {'aarch64', 'arm64'}}
_TOKEN = re.compile(r'^(?:(sm|compute)_(\d+?)(\d)([af]?)|(\d+)\.(\d)([af]?)(\+PTX)?)$', re.I)


def parse_arch(token: str) -> list[tuple[int, int, str, str]]:
    """'sm_89' | 'sm_90a' | 'compute_90' | '8.9' | '8.9+PTX' -> [(major, minor, 'sass'|'ptx', suffix)]."""
    m = _TOKEN.match(token.strip())
    if not m:
        raise ValueError(f'unrecognized architecture token: {token!r}')
    if m.group(1):
        kind = 'sass' if m.group(1).lower() == 'sm' else 'ptx'
        return [(int(m.group(2)), int(m.group(3)), kind, m.group(4).lower())]
    major, minor, suffix = int(m.group(5)), int(m.group(6)), m.group(7).lower()
    out = [(major, minor, 'sass', suffix)]
    if m.group(8):
        out.append((major, minor, 'ptx', suffix))
    return out


def parse_arch_list(text_or_list) -> list[tuple[int, int, str, str]]:
    tokens = text_or_list if isinstance(text_or_list, list) else re.split(r'[\s;,]+', text_or_list.strip())
    entries = [e for t in tokens if t for e in parse_arch(t)]
    if not entries:
        raise ValueError('empty architecture list')
    return entries


def coverage(arch_list, capability) -> dict:
    """Return {'status': 'native'|'ptx-jit'|'unsupported', 'via': token or None}."""
    major, minor = capability
    native = ptx = None
    for a_major, a_minor, kind, suffix in parse_arch_list(arch_list):
        if suffix == 'a':
            ok = (a_major, a_minor) == (major, minor)
        elif kind == 'sass':
            ok = a_major == major and minor >= a_minor
        else:
            ok = (major, minor) >= (a_major, a_minor)
        label = f"{'sm' if kind == 'sass' else 'compute'}_{a_major}{a_minor}{suffix}"
        if ok and kind == 'sass':
            native = native or label
        elif ok:
            ptx = ptx or label
    if native:
        return {'status': 'native', 'via': native}
    if ptx:
        return {'status': 'ptx-jit', 'via': ptx}
    return {'status': 'unsupported', 'via': None}


def evaluate(report: dict, expected_gpus: int, require_native: bool = False, expected_platform=None) -> dict:
    """Pure decision over a probe report; unit-testable without a GPU."""
    failures, warnings = [], []
    if expected_platform is not None:
        if expected_platform not in PLATFORM_MACHINES:
            raise ValueError(f'unsupported platform {expected_platform!r}; use linux/amd64 or linux/arm64')
        if str(report.get('machine', '')).lower() not in PLATFORM_MACHINES[expected_platform]:
            failures.append(f"host CPU {report.get('machine')} does not match image platform {expected_platform}")
    if report.get('cuda_error'):
        failures.append(f"CUDA initialisation failed (driver/runtime mismatch?): {report['cuda_error']}")
    devices = report.get('devices', [])
    if len(devices) != expected_gpus:
        failures.append(f'expected {expected_gpus} GPUs, found {len(devices)}')
    for d in devices:
        if report.get('arch_list') is None:
            d['coverage'] = {'status': 'not_checked', 'via': None}
            warnings.append(f"GPU {d['index']}: architecture coverage not checked; pass --arch-list from the image lock")
            cov = d['coverage']
        elif d.get('capability') is None:
            cov = d['coverage'] = {'status': 'unsupported', 'via': None}
            failures.append(f"GPU {d['index']}: compute capability unknown (nvidia-smi compute_cap unsupported?)")
            continue
        else:
            cov = d['coverage'] = coverage(report['arch_list'] or [], tuple(d['capability']))
        if cov['status'] == 'unsupported':
            failures.append(f"GPU {d['index']} {d.get('name')} sm_{d['capability'][0]}{d['capability'][1]} "
                            f"not covered by image architectures {report.get('arch_list')}")
        elif cov['status'] == 'ptx-jit':
            (failures if require_native else warnings).append(
                f"GPU {d['index']} runs via PTX JIT ({cov['via']}): slower first start, unqualified kernels")
        if d.get('ecc_uncorrected'):
            failures.append(f"GPU {d['index']} reports {d['ecc_uncorrected']} uncorrected ECC errors")
        if d.get('kernel_ok') is False:
            failures.append(f"GPU {d['index']} kernel test failed: {d.get('kernel_error')}")
    if report.get('framework') == 'none':
        if not report.get('smoke'):
            warnings.append('no kernel or collective test ran: pass --smoke with the framework\'s own GPU/collective check')
    elif expected_gpus > 1 and report.get('nccl_ok') is not True:
        failures.append(f"NCCL all-reduce failed: {report.get('nccl_error', 'not run')}")
    for name, result in report.get('smoke', {}).items():
        if result != 'ok':
            failures.append(f'extension smoke {name} failed: {result}')
    for d in report.get('disk', []):
        if d.get('error'):
            failures.append(f"free space of {d['path']} unknown: {d['error']}")
        elif d['free_gb'] < d['min_gb']:
            failures.append(f"{d['path']} has {d['free_gb']:.1f} GB free, needs {d['min_gb']} GB "
                            '(disk budget: raise disk.size_gb or shrink the cache)')
    return {'ok': not failures, 'failures': failures, 'warnings': warnings}


def disk_report(specs) -> list:
    """Free space for each PATH=GB requirement (the disk budget), checked before any download."""
    out = []
    for spec in specs:
        path, _, gb = spec.rpartition('=')
        if not path or not gb:
            raise ValueError(f'--min-free-gb takes PATH=GB, got {spec!r}')
        entry = {'path': path, 'min_gb': float(gb)}
        try:
            entry['free_gb'] = round(shutil.disk_usage(os.path.expanduser(path)).free / 1e9, 1)
        except OSError as e:
            entry['error'] = f'{type(e).__name__}: {e}'
        out.append(entry)
    return out


# ---- probe (needs torch + GPUs; runs inside the image) -------------------------------------

def _nccl_worker(rank, world, port, out):
    import torch
    import torch.distributed as dist
    dist.init_process_group('nccl', init_method=f'tcp://127.0.0.1:{port}', rank=rank, world_size=world)
    torch.cuda.set_device(rank)
    t = torch.full((1 << 20,), float(rank + 1), device='cuda')
    dist.all_reduce(t)
    torch.cuda.synchronize()
    out[rank] = bool(torch.all(t == world * (world + 1) / 2).item())
    dist.destroy_process_group()


def parse_nvidia_smi(text: str) -> dict:
    """Parse `index,name,compute_cap,driver_version,ecc.errors.uncorrected.volatile.total` CSV rows."""
    rows = {}
    for line in text.strip().splitlines():
        idx, name, cap, driver, ecc = [x.strip() for x in line.split(',')]
        capability = [int(x) for x in cap.split('.')] if re.fullmatch(r'\d+\.\d+', cap) else None
        rows[int(idx)] = {'index': int(idx), 'name': name, 'capability': capability, 'driver': driver,
                          'ecc_uncorrected': int(ecc) if ecc.isdigit() else 0}
    return rows


def _nvidia_smi():
    fields = 'index,name,compute_cap,driver_version,ecc.errors.uncorrected.volatile.total'
    try:
        res = subprocess.run(['nvidia-smi', f'--query-gpu={fields}', '--format=csv,noheader,nounits'],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {'error': str(e)}
    if res.returncode != 0:
        return {'error': res.stderr.strip()[:300] or f'nvidia-smi exit {res.returncode}'}
    return parse_nvidia_smi(res.stdout)


def run_smoke(specs) -> dict:
    import importlib
    out = {}
    for spec in specs:
        module, _, func = spec.partition(':')
        try:
            mod = importlib.import_module(module)
            if func:
                getattr(mod, func)()
            out[spec] = 'ok'
        except Exception as e:  # noqa: BLE001
            out[spec] = f'{type(e).__name__}: {e}'
    return out


def probe_without_framework(expected_gpus: int, smoke=(), arch_list=None) -> dict:
    """Framework-neutral probe: nvidia-smi facts, image-lock arch list, project smoke checks."""
    t0 = time.time()
    report = {'framework': 'none', 'devices': [], 'machine': platform.machine(), 'arch_list': None}
    if arch_list:
        parse_arch_list(arch_list)                    # validate tokens; raises ValueError
        report['arch_list'] = [x for x in re.split(r'[\s;,]+', arch_list.strip()) if x]
    smi = _nvidia_smi()
    if 'error' in smi:
        report['cuda_error'] = f"nvidia-smi failed: {smi['error']}"
        return report
    report['devices'] = [smi[i] for i in sorted(smi)]
    report['smoke'] = run_smoke(smoke)
    report['seconds'] = round(time.time() - t0, 1)
    return report


def probe(expected_gpus: int, nccl: bool = True, smoke=(), framework='auto', arch_list=None) -> dict:
    import socket
    if framework == 'auto':
        try:
            import torch  # noqa: F401
            framework = 'torch'
        except ImportError:
            framework = 'none'
    if framework == 'none':
        return probe_without_framework(expected_gpus, smoke, arch_list)
    t0 = time.time()
    report = {'framework': 'torch', 'devices': [], 'smoke': {}, 'machine': platform.machine()}
    try:
        import torch
        report.update(torch=torch.__version__, cuda=torch.version.cuda, arch_list=torch.cuda.get_arch_list())
        n = torch.cuda.device_count()
        torch.cuda.init()
    except Exception as e:  # noqa: BLE001 - any init failure is a preflight failure
        report['cuda_error'] = f'{type(e).__name__}: {e}'
        return report
    smi = _nvidia_smi()
    for i in range(n):
        d = {'index': i, 'name': torch.cuda.get_device_name(i), 'capability': list(torch.cuda.get_device_capability(i)),
             **({k: v for k, v in smi.get(i, {}).items() if k in ('driver', 'ecc_uncorrected')}
                if isinstance(smi, dict) and 'error' not in smi else {})}
        try:
            a = torch.randn(1024, 1024, device=f'cuda:{i}', dtype=torch.float16)
            # Sum in float32: the fp16 product is finite, but its fp16 sum (spread ~3e4) overflows 65504
            # in about 1 of 20 healthy draws and failed real GPUs.
            d['kernel_ok'] = bool(torch.isfinite((a @ a).float().sum()).item())
        except Exception as e:  # noqa: BLE001
            d.update(kernel_ok=False, kernel_error=f'{type(e).__name__}: {e}')
        report['devices'].append(d)
    if nccl and n > 1:
        import torch.multiprocessing as mp
        try:
            with socket.socket() as s:
                s.bind(('127.0.0.1', 0))
                port = s.getsockname()[1]
            out = mp.Manager().dict()
            mp.spawn(_nccl_worker, args=(n, port, out), nprocs=n, join=True)
            report['nccl_ok'] = all(out.get(r) for r in range(n))
        except Exception as e:  # noqa: BLE001
            report.update(nccl_ok=False, nccl_error=f'{type(e).__name__}: {e}')
    report['smoke'] = run_smoke(smoke)
    report['seconds'] = round(time.time() - t0, 1)
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    c = sub.add_parser('coverage')
    c.add_argument('--arch-list', required=True)
    c.add_argument('--capability', required=True, help='e.g. 8.9')
    p = sub.add_parser('probe')
    p.add_argument('--expected-gpus', type=int, required=True)
    p.add_argument('--expected-platform', choices=sorted(PLATFORM_MACHINES), help='image platform from image lock')
    p.add_argument('--require-native', action='store_true', help='treat PTX-JIT coverage as failure')
    p.add_argument('--no-nccl', action='store_true')
    p.add_argument('--framework', choices=['auto', 'torch', 'none'], default='auto',
                   help='torch: use PyTorch checks; none: nvidia-smi + --arch-list + --smoke (any framework)')
    p.add_argument('--arch-list', help='compiled architectures from the image lock (used with --framework none)')
    p.add_argument('--smoke', nargs='*', default=[], help='module or module:function smoke checks for extensions')
    p.add_argument('--min-free-gb', action='append', default=[], metavar='PATH=GB',
                   help='disk budget: PATH must have at least GB free (repeatable, e.g. /=60 or ~/.cache=40)')
    args = ap.parse_args(argv)
    try:
        if args.cmd == 'coverage':
            major, minor = (int(x) for x in args.capability.split('.'))
            result = coverage(args.arch_list, (major, minor))
            print(json.dumps(result))
            return 0 if result['status'] != 'unsupported' else 1
        report = probe(args.expected_gpus, nccl=not args.no_nccl, smoke=args.smoke,
                       framework=args.framework, arch_list=args.arch_list)
        report['disk'] = disk_report(args.min_free_gb)
        decision = evaluate(report, args.expected_gpus, args.require_native, args.expected_platform)
        print(json.dumps({**decision, 'report': report}, default=str))
        return 0 if decision['ok'] else 1
    except ValueError as e:
        print(json.dumps({'ok': False, 'error': str(e)}))
        return 2


if __name__ == '__main__':
    sys.exit(main())
