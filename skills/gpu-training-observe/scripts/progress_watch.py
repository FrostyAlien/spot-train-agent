#!/usr/bin/env python3
"""Worker-side stall watchdog for a training run; stdlib only, vendored in the image at /opt/gpu-skills/.

A job is stalled when it has gone silent: no rank has ticked and the job's processes moved no bytes for longer
than the quiet limit of the phase in force. A save, evaluation or end-of-training phase stays alive as long as it
ticks, reads or writes; a hang goes flat and is caught.

Ticks: every rank calls tick() with Python ints at the events E1-E4, from hooks the training loop runs (never
from a timer or a thread that keeps going while the loop is stuck):

    import progress_watch as pw
    pw.tick('train', count=step, step=step, rank=rank)    # E1 (required): the end of every optimizer step
    pw.tick('eval', count=batch, step=step, rank=rank)     # E2: an evaluation begins (0) and every eval batch ends
    pw.tick('save', count=0, step=step, rank=rank)         # E3: a save begins (from a hook that fires before the write)
    pw.tick('final', count=0, step=step, rank=rank)        # E4: end-of-training work begins
The next 'train' tick ends an evaluation or a save. A PyTorch script without hooks gets its train ticks from rank
mode: torch_rank.py, next to this file.

The watcher wraps the launcher in the task's run:   python3 progress_watch.py watch -- <launcher> ...
Every WATCH_POLL_SECONDS (15) it reads the heartbeats ($HEARTBEAT_DIR/rank-<r>.json), the byte counters of the
launcher's process tree (/proc/<pid>/io: rchar + wchar + write_bytes) and the system's finished writeback
(/proc/vmstat nr_written). The job was last heard at the latest of: the newest tick of a live rank, the last poll
in which the tree moved at least 1 MiB or, during a save or end-of-training work, the system wrote back at 4 MiB/s
or more (a flush the writer waits for; a VM's own logging writes back about 0.1 MiB/s), the start of this allocation. It stalls when
  - it has been silent longer than the quiet limit of the phase in force: of the live ranks' phases, the one with
    the largest limit (no live rank: 'startup' before any tick, 'final' once all ranks exited). STALL_LIMITS sets the limits
    (defaults "train=180,eval=600,save=600,final=600,startup=1800"); at run time 'train' rises to STALL_K (5) x the
    largest train-to-train gap and 'eval' to STALL_K x the largest eval gap (the tail after the last batch included);
  - or a rank's phase listed in STALL_CEILINGS ("phase=seconds,...", none by default) has lasted longer than its
    ceiling, ticking or not.
The whole job is judged, not each rank: ranks waiting in a collective while rank 0 saves or evaluates alone are
no stall. Without readable /proc/<pid>/io (not Linux, a restricted procfs): one WATCH-WARN line, then ticks alone.
On a stall: one `STALL <utc> rank= phase= count= step= gap= limit= rule=quiet|ceiling` line (rank= is the live
rank with the oldest tick), a dump request to every FIFO matching STALL_DUMP_PIPES (the framework's dump pipes),
SIGUSR2 to the rank processes one at a time, 1 s apart (their stacks follow in the job log), 15 s, SIGTERM to the
launcher's process group and every descendant (snapshotted first: torchrun workers lead their own sessions), 45 s,
SIGKILL, exit 93. Every PROGRESS_PERIOD_SECONDS (300) and at start: `PROGRESS <utc> step= phase= age= quiet=`.
At exit: `WATCH-SUMMARY {json}` (gaps, eval tail, phase durations, longest quiet stretch and MB moved per phase,
the watcher's own CPU time), printed and appended to $HEARTBEAT_DIR/summaries.jsonl. A SIGTERM to the watcher
(cancel, runtime timeout) is passed to the launcher, which gets 60 s. A watchdog error prints one WATCH-ERROR line
and turns the watchdog off for that start: it never stops a healthy run.
With $CHECKPOINT_DIR set, the watcher keeps one alive file per allocation on the checkpoint mount,
$CHECKPOINT_DIR/alive/<start>-<host>.json ({start, last, period_seconds}, epoch seconds), rewritten at start, with
every PROGRESS line and at exit: the run's limits (checkpoint_marker.py limits) read how long each allocation ran.
With $NOTIFY_URL set, a stall is also sent through notify.py next to this file (best effort, 20 s at most).
A failure of either prints one WATCH-WARN line and changes nothing else.
"""
import json
import numbers
import os
import signal
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

STALL_EXIT = 93
WRITE_EVERY = 5.0                 # seconds between heartbeat writes of one rank (a phase change writes at once)
LIMITS = {'train': 180.0, 'eval': 600.0, 'save': 600.0, 'final': 600.0, 'startup': 1800.0}
MOVED = 1 << 20                   # bytes per poll that count as activity; a log or metrics trickle stays below
FLUSH = 4 << 20                   # bytes per second of system writeback that count: a flush, not the VM's own logging
OLD = ('STALL_FLOOR_SECONDS', 'STALL_SEED_GAP_SECONDS', 'PHASE_BUDGETS')   # v0.8 settings, refused
PROC = Path('/proc')
_me = {}                          # this process's heartbeat state


class Alive:
    """This allocation's alive file on the checkpoint mount; fails open (one warning, then silent)."""

    def __init__(self, period: float):
        root = os.environ.get('CHECKPOINT_DIR')
        self.start, self.period = time.time(), period
        stamp = datetime.fromtimestamp(self.start, timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        host = ''.join(c for c in os.uname().nodename if c.isalnum() or c in '-.')[:40] or 'host'
        self.path = Path(root) / 'alive' / f'{stamp}-{host}.json' if root else None

    def write(self):
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({'start': self.start, 'last': time.time(),
                                             'period_seconds': self.period}))
        except OSError as e:
            print(f'WATCH-WARN alive file not written ({type(e).__name__}): the limits count from the submission', flush=True)
            self.path = None


def _notify(text: str, event: str) -> None:
    """Best effort through notify.py next to this file; never raises, never waits more than 20 s."""
    if not os.environ.get('NOTIFY_URL'):
        return
    try:
        subprocess.run([sys.executable, str(Path(__file__).with_name('notify.py')), '--event', event, text],
                       timeout=20, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        print(f'WATCH-WARN notice not sent ({type(e).__name__})', flush=True)


def _dir() -> Path:
    return Path(os.environ.get('HEARTBEAT_DIR', '/tmp/heartbeat'))


def _env(name, default):
    return float(os.environ.get(name) or default)


def _n(v) -> int:
    """A plain int. A framework tensor is never converted (int() of a GPU tensor waits for the device): -1."""
    return v if type(v) is int else int(v) if isinstance(v, (numbers.Real, str)) else -1


def tick(phase: str, count: int = 0, step: int = 0, rank: int = 0, now: float = None) -> None:
    """Record progress of this rank. Cheap: writes its file at most every 5 s, at once on a phase change or a
    new largest gap. Measures the largest gap between consecutive 'train' ticks and between consecutive 'eval'
    ticks (with the tail from the last eval tick to the next tick), and the duration of every other phase."""
    try:
        _tick(str(phase), _n(count), _n(step), _n(rank), time.monotonic() if now is None else now)
    except Exception:  # noqa: BLE001 - never break training over a heartbeat
        pass


def _tick(phase: str, count: int, step: int, rank: int, now: float) -> None:
    s = _me
    if not s:
        s.update(phase=None, phase_start=now, phase_max={}, max_gap=0.0, max_eval_gap=0.0, eval_tail=0.0,
                 train_at=None, eval_at=None, train_ticks=0)
    changed = s['phase'] != phase
    if changed:
        old = s['phase']
        if old not in (None, 'train'):
            s['phase_max'][old] = round(max(s['phase_max'].get(old, 0.0), now - s['phase_start']), 1)
        if old == 'eval' and s['eval_at'] is not None:     # the tail: last eval batch to the next tick
            tail = round(now - s['eval_at'], 1)
            s['eval_tail'] = max(s['eval_tail'], tail)
            s['max_eval_gap'] = max(s['max_eval_gap'], tail)
        s.update(phase=phase, phase_start=now, train_at=None, eval_at=None)
    if phase in ('train', 'eval'):
        at, key = ('train_at', 'max_gap') if phase == 'train' else ('eval_at', 'max_eval_gap')
        if s[at] is not None and round(now - s[at], 1) > s[key]:
            s[key] = round(now - s[at], 1)
            changed = True
        s[at] = now
        s['train_ticks'] += phase == 'train'
    if changed or now - s.get('written', float('-inf')) >= WRITE_EVERY:
        record = {'pid': os.getpid(), 'rank': rank, 'phase': phase, 'count': count, 'step': step, 'tick': now,
                  'phase_start': s['phase_start'], 'max_gap': s['max_gap'], 'max_eval_gap': s['max_eval_gap'],
                  'eval_tail': s['eval_tail'], 'phase_max': s['phase_max'], 'train_ticks': s['train_ticks']}
        d = _dir()
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f'.rank-{rank}.{os.getpid()}.tmp'
        tmp.write_text(json.dumps(record))
        os.replace(tmp, d / f'rank-{rank}.json')              # atomic: the watcher never reads half a file
        s['written'] = now


def read_ranks(d: Path) -> list:
    out = []
    for f in sorted(d.glob('rank-*.json')):
        try:
            r = json.loads(f.read_text())
        except (OSError, ValueError):
            continue                                         # replaced between list and read: next poll
        if (isinstance(r, dict) and isinstance(r.get('phase'), str)
                and all(type(r.get(k)) in (int, float) for k in ('tick', 'phase_start', 'pid'))):
            out.append(r)
    return out


def alive(pid) -> bool:
    """The rank's process still runs (a zombie waiting for its launcher to reap it does not)."""
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, ValueError, TypeError):
        return False
    except PermissionError:
        return True
    try:
        return (PROC / str(int(pid)) / 'stat').read_text().rsplit(')', 1)[1].split()[0] != 'Z'
    except (OSError, IndexError):
        return True


def limit(phase: str, ranks: list, limits: dict, k: float) -> float:
    """The quiet limit of a phase; train and eval rise to k x the largest gap the live ranks have seen."""
    base = limits.get(phase, limits['train'])
    gap = {'train': 'max_gap', 'eval': 'max_eval_gap'}.get(phase)
    return max(base, k * max((r.get(gap, 0.0) for r in ranks), default=0.0)) if gap else base


def in_force(ranks: list, limits: dict, k: float, ended: bool = False):
    """(phase, limit) that judges the job: the live ranks' phase with the largest limit. With no live rank:
    'startup' before any rank ticked; 'final' once every rank that ticked has exited (the launcher's own
    end-of-run work, or a restart of its workers)."""
    if not ranks:
        phase = 'final' if ended else 'startup'
        return phase, limit(phase, ranks, limits, k)
    return max(((r['phase'], limit(r['phase'], ranks, limits, k)) for r in ranks), key=lambda x: x[1])


def judge(ranks: list, now: float, heard: float, limits: dict, k: float, ceilings: dict, ended=False):
    """Pure decision over the live ranks: None, or (rule, rank record, phase, seconds, limit). heard is the later
    of the allocation's start and the last poll that moved bytes; a live rank's tick counts too."""
    heard = max([heard] + [r['tick'] for r in ranks])
    phase, lim = in_force(ranks, limits, k, ended)
    if now - heard > lim:
        who = min(ranks, key=lambda r: r['tick'], default={'rank': '-', 'count': 0, 'step': 0})
        return 'quiet', who, phase, now - heard, lim
    for r in ranks:
        if r['phase'] in ceilings and now - r['phase_start'] > ceilings[r['phase']]:
            return 'ceiling', r, r['phase'], now - r['phase_start'], ceilings[r['phase']]
    return None


def pairs(text: str) -> dict:
    out = {}
    for part in filter(None, (p.strip() for p in (text or '').split(','))):
        name, _, value = part.partition('=')
        out[name.strip()] = float(value)
    return out


class Io:
    """Bytes moved since the last poll by a process tree (per-pid deltas of /proc/<pid>/io: an exited process
    drops out, a new one counts from zero) and by the system's finished writeback (/proc/vmstat nr_written,
    which covers fsync and close waits, where the writer's own counters are flat). A parent that reaps a child
    takes over the child's lifetime counters: the part already counted is subtracted from the parent's jump."""
    FIELDS = ('rchar', 'wchar', 'write_bytes')

    def __init__(self, proc: Path = None):
        self.proc, self.last, self.written, self.credit = proc or PROC, {}, None, {}
        try:
            self.ok = self._bytes('self') >= 0
        except (OSError, ValueError):
            self.ok = False

    def _bytes(self, pid) -> int:
        fields = dict(line.split(':', 1) for line in (self.proc / str(pid) / 'io').read_text().splitlines() if ':' in line)
        return sum(int(fields[f]) for f in self.FIELDS)

    def moved(self, tree: dict) -> tuple:
        """(bytes the tree moved, bytes the system wrote back) since the previous call; tree maps pid to ppid."""
        def depth(pid, seen=()):
            return 0 if tree.get(pid) not in tree or pid in seen else 1 + depth(tree[pid], (*seen, pid))
        now, moved = {}, 0
        for pid in sorted(tree, key=depth):                  # parents before children: a reap between the two
            try:                                             # reads shows in the next poll, where the credit waits
                now[pid] = self._bytes(pid)
            except (OSError, ValueError, KeyError):
                continue                                     # exited between the scan and the read
        for pid, (total, ppid) in self.last.items():
            if pid not in now:                               # gone: its parent takes over its counters
                self.credit[ppid] = [self.credit.get(ppid, [0, 0])[0] + total, 2]   # for this poll and the next
        for pid, total in now.items():
            raw = max(0, total - self.last.get(pid, (0, None))[0])
            used = min(raw, self.credit.get(pid, [0])[0])
            if used:
                self.credit[pid][0] -= used
            moved += raw - used
        for ppid in list(self.credit):
            self.credit[ppid][1] -= 1
            if self.credit[ppid][0] <= 0 or self.credit[ppid][1] <= 0:
                del self.credit[ppid]
        self.last, written, back = {pid: (now[pid], tree[pid]) for pid in now}, None, 0
        try:
            for line in (self.proc / 'vmstat').read_text().splitlines():
                if line.startswith('nr_written '):
                    written = int(line.split()[1]) * os.sysconf('SC_PAGESIZE')
        except (OSError, ValueError, IndexError):
            pass
        if written is not None:
            back = max(0, written - self.written) if self.written is not None else 0
            self.written = written
        return moved, back


def _utc() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def parents() -> dict:
    """pid -> parent pid of every process."""
    out = {}
    if PROC.is_dir():
        for d in os.listdir(PROC):
            if d.isdigit():
                try:
                    out[int(d)] = int((PROC / d / 'stat').read_text().rsplit(')', 1)[1].split()[1])
                except (OSError, ValueError, IndexError):
                    continue
    else:                                                    # not Linux (tests): ps
        for line in subprocess.run(['ps', '-A', '-o', 'pid=,ppid='], capture_output=True, text=True).stdout.split('\n'):
            if line.strip():
                pid, ppid = map(int, line.split())
                out[pid] = ppid
    return out


def descendants(root: int, links: dict = None) -> set:
    """Every process below root, by parent links (a new session does not break them)."""
    children = {}
    for pid, ppid in (parents() if links is None else links).items():
        children.setdefault(ppid, []).append(pid)
    found, todo = set(), [root]
    while todo:
        for c in children.get(todo.pop(), []):
            if c not in found:
                found.add(c)
                todo.append(c)
    return found


def _kill(pids, sig):
    for pid in pids:
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def _dump_requests() -> int:
    """Ask the framework for a diagnostic dump: one write to each FIFO matching STALL_DUMP_PIPES, a glob the
    renderer sets (for PyTorch: its flight recorder's pipes). Non-blocking; a missing pipe or reader is skipped."""
    pattern = os.environ.get('STALL_DUMP_PIPES')
    asked = 0
    for p in sorted(Path(pattern).parent.glob(Path(pattern).name)) if pattern else []:
        try:
            if stat.S_ISFIFO(p.stat().st_mode):
                fd = os.open(p, os.O_WRONLY | os.O_NONBLOCK)
                try:
                    os.write(fd, b'1\n')
                finally:
                    os.close(fd)
                asked += 1
        except OSError:
            continue
    return asked


def _dump_stacks(pids, pause: float = 1.0):
    """SIGUSR2 to one rank at a time: dumps written at once interleave in the log and cannot be read."""
    for i, pid in enumerate(sorted(pids)):
        if i:
            time.sleep(pause)
        _kill([pid], signal.SIGUSR2)


def _stop(proc, wait: float):
    """SIGTERM the launcher's group and every descendant, wait, then SIGKILL whatever is left."""
    tree = descendants(proc.pid) | {proc.pid}
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    _kill(tree, signal.SIGTERM)
    try:
        proc.wait(wait)
    except subprocess.TimeoutExpired:
        pass
    tree |= descendants(proc.pid)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    _kill(tree, signal.SIGKILL)
    proc.wait()


def _summary(d: Path, ranks: list, watcher: dict = None) -> dict:
    phases = {}
    for r in ranks:
        done = dict(r.get('phase_max') or {})
        if r.get('phase') != 'train':                         # the phase a rank ended in (often `final`)
            done[r['phase']] = max(done.get(r['phase'], 0.0), round(r['tick'] - r['phase_start'], 1))
        for name, seconds in done.items():
            phases[name] = max(phases.get(name, 0.0), seconds)
    def most(key):
        return max((r.get(key, 0.0) for r in ranks), default=0.0)
    out = {'largest_train_gap_seconds': most('max_gap'), 'largest_eval_gap_seconds': most('max_eval_gap'),
           'eval_tail_seconds': most('eval_tail'), 'phase_seconds': phases,
           'train_ticks': max((r.get('train_ticks', 0) for r in ranks), default=0), **(watcher or {})}
    print('WATCH-SUMMARY ' + json.dumps(out, sort_keys=True), flush=True)
    try:
        with open(d / 'summaries.jsonl', 'a') as f:
            f.write(json.dumps(out, sort_keys=True) + '\n')
    except OSError:
        pass
    return out


def watch(argv: list) -> int:
    old = [n for n in OLD if n in os.environ]
    if old:
        print(f"WATCH-ERROR {', '.join(old)}: v0.8 settings; render the task again with this version "
              '(STALL_LIMITS, STALL_CEILINGS)', flush=True)
        return 2
    d = _dir()
    d.mkdir(parents=True, exist_ok=True)
    for f in d.glob('rank-*.json'):                          # a previous start's heartbeats are not ours
        f.unlink()
    limits, ceilings = {**LIMITS, **pairs(os.environ.get('STALL_LIMITS', ''))}, pairs(os.environ.get('STALL_CEILINGS', ''))
    k, period = _env('STALL_K', 5), _env('PROGRESS_PERIOD_SECONDS', 300)
    poll = _env('WATCH_POLL_SECONDS', 15)
    dump_wait, term_wait = _env('STALL_DUMP_WAIT_SECONDS', 15), _env('STALL_TERM_WAIT_SECONDS', 45)
    io = Io()
    if not io.ok:
        print('WATCH-WARN /proc/<pid>/io is not readable here: judging by ticks alone', flush=True)
    started = io_at = t0 = time.monotonic()
    beat = Alive(period)
    beat.write()
    cpu0 = time.process_time()
    quiet_max, io_mb = {}, {}
    proc = subprocess.Popen(argv, start_new_session=True)
    stopping = []

    def forward(signum, _frame):                             # cancel or runtime timeout: stop the launcher
        stopping.append(signum)
    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)

    def stats():
        return {'quiet_seconds': quiet_max, 'io_mb': {p: round(v, 1) for p, v in io_mb.items()}, 'io_signal': io.ok,
                'watcher_cpu_seconds': round(time.process_time() - cpu0, 2), 'wall_seconds': round(time.monotonic() - t0)}
    last_progress, watching = None, True
    while True:
        if watching:
            try:
                now, ranks = time.monotonic(), read_ranks(d)
                live = [r for r in ranks if alive(r['pid'])]  # an exited rank cannot stall
                if ranks and not live:                        # all ranks gone, the launcher still up
                    started = max(started, max(r['tick'] for r in ranks))
                ended = bool(ranks) and not live              # every rank that ticked has exited
                moved = 0
                if io.ok:
                    links = parents()
                    moved, back = io.moved({p: links.get(p) for p in descendants(proc.pid, links) | {proc.pid}})
                    # System writeback is not the job's alone: it counts only where a blocked flush is expected.
                    flushing = ended or any(r['phase'] in ('save', 'final') for r in live)
                    if moved >= MOVED or (flushing and back >= FLUSH * poll):
                        io_at = now
                phase, _ = in_force(live, limits, k, ended)
                quiet = now - max([started, io_at] + [r['tick'] for r in live])
                quiet_max[phase] = max(quiet_max.get(phase, 0.0), round(quiet, 1))
                io_mb[phase] = io_mb.get(phase, 0.0) + moved / 1e6
                if last_progress is None or now - last_progress >= period:
                    oldest = min(live, key=lambda r: r['tick'], default=None)
                    print(f"PROGRESS {_utc()} step={min((r['step'] for r in live), default=0)} phase={phase} "
                          f"age={now - (oldest['tick'] if oldest else started):.0f} quiet={quiet:.0f}", flush=True)
                    last_progress = now
                    beat.write()
                verdict = judge(live, now, max(started, io_at), limits, k, ceilings, ended)
            except Exception as e:  # noqa: BLE001 - a watchdog bug must never stop a healthy run: fail open
                print(f'WATCH-ERROR {type(e).__name__}: {e}; no stall detection for the rest of this start', flush=True)
                watching, verdict = False, None
            if verdict:
                rule, r, phase, gap, lim = verdict
                print(f"STALL {_utc()} rank={r.get('rank')} phase={phase} count={r.get('count')} "
                      f"step={r.get('step')} gap={gap:.0f} limit={lim:.0f} rule={rule}", flush=True)
                asked = _dump_requests()
                if asked:
                    print(f'WATCH-NOTE dump requested through {asked} pipe(s) ({os.environ["STALL_DUMP_PIPES"]})', flush=True)
                _dump_stacks({int(x['pid']) for x in live})             # every rank's stack into the log
                _notify(f"STALL in phase {phase} at step {r.get('step')} (silent {gap:.0f} s, limit {lim:.0f} s): "
                        'stopping the run, exit 93; every rank\'s stack is in the job log', 'stall')
                time.sleep(dump_wait)
                _stop(proc, term_wait)
                beat.write()
                _summary(d, read_ranks(d), stats())
                return STALL_EXIT
        try:
            code = proc.wait(poll)
            break
        except subprocess.TimeoutExpired:
            pass
        if stopping:
            _stop(proc, 60)
            code = proc.returncode
            break
    beat.write()
    _summary(d, read_ranks(d), stats())
    return code if code >= 0 else 128 - code


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) >= 3 and argv[:2] == ['watch', '--']:
        return watch(argv[2:])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main())
