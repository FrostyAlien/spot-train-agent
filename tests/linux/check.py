"""Run one Linux watchdog scenario under progress_watch.py and check what it should show (tests/linux/run.sh).

Every limit is 30 s (save 20 s in t6) and the watcher polls every 5 s, so each scenario takes 1-3 minutes.
Prints `PASS name: ...` or `FAIL name: ...` with the measured numbers, then the watcher's output.
"""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, '/opt/recover')
import checkpoint_marker as cm     # noqa: E402

W = '/opt/gpu-skills/progress_watch.py'
BASE = {'WATCH_POLL_SECONDS': '5', 'STALL_DUMP_WAIT_SECONDS': '2', 'STALL_TERM_WAIT_SECONDS': '5',
        'PROGRESS_PERIOD_SECONDS': '30', 'STALL_LIMITS': 'train=30,eval=30,save=30,final=30,startup=60',
        'HEARTBEAT_DIR': '/tmp/hb'}
TORCHRUN = ['torchrun', '--standalone', '--nproc_per_node=2', '/opt/gpu-skills/torch_rank.py', '--step-hook', '--', '/w/t7_train.py']


def run(argv, **env):
    os.sync()                                          # earlier scenarios' writeback must not count here
    p = subprocess.run(['python3', W, 'watch', '--', *argv], capture_output=True, text=True, timeout=900,
                       env={**os.environ, **BASE, **env})
    out = p.stdout + p.stderr
    summary = next((json.loads(ln.split(' ', 1)[1]) for ln in out.splitlines() if ln.startswith('WATCH-SUMMARY ')), {})
    m = re.search(r'^STALL (\S+) .*$', out, re.M)
    stall = m.group(0) if m else None
    at = datetime.strptime(m.group(1), '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).timestamp() if m else None
    return p.returncode, out, summary, stall, at


def mark(out, name):
    m = re.search(rf'{name} (\d+)', out)
    return float(m.group(1)) if m else None


def main():
    name = sys.argv[1]
    checks, notes = [], {}
    if name == 't1':      # rank 0 saves for 60 s, rank 1 waits silently: alive through the bytes
        code, out, s, stall, _ = run(['python3', '/w/jobs.py', 't1'])
        notes = {'io_mb': s.get('io_mb'), 'quiet_seconds': s.get('quiet_seconds'), 'phase_seconds': s.get('phase_seconds'),
                 'watcher_cpu_seconds': s.get('watcher_cpu_seconds'), 'wall_seconds': s.get('wall_seconds'),
                 'limits_without_eval': cm.watch_limits(s)}
        checks = [code == 0, stall is None, s.get('io_mb', {}).get('save', 0) >= 250,
                  s.get('quiet_seconds', {}).get('save', 99) < 15, s.get('phase_seconds', {}).get('save', 0) > 30,
                  'eval' not in s.get('phase_seconds', {})]
    elif name == 't2':    # a save that stops writing: a stall within the limit plus the writeback tail
        code, out, s, stall, at = run(['python3', '/w/jobs.py', 't2'])
        delay = at - mark(out, 'T2-LAST-WRITE') if at else None
        notes = {'stall': stall, 'seconds_after_last_write': delay}
        checks = [code == 93, stall and 'phase=save' in stall, delay is not None and 30 <= delay <= 80]
    elif name == 't3':    # a training hang while a thread trickles 20 KB/s: the 1 MiB threshold holds
        code, out, s, stall, at = run(['python3', '/w/jobs.py', 't3'])
        delay = at - mark(out, 'T3-HANG') if at else None
        notes = {'stall': stall, 'seconds_after_hang': delay}
        checks = [code == 93, stall and 'phase=train' in stall, delay is not None and 25 <= delay <= 50]   # heartbeats lag up to 5 s
    elif name == 't4':    # an async writer keeps a hung job alive until it finishes, then the stall
        code, out, s, stall, at = run(['python3', '/w/jobs.py', 't4'])
        done = mark(out, 'T4-WRITER-DONE')
        delay = at - done if at and done else None
        notes = {'stall': stall, 'seconds_after_writer_done': delay}
        checks = [code == 93, delay is not None and 25 <= delay <= 80]
    elif name == 't5':    # 90 s of eval ticks every second: alive; when they stop, a stall at the eval limit
        code, out, s, stall, at = run(['python3', '/w/jobs.py', 't5'])
        delay = at - mark(out, 'T5-LAST-EVAL') if at else None
        notes = {'stall': stall, 'seconds_after_last_eval_tick': delay, 'largest_eval_gap': s.get('largest_eval_gap_seconds')}
        checks = [code == 93, stall and 'phase=eval' in stall, delay is not None and 25 <= delay <= 45]
    elif name == 't6':    # fsync on a throttled disk (10 MB/s) blocks the writer longer than the save limit: a flush keeps it alive
        code, out, s, stall, _ = run(['python3', '/w/jobs.py', 't6'], STALL_LIMITS='train=30,eval=30,save=20,final=30,startup=60')
        seconds = mark(out, 'T6-SAVE-SECONDS')
        notes = {'save_seconds': seconds, 'quiet_seconds': s.get('quiet_seconds'), 'io_mb': s.get('io_mb')}
        checks = [code == 0, stall is None, seconds is not None and seconds > 25]
    elif name == 't7':    # rank mode under torchrun, no hooks in the script; an injected stall
        code, out, s, stall, at = run(TORCHRUN + ['stall'])
        after = out.split(stall, 1)[1] if stall else ''
        dumps = len(re.findall(r'File "/w/t7_train.py", line \d+ in main', after))
        notes = {'stall': stall, 'stack_frames_in_t7_main': dumps, 'train_ticks': s.get('train_ticks')}
        checks = [code == 93, stall and 'phase=train' in stall, dumps >= 2, s.get('train_ticks', 0) >= 1]
    elif name == 't7b':   # rank mode, an evaluation no hook announces: a long train gap, widened by the helper
        code, out, s, stall, _ = run(TORCHRUN + ['hidden-eval'])
        limits = cm.watch_limits(s, eval_scale=5, notes=(n := []))
        notes = {'quiet_seconds': s.get('quiet_seconds'), 'largest_train_gap': s.get('largest_train_gap_seconds'),
                 'recommended': limits, 'notes': n}
        checks = [code == 0, stall is None, 'eval' not in s.get('phase_seconds', {}),
                  s.get('quiet_seconds', {}).get('train', 0) >= 15, limits['train'] >= 225,
                  any('inside train gaps' in x for x in n)]
    elif name == 't8':    # Lightning (callbacks via its entry point, its own launcher), rank-0 saves, final ticks
        code, out, s, stall, _ = run(['python3', '/w/lit.py'])
        notes = {'phase_seconds': s.get('phase_seconds'), 'io_mb': s.get('io_mb'), 'train_ticks': s.get('train_ticks'),
                 'quiet_seconds': s.get('quiet_seconds')}
        checks = [code == 0, stall is None, {'eval', 'save', 'final'} <= set(s.get('phase_seconds', {})),
                  s.get('train_ticks', 0) >= 50, sum(s.get('io_mb', {}).values()) >= 600]   # three 256 MB saves
    ok = bool(checks) and all(checks)
    print(f"{'PASS' if ok else 'FAIL'} {name}: {json.dumps(notes, default=str)} checks={checks} exit={code}", flush=True)
    print('----- watcher output -----\n' + out[-6000:], flush=True)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
