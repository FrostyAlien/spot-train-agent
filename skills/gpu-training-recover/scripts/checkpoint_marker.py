#!/usr/bin/env python3
"""Commit and select checkpoints on a SkyPilot bucket mount (MOUNT_CACHED or MOUNT); stdlib only.

Layout under the mounted checkpoint directory (one immutable directory per checkpoint and attempt):

  <run_dir>/<step, 12 digits>-<attempt>/payload/...    framework-native checkpoint files
  <run_dir>/<step, 12 digits>-<attempt>/manifest.json  verifier manifest (sizes, SHA-256, roles, identities)
  <run_dir>/<step, 12 digits>-<attempt>/COMMITTED.json written last, only after everything above verifies

<attempt> is set once per worker start and shared by all ranks (for example a UTC timestamp such as
20261002T120000Z, exported by the entrypoint). A replacement that re-trains to a step the previous
worker saved but never committed therefore writes a new directory instead of reusing a partial one.

The marker is written after the payload and manifest are closed. The rendered checkpoint mount
(MOUNT_CACHED with SkyPilot's MODEL_CHECKPOINT_RW preset) uploads in parallel and rclone re-queues a
failed upload behind later files, so upload order is not guaranteed: select() verifying every file is
what proves completeness. A marker that landed before its payload makes that checkpoint look damaged,
and select() falls back to the previous one (or blocks when it was the only one). Never rename on
the mount, write each file once, and never delete checkpoints from the training worker (uploads of the
newest ones may still be queued); prune from outside the worker or at finish.

Two thin helpers make a run provable after recovery (both write-once files on the same mount):
  <run_dir>/journal/<attempt>/<seq>-<event>.json   per-attempt events (preflight, selection, commits,
                                                   finish); `sky jobs logs` keeps only the last attempt
  <run_dir>/FINAL.json                             written once at the end: outcome, final step and
                                                   directory, manifest hash, extra artifacts (best model...)
  <run_dir>/alive/<start>-<host>.json              one per allocation, rewritten by the stall watchdog every
                                                   progress period: {start, last, period_seconds} (epoch s);
                                                   the only file here that is rewritten, read by `limits`

  commit       STEP_DIR --fields FIELDS.json --files FILES.json    write manifest, verify, then COMMITTED.json;
                                                                   prints `CHECKPOINT_COMMITTED {json}` (the line
                                                                   operators and drills wait for)
  select       RUN_DIR --mount-root /checkpoints IDENTITY           JSON: resume | fresh; exit 3 when blocked
  journal      RUN_DIR --attempt A --event E [--field k=v ...]      append one write-once event record (also
                                                                   printed as `CHECKPOINT_JOURNAL {json}`)
  journal-show RUN_DIR                                              all events of all attempts, in order
  journal-check RUN_DIR --expect fresh[,resume] [--require EVENT] [--require-final] [--submitted-at EPOCH]
               [--max-memory-fraction F] [--watch-summaries F [--eval-scale S]]   qualification result: one start per
                                                                   expected selection, each with a commit, no fresh
                                                                   start after an earlier commit (and FINAL.json
                                                                   completed); exit 1 if not; echoes the measurements
                                                                   and the watchdog's recommended quiet limits
  limits       RUN_DIR (--from-env | --submitted-at EPOCH --ceiling-hourly USD --budget USD) [--reserved USD]
               [--spent-before USD] [--free-without-vm] [--startup-allowance-minutes M]
               [--max-attempts-without-progress N] [--max-attempts N] [--deadline ISO] [--shutdown-reserve-minutes M]
                                                                   the run's limits at a worker start, from the journal
                                                                   and the alive files: JSON with the spend upper bound,
                                                                   allocations, stop_at_utc (and stop_by: budget or
                                                                   deadline) and the limit reached; exit 5 when a limit is
                                                                   reached now (stop without training). Only the deadline
                                                                   is final: a budget or attempt stop writes no FINAL.json,
                                                                   so a relaunch with a raised limit resumes the run
  finalize     RUN_DIR IDENTITY --outcome completed|deadline_stopped|stopped [--target-step N] [--artifact REL ...]
  verify-final RUN_DIR IDENTITY                                     FINAL.json still matches the bucket; exit 3 if not;
                                                                   lists lineage_warnings (lost progress)
  prune-list   RUN_DIR [--keep 3]                                   checkpoint directories safe to delete from outside
                                                                   the worker (deletes nothing)
  IDENTITY = --run-id R --data-version D --model-version M --world-size N --image-digest I --code-sha256 C
             --config-sha256 F
"""
from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import math
import os
import re
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verify_checkpoint as vc  # noqa: E402

STEP_DIGITS = 12
COMMIT_LINE = 'CHECKPOINT_COMMITTED'
JOURNAL_LINE = 'CHECKPOINT_JOURNAL'
# Verifier errors that mean "wrong experiment or software", not a damaged candidate: never fall back.
BLOCKING = ('identity mismatch', 'unsupported', 'commit run mismatch')


class Blocked(Exception):
    """Recovery cannot proceed safely; never start fresh after this."""


NAME = re.compile(r'(\d{%d})-([A-Za-z0-9]{1,64})' % STEP_DIGITS)


def step_dir(run_dir, global_step: int, attempt: str) -> Path:
    if not re.fullmatch(r'[A-Za-z0-9]{1,64}', attempt or ''):
        raise ValueError('attempt must be 1-64 letters or digits, the same for every rank of this worker start')
    return Path(run_dir) / f'{global_step:0{STEP_DIGITS}d}-{attempt}'


def _sha256(path: Path) -> tuple[int, str]:
    h, n = hashlib.sha256(), 0
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            n += len(chunk)
            h.update(chunk)
    return n, h.hexdigest()


def _write_once(path: Path, data: bytes):
    with open(path, 'xb') as f:            # create-only: an existing marker or manifest is never overwritten
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def commit(directory, fields: dict, files: list) -> dict:
    """Write manifest.json, verify the checkpoint against it, then write COMMITTED.json last.

    Call on one rank after every rank's save has finished and every payload file is closed.
    fields: manifest fields (run_id, data_version, model_version, world_size, global_step,
            optimizer_boundary, scaler_used, image_digest, code_sha256, config_sha256, derived_roles, ...).
    files:  [{'path': 'payload/...', 'roles': [...], 'rank': int (rng/data entries)}]; sizes and hashes are computed.
    """
    root = Path(directory)
    entries = []
    for f in files:
        size, digest = _sha256(root / f['path'])
        entries.append({**f, 'bytes': size, 'sha256': digest})
    manifest = {'schema_version': 1, **fields, 'files': entries}
    manifest_raw = json.dumps(manifest, sort_keys=True, indent=1).encode()
    _write_once(root / 'manifest.json', manifest_raw)
    record = {'schema_version': 1, 'run_id': fields['run_id'], 'global_step': fields['global_step'],
              'manifest': 'manifest.json', 'manifest_sha256': hashlib.sha256(manifest_raw).hexdigest(),
              'committed_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
    identity = {k: fields[k] for k in vc.IDENTITY_FIELDS if k in fields}
    report = vc.verify(root, fields['run_id'], fields['data_version'], fields['model_version'],
                       fields['world_size'], identity=identity, pending_commit=record)
    _write_once(root / 'COMMITTED.json', vc.commit_bytes(record))
    # One standard log line per commit: runbooks wait for it and the drill stops the VM right after it.
    print(COMMIT_LINE + ' ' + json.dumps({'run_id': fields['run_id'], 'global_step': fields['global_step'],
                                           'dir': root.name, 'manifest_sha256': record['manifest_sha256']},
                                          sort_keys=True), flush=True)
    return {'committed': True, 'global_step': fields['global_step'], 'manifest_sha256': record['manifest_sha256'],
            'files': report['files_verified'], 'bytes': report['bytes_verified']}


def select(run_dir, run_id, data_version, model_version, world_size, identity, require_identity=True,
           mount_root=None) -> dict:
    """Newest committed checkpoint that verifies; 'fresh' only when no checkpoint was ever committed.

    Candidates are ordered by step, then by attempt (newest attempt first when the same step was saved twice).
    Directories without COMMITTED.json are saves in progress or uploads cut off by preemption and are ignored.
    A committed candidate that is positively damaged (missing/changed/unlisted files) is skipped with a reason;
    identity or software mismatches, unreadable storage and 'committed but none valid' raise Blocked.
    """
    run = Path(run_dir)
    try:
        if mount_root is not None:
            if not os.path.ismount(mount_root):
                # An empty local directory where the bucket should be mounted must never read as 'fresh'.
                raise Blocked(f'{mount_root} is not a mounted filesystem')
            # The bucket is mounted. Object stores have no empty directories, so the run's prefix
            # (and its parents) exist only after the first write: missing means nothing was committed.
        elif not run.parent.is_dir():
            raise Blocked(f'checkpoint mount {run.parent} is not available')
        if not run.exists():
            return {'action': 'fresh', 'reason': 'no checkpoint directory for this run yet'}
        names = sorted((p.name for p in run.iterdir() if p.is_dir() and NAME.fullmatch(p.name)), reverse=True)
        committed = [n for n in names if (run / n / 'COMMITTED.json').is_file()]
    except OSError as e:
        raise Blocked(f'cannot list {run}: {e}') from e
    if not committed:
        return {'action': 'fresh', 'reason': 'no committed checkpoint',
                'uncommitted': names}
    skipped = []
    for name in committed:
        path = run / name
        try:
            report = vc.verify(path, run_id, data_version, model_version, world_size,
                               identity=identity, require_identity=require_identity)
        except vc.Invalid as e:
            if str(e).startswith(BLOCKING):
                raise Blocked(f'{path}: {e}') from e
            skipped.append({'checkpoint': name, 'reason': str(e)})
            continue
        except OSError as e:               # access or I/O problem is not evidence of damage
            raise Blocked(f'cannot read {path}: {e}') from e
        if report['global_step'] != int(NAME.fullmatch(name).group(1)):
            skipped.append({'checkpoint': name, 'reason': f"directory name does not match global_step {report['global_step']}"})
            continue
        return {'action': 'resume', 'path': str(path), 'global_step': report['global_step'],
                'manifest_sha256': report['manifest_sha256'], 'skipped': skipped}
    raise Blocked(f'checkpoints were committed but none verifies; skipped={skipped}')


EVENT = re.compile(r'[a-z][a-z0-9_-]{0,39}')
# Measurements the entrypoint journals in qualification, reported conservatively: the slowest read rate,
# the largest of the rest. save_seconds skips the run's first save (the steady state is a later one).
MEASURES = ('read_mb_s', 'save_seconds', 'checkpoint_gb', 'startup_seconds', 'step_seconds', 'eval_seconds',
            'loader_wait_fraction')
LOW_UTILISATION = 0.3          # peak GPU memory fraction below which the report says the GPUs are under-used
OUTCOMES = ('completed', 'deadline_stopped', 'stopped')   # final; a budget or attempt limit is not (the run can go on)
LIMIT_EXIT = 5                 # `limits`: a limit is reached now; the worker stops without training


def _utc() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def journal(run_dir, attempt: str, event: str, fields: dict | None = None) -> Path:
    """Write one event of this worker start as its own write-once file (rank 0 only).
    Creates the run prefix if needed: an object store has no directory before the first write."""
    if not re.fullmatch(r'[A-Za-z0-9]{1,64}', attempt or ''):
        raise ValueError('attempt must be 1-64 letters or digits')
    if not EVENT.fullmatch(event or ''):
        raise ValueError('event must be a short lowercase name, e.g. preflight, selection, commit, finish')
    d = Path(run_dir) / 'journal' / attempt
    d.mkdir(parents=True, exist_ok=True)
    seq = 1 + max((int(p.name[:4]) for p in d.glob('[0-9][0-9][0-9][0-9]-*.json')), default=0)
    record = {**(fields or {}), 'schema_version': 1, 'attempt': attempt, 'seq': seq, 'event': event,
              'time': _utc(), 'host': socket.gethostname()}
    path = d / f'{seq:04d}-{event}.json'
    _write_once(path, json.dumps(record, sort_keys=True).encode())
    print(JOURNAL_LINE + ' ' + json.dumps(record, sort_keys=True), flush=True)   # also in the job log
    return path


def read_journal(run_dir) -> list:
    """Every journal record of every attempt, ordered by attempt then sequence; unreadable files are
    reported in place (an upload cut off by preemption), never skipped silently."""
    out = []
    root = Path(run_dir) / 'journal'
    for attempt in sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []:
        for f in sorted(attempt.glob('[0-9][0-9][0-9][0-9]-*.json')):
            try:
                out.append(json.loads(f.read_text()))
            except (OSError, ValueError) as e:
                out.append({'attempt': attempt.name, 'file': f.name, 'error': f'{type(e).__name__}: {e}'})
    return out


def _epoch(text: str) -> float:
    """Unix seconds of an ISO-8601 UTC time ending in Z, with or without seconds (the contract allows both)."""
    fmt = '%Y-%m-%dT%H:%M:%SZ' if text.count(':') == 2 else '%Y-%m-%dT%H:%MZ'
    return float(calendar.timegm(time.strptime(text, fmt)))


def lineage_problems(runs: list) -> list:
    """Worker starts (each a list of events, in time order) that began fresh after an earlier start had
    committed: that progress was lost (its upload did not finish, or the checkpoint did not verify)."""
    problems, committed = [], False
    for i, run in enumerate(runs, 1):
        sel = [e.get('action') for e in run if e.get('event') == 'selection']
        if sel[:1] == ['fresh'] and committed:
            problems.append(f'start {i}: fresh after an earlier start committed (progress lost)')
        committed = committed or any(e.get('event') == 'commit' for e in run)
    return problems


def check_journal(run_dir, expect: list, require=(), submitted_at=None, max_memory_fraction=None,
                  require_final=False, watch_summaries=None, eval_scale=None) -> dict:
    """Qualification result from the journal: one worker start (attempt) per expected selection action,
    in order, each with a commit and the required events; with require_final, FINAL.json says completed.
    With submitted_at (Unix seconds of the launch command) it measures the allocation start (first
    preflight) and recommends the guard's startup_allowance_minutes. An event field peak_memory_fraction
    above max_memory_fraction fails; below LOW_UTILISATION the result notes low_utilisation. watch_summaries
    (the stall watchdog's summaries.jsonl) adds its measurements and recommended.watchdog, the main run's quiet
    limits; eval_scale (the full eval set over the subset's) scales the evaluation's silent tail."""
    events = read_journal(run_dir)
    failures, attempts = [], {}
    for e in events:
        if 'error' in e:
            failures.append(f"unreadable journal entry {e.get('file')} of attempt {e.get('attempt')}")
        else:
            attempts.setdefault(e['attempt'], []).append(e)
    # Worker starts in time order (first event), not by tag: a tag need only be unique per start.
    order = sorted(attempts, key=lambda a: (min(_epoch(e['time']) for e in attempts[a] if 'time' in e), a))
    runs = [attempts[a] for a in order]
    if len(runs) != len(expect):
        failures.append(f'expected {len(expect)} worker start(s), found {len(runs)}')
    for i, (run, action) in enumerate(zip(runs, expect), 1):
        kinds = [e['event'] for e in run]
        sel = [e.get('action') for e in run if e['event'] == 'selection']
        if sel[:1] != [action]:
            failures.append(f'start {i}: expected selection {action}, got {sel}')
        failures += [f'start {i}: no {k} journaled' for k in ('commit', *require) if k not in kinds]
    failures += lineage_problems(runs)
    peak = [float(e['peak_memory_fraction']) for e in events if 'peak_memory_fraction' in e]
    result, notes = {'ok': False, 'starts': len(runs), 'events': len(events)}, []
    for key in MEASURES:                        # measured by the entrypoint in qualification
        try:
            values = [float(e[key]) for e in events if key in e]
        except (TypeError, ValueError):
            failures.append(f'{key} is not a number')
            continue
        if key == 'save_seconds' and len(values) > 1:
            values = values[1:]
        if values:
            result[key] = min(values) if key == 'read_mb_s' else max(values)
    if peak:
        result['peak_memory_fraction'] = max(peak)
        if max_memory_fraction is not None and max(peak) > max_memory_fraction:
            failures.append(f'peak GPU memory {max(peak):.2f} of total exceeds {max_memory_fraction}: lower the '
                            'per-device batch (argv) before the paid run')
        if max(peak) < LOW_UTILISATION:
            notes.append('low_utilisation')
    if watch_summaries is not None:
        try:
            lines = [json.loads(ln) for ln in Path(watch_summaries).read_text().splitlines() if ln.strip()]
            for key in ('largest_train_gap_seconds', 'largest_eval_gap_seconds', 'eval_tail_seconds'):
                result[key] = max(float(x.get(key, 0.0)) for x in lines)
            for key in ('phase_seconds', 'quiet_seconds', 'io_mb'):
                merged = {}
                for x in lines:
                    for name, value in x.get(key, {}).items():
                        merged[name] = max(merged.get(name, 0.0), float(value))
                result[key] = merged
            result['io_signal'] = all(x.get('io_signal', False) for x in lines)
            result['train_ticks'] = max(int(x.get('train_ticks', 0)) for x in lines)
            if result['train_ticks'] == 0:
                failures.append("no train tick reached the stall watchdog: the entrypoint's hooks did not tick; fix "
                                'them, or set watchdog.enabled false')
            result['recommended'] = {'watchdog': {'limits': watch_limits(result, eval_scale, notes)}}
        except (OSError, ValueError, KeyError, TypeError) as e:
            failures.append(f'no readable watchdog summary ({type(e).__name__}): the stall watchdog did not run')
    if notes:
        result['notes'] = notes
    if require_final:
        try:
            outcome = json.loads((Path(run_dir) / 'FINAL.json').read_text()).get('outcome')
        except (OSError, ValueError) as e:
            outcome = f'unreadable ({type(e).__name__})'
        if outcome != 'completed':
            failures.append(f'FINAL.json outcome {outcome}, expected completed (start 2 runs to the end and finalizes)')
    starts = [_epoch(e['time']) for e in events if e.get('event') == 'preflight' and 'time' in e]
    if submitted_at is not None and starts:
        minutes = (min(starts) - submitted_at) / 60
        result['startup_minutes'] = round(minutes, 1)
        result['recommended_startup_allowance_minutes'] = math.ceil(1.5 * max(minutes, 0)) + 2
    result.update(ok=not failures, failures=failures)
    return result


def watch_limits(m: dict, eval_scale=None, notes=None) -> dict:
    """The main run's quiet limits (seconds without a tick or bytes moved) from the qualification's watchdog
    measurements m; the floors are progress_watch.py's defaults. notes receives one line per fallback used."""
    notes = [] if notes is None else notes
    quiet, phases, scale = m.get('quiet_seconds', {}), m.get('phase_seconds', {}), eval_scale or 1.0
    limits = {'train': max(180, 5 * m.get('largest_train_gap_seconds', 0.0)),
              'eval': max(600, 5 * m.get('largest_eval_gap_seconds', 0.0), 3 * m.get('eval_tail_seconds', 0.0) * scale),
              'save': max(600, 3 * quiet.get('save', 0.0)),
              'final': max(600, 3 * quiet.get('final', 0.0)),
              'startup': max(1800, 3 * quiet.get('startup', 0.0))}
    gb = m.get('checkpoint_gb')
    # Only saves long enough for the watcher's polls (15 s) to land inside them show their bytes in io_mb.save;
    # shorter ones are attributed to the next phase, and stay far below every limit anyway.
    seen = phases.get('save', 0.0) >= 30
    if 'save' in phases and (not m.get('io_signal') or (seen and gb and m.get('io_mb', {}).get('save', 0.0) < 500 * gb)):
        limits['save'] = max(limits['save'], 5 * phases['save'])
        notes.append('the byte counters did not see the saves (no /proc io, or io_mb.save below half the checkpoint size: '
                     'io_uring, GPUDirect Storage or mmap writers): the save limit falls back to 5 x the save duration')
    if eval_scale and m.get('largest_eval_gap_seconds', 0.0) == 0 and 'eval' not in phases:
        limits['train'] = max(limits['train'], 3 * quiet.get('train', 0.0) * eval_scale)
        notes.append('the evaluation ran inside train gaps (no eval ticks): the train limit covers the full '
                     'evaluation (3 x the longest quiet train stretch x eval_scale); hangs are found later')
    return {p: math.ceil(v) for p, v in limits.items()}


def prune_list(run_dir, keep: int = 3) -> dict:
    """Checkpoint directories safe to delete from outside the worker (the control host, at finish or during
    a long run): every directory older than the newest `keep` committed checkpoints whose marker and every
    manifest file are present at the manifest size, never FINAL.json's checkpoint or a directory holding one
    of its artifacts, never anything newer. Nothing when fewer than `keep` are complete. Deletes nothing."""
    run = Path(run_dir)
    names = sorted((p.name for p in run.iterdir() if p.is_dir() and NAME.fullmatch(p.name)), reverse=True)
    try:
        record = json.loads((run / 'FINAL.json').read_text())
    except FileNotFoundError:
        record = {}
    final = record.get('final_dir')
    held = {final} | {str(a.get('path', '')).split('/', 1)[0] for a in record.get('artifacts', []) if isinstance(a, dict)}
    kept = []
    for name in names:
        if len(kept) == keep:
            break
        d = run / name
        try:
            files = json.loads((d / 'manifest.json').read_text())['files']
            if (d / 'COMMITTED.json').is_file() and all((d / f['path']).stat().st_size == f['bytes'] for f in files):
                kept.append(name)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    if len(kept) < keep:
        return {'keep': kept, 'final_dir': final, 'prunable': [], 'reason': f'fewer than {keep} complete checkpoints'}
    return {'keep': kept, 'final_dir': final, 'prunable': [n for n in names if n < kept[-1] and n not in held]}


ALIVE_NAME = re.compile(r'(\d{8}T\d{6}Z)-.*\.json')


def read_alive(run_dir) -> tuple[list, list]:
    """The allocations' alive records ({start, last, period_seconds}) in start order, and the names of unreadable
    ones. An unreadable record (an upload cut off) keeps its start from the file name and no last time."""
    records, bad = [], []
    root = Path(run_dir) / 'alive'
    for f in sorted(root.glob('*.json')) if root.is_dir() else []:
        try:
            r = json.loads(f.read_text())
            records.append({k: float(r[k]) for k in ('start', 'last', 'period_seconds')})
        except (OSError, ValueError, KeyError, TypeError):
            bad.append(f.name)
            m = ALIVE_NAME.fullmatch(f.name)
            if m:
                records.append({'start': float(calendar.timegm(time.strptime(m.group(1), '%Y%m%dT%H%M%SZ'))),
                                'last': None, 'period_seconds': 300.0})
    return sorted(records, key=lambda r: r['start']), bad


def _starts(events: list) -> list:
    """Journal events grouped per worker start, in time order (an unreadable entry belongs to no start)."""
    attempts = {}
    for e in events:
        if 'error' not in e:
            attempts.setdefault(e['attempt'], []).append(e)
    order = sorted(attempts, key=lambda a: (min((_epoch(e['time']) for e in attempts[a] if 'time' in e),
                                                default=0), a))
    return [attempts[a] for a in order]


def limits(run_dir, *, submitted_at: float, ceiling_hourly: float, budget: float, now: float | None = None,
           reserved: float = 0.0, spent_before: float = 0.0, free_without_vm: bool = False,
           startup_allowance_minutes: float = 10.0, max_attempts_without_progress: int | None = 10,
           max_attempts: int | None = None, deadline: str | None = None,
           shutdown_reserve_minutes: float = 6.0) -> dict:
    """The run's limits, checked by the worker at each start after it journaled its preflight (so the newest
    start in the journal is this one). Spend is an upper bound at the ceiling (the on-demand price):
    - free_without_vm (the provider bills only while a VM exists): every allocation's alive span plus one
      progress period, plus the startup allowance before the watchdog started, for each allocation known from
      the alive files or the journal. An allocation lost before its watchdog started and before any journal
      entry is not seen. Without readable alive files the bound falls back to the time since submission;
    - otherwise the time since submission (waiting is billed too).
    Plus reserved (controller, storage, spend outside the run) and spent_before (an extra amount, normally 0: with
    submitted_at the run's first submission, earlier jobs of the run are already in the bound). stop_at_utc is when the budget
    runway, less the shutdown reserve, runs out (or the deadline, if earlier): the worker stops gracefully there.
    limit names what is reached now: deadline, attempts, no_progress or budget (None: keep training)."""
    now = time.time() if now is None else now
    if not ceiling_hourly > 0 or not budget > 0:
        raise ValueError('ceiling_hourly and budget must be positive')
    # A start that never trained is not an allocation of its own: one that only put its new VM under the spot price
    # cap and stopped it, or one a limit stopped before training (so a raised limit is not used up by the stop).
    runs = [r for r in _starts(read_journal(run_dir))
            if any(e.get('event') == 'selection' for e in r)
            or not any((e.get('event') == 'price_cap' and e.get('state') == 'switched') or e.get('event') == 'limit'
                       for e in r)]
    allocations = len(runs)
    without_progress = 0
    for run in reversed(runs[:-1]):                 # earlier starts in a row that ended without a commit
        if any(e.get('event') == 'commit' for e in run):
            break
        without_progress += 1
    notes, basis = [], 'since_submission'
    hours = max(0.0, now - submitted_at) / 3600
    if free_without_vm:
        alive, bad = read_alive(run_dir)
        if alive:
            # Each allocation ran from its start to the latest of its alive file's last write and its journal
            # events, plus two progress periods (a write that failed or was still queued behind checkpoint
            # uploads). An unreadable file counts up to the next allocation's start (or now).
            times = sorted(_epoch(e['time']) for e in read_journal(run_dir) if 'time' in e)
            spans = 0.0
            for run in runs:                            # a start older than every alive file: its journal span
                when = [_epoch(e['time']) for e in run if 'time' in e]
                if when and max(when) < alive[0]['start']:
                    spans += max(when) - min(when) + 2 * 300.0
            for i, a in enumerate(alive):
                nxt = alive[i + 1]['start'] if i + 1 < len(alive) else now
                last = a['last'] if a['last'] is not None else nxt
                last = max([last] + [t for t in times if a['start'] <= t < nxt])
                spans += max(0.0, min(last, now) - a['start']) + 2 * a['period_seconds']
            known = max(len(alive), allocations)
            hours, basis = (spans + known * startup_allowance_minutes * 60) / 3600, 'allocations'
            if bad:
                notes.append(f"alive files unreadable ({', '.join(bad)}): counted up to the next allocation")
        else:
            notes.append('no alive files')
    spend = hours * ceiling_hourly + reserved + spent_before
    runway_hours = (budget - spend) / ceiling_hourly - shutdown_reserve_minutes / 60
    stop_at, stop_by = now + max(0.0, runway_hours) * 3600, 'budget'
    if deadline and _epoch(deadline) <= stop_at:
        stop_at, stop_by = _epoch(deadline), 'deadline'
    limit = None
    if deadline and now >= _epoch(deadline):
        limit = 'deadline'
    elif max_attempts is not None and allocations > max_attempts:
        limit = 'attempts'
    elif max_attempts_without_progress is not None and without_progress >= max_attempts_without_progress:
        limit = 'no_progress'
    elif runway_hours <= 0:
        limit = 'budget'
    result = {'limit': limit, 'allocations': allocations, 'allocations_without_progress': without_progress,
              'spend_upper_usd': round(spend, 2), 'spend_basis': basis, 'budget_usd': budget,
              'runway_hours': round(max(0.0, runway_hours), 3),
              'stop_at_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(stop_at)), 'stop_by': stop_by}
    if notes:
        result['notes'] = notes
    return result


def _artifact(run: Path, rel: str) -> dict:
    path = (run / rel).resolve()
    if run.resolve() not in path.parents:
        raise ValueError(f'artifact {rel} must be inside the run directory')
    size, digest = _sha256(path)
    return {'path': rel, 'bytes': size, 'sha256': digest}


def finalize(run_dir, run_id, data_version, model_version, world_size, identity, outcome: str,
             target_step: int | None = None, artifacts=(), require_identity=True, mount_root=None) -> dict:
    """Write FINAL.json once: the newest committed checkpoint that verifies, the outcome and the hashes
    of extra durable artifacts (best model, exported results). 'completed' needs target_step reached."""
    if outcome not in OUTCOMES:
        raise ValueError(f'outcome must be one of {OUTCOMES}')
    run = Path(run_dir)
    sel = select(run, run_id, data_version, model_version, world_size, identity,
                 require_identity=require_identity, mount_root=mount_root)
    if sel['action'] != 'resume':
        raise Blocked('nothing was committed: there is no final checkpoint')
    if outcome == 'completed' and target_step is not None and sel['global_step'] != target_step:
        raise Blocked(f"newest verified checkpoint is step {sel['global_step']}, not the target {target_step}")
    final_dir = Path(sel['path']).name
    manifest = json.loads((Path(sel['path']) / 'manifest.json').read_text())
    record = {'schema_version': 1, 'run_id': run_id, 'outcome': outcome, 'final_step': sel['global_step'],
              'final_dir': final_dir, 'manifest_sha256': sel['manifest_sha256'],
              'attempt': manifest.get('attempt', NAME.fullmatch(final_dir).group(2)),
              'artifacts': [_artifact(run, a) for a in artifacts], 'skipped': sel['skipped'],
              'finalized_at': _utc()}
    if (run / 'FINAL.json').exists():
        raise Blocked(f"FINAL.json already exists (outcome {json.loads((run / 'FINAL.json').read_text()).get('outcome')}): "
                      'this run is finalized')
    _write_once(run / 'FINAL.json', json.dumps(record, sort_keys=True, indent=1).encode())
    return record


def verify_final(run_dir, run_id, data_version, model_version, world_size, identity,
                 require_identity=True, mount_root=None) -> dict:
    """Check that FINAL.json still names the newest verified checkpoint and that its artifacts match."""
    run = Path(run_dir)
    try:
        final = json.loads((run / 'FINAL.json').read_text())
    except (OSError, ValueError) as e:
        raise Blocked(f'no readable FINAL.json: {e}') from e
    if final.get('schema_version') != 1 or final.get('run_id') != run_id or final.get('outcome') not in OUTCOMES:
        raise Blocked('FINAL.json has an unknown schema, run or outcome')
    sel = select(run, run_id, data_version, model_version, world_size, identity,
                 require_identity=require_identity, mount_root=mount_root)
    if sel['action'] != 'resume' or Path(sel['path']).name != final['final_dir'] \
            or sel['manifest_sha256'] != final['manifest_sha256']:
        raise Blocked(f"FINAL.json names {final['final_dir']} but the newest verified checkpoint is "
                      f"{Path(sel['path']).name if sel['action'] == 'resume' else 'none'}")
    for a in final.get('artifacts', []):
        try:
            now = _artifact(run, a['path'])
        except OSError as e:
            raise Blocked(f"artifact {a['path']} unreadable: {e}") from e
        if (now['bytes'], now['sha256']) != (a['bytes'], a['sha256']):
            raise Blocked(f"artifact {a['path']} changed since FINAL.json")
    starts = {}
    for e in read_journal(run):
        if 'error' not in e:
            starts.setdefault(e['attempt'], []).append(e)
    order = sorted(starts, key=lambda a: (min((_epoch(e['time']) for e in starts[a] if 'time' in e), default=0), a))
    return {'ok': True, 'outcome': final['outcome'], 'final_step': final['final_step'],
            'final_dir': final['final_dir'], 'artifacts': len(final.get('artifacts', [])),
            'starts': len(order), 'lineage_warnings': lineage_problems([starts[a] for a in order])}


def _limits_from_env(args):
    """The rendered task's limit variables (lifecycle entrypoint contract) in place of the flags."""
    env = os.environ
    def num(name):
        return float(env[name]) if env.get(name, '').strip() else None
    args.submitted_at = num('CAMPAIGN_SUBMITTED_AT')
    args.budget, args.ceiling_hourly = num('BUDGET_LIMIT_USD'), num('CEILING_HOURLY_USD')
    args.reserved = num('BUDGET_RESERVED_USD') or 0.0
    args.spent_before = num('BUDGET_SPENT_BEFORE_USD') or 0.0
    args.free_without_vm = env.get('FREE_WITHOUT_VM') == '1'
    if num('STARTUP_ALLOWANCE_MINUTES') is not None:
        args.startup_allowance_minutes = num('STARTUP_ALLOWANCE_MINUTES')
    if num('MAX_ATTEMPTS_WITHOUT_PROGRESS') is not None:
        args.max_attempts_without_progress = int(num('MAX_ATTEMPTS_WITHOUT_PROGRESS'))
    if num('MAX_ATTEMPTS') is not None:
        args.max_attempts = int(num('MAX_ATTEMPTS'))
    args.deadline = env.get('CAMPAIGN_DEADLINE_UTC') or None


def _identity_args(p):
    p.add_argument('run_dir')
    p.add_argument('--mount-root', help='the SkyPilot mount point (e.g. /checkpoints); blocks if it is not mounted')
    for name in ('run-id', 'data-version', 'model-version'):
        p.add_argument('--' + name, required=True)
    p.add_argument('--world-size', required=True, type=int)
    for name in vc.IDENTITY_FIELDS:
        p.add_argument('--' + name.replace('_', '-'), required=True)


def _identity(args) -> tuple:
    return (args.run_dir, args.run_id, args.data_version, args.model_version, args.world_size,
            {k: getattr(args, k) for k in vc.IDENTITY_FIELDS})


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    c = sub.add_parser('commit')
    c.add_argument('directory')
    c.add_argument('--fields', required=True, help='JSON file with manifest fields')
    c.add_argument('--files', required=True, help='JSON file with the payload file list and roles')
    _identity_args(sub.add_parser('select'))
    j = sub.add_parser('journal')
    j.add_argument('run_dir')
    j.add_argument('--attempt', required=True)
    j.add_argument('--event', required=True)
    j.add_argument('--field', action='append', default=[], metavar='KEY=VALUE')
    sub.add_parser('journal-show').add_argument('run_dir')
    q = sub.add_parser('journal-check')
    q.add_argument('run_dir')
    q.add_argument('--expect', required=True, help='selection actions per worker start, e.g. fresh,resume')
    q.add_argument('--require', action='append', default=[], help='event every start must journal, e.g. preflight')
    q.add_argument('--require-final', action='store_true', help='FINAL.json must say completed')
    q.add_argument('--watch-summaries', help="the stall watchdog's summaries.jsonl (progress_watch.py)")
    q.add_argument('--eval-scale', type=float, help='the full eval set size over the qualification eval set size '
                   '(omit when the project does not evaluate)')
    q.add_argument('--submitted-at', type=float, help='Unix seconds when the launch command started')
    q.add_argument('--max-memory-fraction', type=float, default=0.9,
                   help='fail when a journaled peak_memory_fraction exceeds this (default 0.9)')
    lm = sub.add_parser('limits')
    lm.add_argument('run_dir')
    lm.add_argument('--submitted-at', type=float, help="Unix seconds the run's first job was submitted")
    lm.add_argument('--ceiling-hourly', type=float, help='the node on-demand price per hour')
    lm.add_argument('--budget', type=float, help="the run's total budget")
    lm.add_argument('--reserved', type=float, default=0.0, help='controller and storage allowance')
    lm.add_argument('--spent-before', type=float, default=0.0,
                    help='an extra amount to count (spend outside the run belongs in --reserved)')
    lm.add_argument('--free-without-vm', action='store_true', help='the provider bills only while a VM exists')
    lm.add_argument('--startup-allowance-minutes', type=float, default=10.0)
    lm.add_argument('--max-attempts-without-progress', type=int, default=10)
    lm.add_argument('--max-attempts', type=int)
    lm.add_argument('--deadline', help='ISO-8601 UTC ending in Z')
    lm.add_argument('--shutdown-reserve-minutes', type=float, default=6.0)
    lm.add_argument('--from-env', action='store_true',
                    help='read every value above from the rendered task environment (the entrypoint contract)')
    lm.add_argument('--now', type=float, help=argparse.SUPPRESS)
    f = sub.add_parser('finalize')
    _identity_args(f)
    f.add_argument('--outcome', required=True, choices=OUTCOMES)
    f.add_argument('--target-step', type=int)
    f.add_argument('--artifact', action='append', default=[], help='path relative to RUN_DIR, e.g. final/best.pt')
    _identity_args(sub.add_parser('verify-final'))
    pr = sub.add_parser('prune-list')
    pr.add_argument('run_dir')
    pr.add_argument('--keep', type=int, default=3, help='complete checkpoints to keep (default 3)')
    args = ap.parse_args(argv)
    try:
        if args.cmd == 'commit':
            result = commit(args.directory, json.loads(Path(args.fields).read_text()),
                            json.loads(Path(args.files).read_text()))
        elif args.cmd == 'journal':
            fields = dict(kv.split('=', 1) for kv in args.field)
            result = {'written': str(journal(args.run_dir, args.attempt, args.event, fields))}
        elif args.cmd == 'journal-show':
            result = read_journal(args.run_dir)
        elif args.cmd == 'journal-check':
            result = check_journal(args.run_dir, [a for a in args.expect.split(',') if a], args.require,
                                   args.submitted_at, args.max_memory_fraction, args.require_final,
                                   args.watch_summaries, args.eval_scale)
            print(json.dumps(result, sort_keys=True))
            return 0 if result['ok'] else 1
        elif args.cmd == 'limits':
            if args.from_env:
                _limits_from_env(args)
            if None in (args.submitted_at, args.ceiling_hourly, args.budget):
                raise ValueError('limits needs --submitted-at, --ceiling-hourly and --budget (or --from-env)')
            result = limits(args.run_dir, submitted_at=args.submitted_at, ceiling_hourly=args.ceiling_hourly,
                            budget=args.budget, now=args.now, reserved=args.reserved, spent_before=args.spent_before,
                            free_without_vm=args.free_without_vm,
                            startup_allowance_minutes=args.startup_allowance_minutes,
                            max_attempts_without_progress=args.max_attempts_without_progress,
                            max_attempts=args.max_attempts, deadline=args.deadline,
                            shutdown_reserve_minutes=args.shutdown_reserve_minutes)
            print(json.dumps(result, sort_keys=True))
            return LIMIT_EXIT if result['limit'] else 0
        elif args.cmd == 'finalize':
            result = finalize(*_identity(args), outcome=args.outcome, target_step=args.target_step,
                              artifacts=args.artifact, mount_root=args.mount_root)
        elif args.cmd == 'prune-list':
            result = prune_list(args.run_dir, max(1, args.keep))
        elif args.cmd == 'verify-final':
            result = verify_final(*_identity(args), mount_root=args.mount_root)
        else:
            result = select(*_identity(args), mount_root=args.mount_root)
    except Blocked as e:
        print(json.dumps({'action': 'blocked', 'reason': str(e)}))
        return 3
    except (vc.Invalid, OSError, KeyError, ValueError) as e:
        print(json.dumps({'ok': False, 'error': f'{type(e).__name__}: {e}'}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
