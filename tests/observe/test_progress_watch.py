import sys as _sys
from pathlib import Path as _Path
SKILL = _Path(__file__).resolve().parents[2] / 'skills' / 'gpu-training-observe'   # the skill under test
_sys.path.insert(0, str(SKILL / 'scripts'))
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import progress_watch as pw

HERE = SKILL / 'scripts'
FAST = {'WATCH_POLL_SECONDS': '0.2', 'STALL_DUMP_WAIT_SECONDS': '0.5', 'STALL_TERM_WAIT_SECONDS': '2',
        'STALL_LIMITS': 'train=1.5', 'PROGRESS_PERIOD_SECONDS': '300'}
L = dict(pw.LIMITS)


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = Path(f'/proc/{pid}/stat')                      # a zombie waiting for its reaper is dead too
    return not (stat.exists() and stat.read_text().rsplit(')', 1)[1].split()[0] == 'Z')


def rank(**over):
    r = {'pid': 1, 'rank': 0, 'phase': 'train', 'count': 5, 'step': 5, 'tick': 100.0, 'phase_start': 50.0,
         'max_gap': 0.0, 'max_eval_gap': 0.0, 'phase_max': {}}
    r.update(over)
    return r


class TickTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ['HEARTBEAT_DIR'] = self.tmp.name
        self.addCleanup(os.environ.pop, 'HEARTBEAT_DIR')
        pw._me.clear()

    def read(self, r=0):
        return json.loads((Path(self.tmp.name) / f'rank-{r}.json').read_text())

    def test_tick_writes_the_phase_label_atomically(self):
        pw.tick('eval', count=3, step=1200, rank=1, now=10.0)
        rec = self.read(1)
        self.assertEqual((rec['phase'], rec['count'], rec['step'], rec['pid'], rec['phase_start']),
                         ('eval', 3, 1200, os.getpid(), 10.0))
        self.assertEqual([p.name for p in Path(self.tmp.name).iterdir()], ['rank-1.json'])   # no temp file left

    def test_writes_are_rate_limited_except_on_a_phase_change(self):
        pw.tick('train', 1, 1, now=0.0)
        pw.tick('train', 2, 2, now=2.0)                        # a new largest gap: written
        pw.tick('train', 3, 3, now=3.0)
        self.assertEqual(self.read()['step'], 2)              # within 5 s, nothing new: not written
        pw.tick('train', 4, 4, now=7.5)
        self.assertEqual(self.read()['step'], 4)
        pw.tick('save', 0, 3, now=6.0)                         # a phase change is written at once
        self.assertEqual(self.read()['phase'], 'save')

    def test_largest_train_gap_and_phase_durations(self):
        for t in (0.0, 1.0, 3.5):
            pw.tick('train', now=t)                            # gaps 1.0, 2.5
        pw.tick('eval', now=4.0)
        pw.tick('eval', now=40.0)
        pw.tick('train', now=44.0)                             # eval lasted 40 s; that time is no train gap
        pw.tick('train', now=45.0)
        pw.tick('save', now=50.0)
        pw.tick('train', now=60.0)
        rec = self.read()
        self.assertEqual((rec['max_gap'], rec['train_ticks']), (2.5, 6))
        self.assertEqual(rec['phase_max'], {'eval': 40.0, 'save': 10.0})

    def test_a_tensor_is_never_converted(self):
        calls = []
        class Tensor:                                          # int() of a GPU tensor waits for the device
            def __int__(self):
                calls.append(1)
                return 7
        pw.tick('train', count=Tensor(), step=Tensor(), rank='1', now=0.0)
        self.assertEqual((self.read(1)['step'], self.read(1)['count'], calls), (-1, -1, []))
        import fractions
        pw.tick('train', count=True, step=fractions.Fraction(9, 1), rank=1, now=1.0)   # numbers convert
        self.assertEqual((self.read(1)['count'], self.read(1)['step']), (1, 9))

    def test_a_heartbeat_failure_never_breaks_training(self):
        pw.tick('train', count=float('inf'), now=1.0)          # OverflowError: skipped, no exception
        pw.tick('train', rank='x', now=1.5)                    # ValueError: skipped
        class Odd(float):
            def __int__(self):
                raise RuntimeError('meta tensor')
        pw.tick('train', count=Odd(1), now=2.0)                # any exception: skipped
        os.environ['HEARTBEAT_DIR'] = '/proc/no-such-dir' if Path('/proc').is_dir() else '/dev/null/x'
        pw.tick('eval', now=3.0)                               # no exception

    def test_eval_gaps_and_the_tail_after_the_last_batch(self):
        pw.tick('train', now=0.0)
        pw.tick('eval', 0, now=1.0)
        pw.tick('eval', 1, now=4.0)                            # gap 3
        pw.tick('eval', 2, now=5.0)
        pw.tick('train', now=12.0)                             # tail 7: metrics after the last batch
        pw.tick('eval', 0, now=20.0)
        pw.tick('save', now=22.0)                              # tail 2
        rec = self.read()
        self.assertEqual((rec['max_eval_gap'], rec['eval_tail'], rec['max_gap']), (7.0, 7.0, 0.0))


class JudgeTests(unittest.TestCase):
    def test_startup_before_any_tick_has_its_own_limit(self):
        self.assertIsNone(pw.judge([], now=1000.0, heard=0.0, limits=L, k=5, ceilings={}))
        rule, r, phase, gap, lim = pw.judge([], now=1900.0, heard=0.0, limits=L, k=5, ceilings={})   # 1800 s
        self.assertEqual((rule, r['rank'], phase, gap, lim), ('quiet', '-', 'startup', 1900.0, 1800.0))
        self.assertEqual(pw.judge([], 700.0, 0.0, {**L, 'startup': 600}, 5, {})[0], 'quiet')

    def test_bytes_moved_keep_a_silent_phase_alive(self):
        save = [rank(phase='save', tick=100.0, phase_start=100.0)]      # no tick for 2000 s
        self.assertIsNone(pw.judge(save, 2100.0, heard=2090.0, limits=L, k=5, ceilings={}))   # bytes 10 s ago
        rule, r, phase, gap, lim = pw.judge(save, 2100.0, heard=1400.0, limits=L, k=5, ceilings={})
        self.assertEqual((rule, phase, gap, lim), ('quiet', 'save', 700.0, 600.0))

    def test_train_limit_floor_and_learned_gap(self):
        quiet = [rank(tick=100.0)]
        self.assertIsNone(pw.judge(quiet, 279.0, 0, L, 5, {}))
        self.assertEqual(pw.judge(quiet, 281.0, 0, L, 5, {})[:3:2], ('quiet', 'train'))
        learned = [rank(tick=100.0, max_gap=50.0)]             # 5 x 50 s = 250 s
        self.assertIsNone(pw.judge(learned, 349.0, 0, L, 5, {}))
        self.assertEqual(pw.judge(learned, 351.0, 0, L, 5, {})[4], 250.0)
        self.assertIsNone(pw.judge(quiet, 399.0, 0, {**L, 'train': 300}, 5, {}))   # the qualification's limit

    def test_eval_limit_learns_from_eval_gaps(self):
        ev = [rank(phase='eval', tick=100.0, max_eval_gap=200.0)]      # 5 x 200 s = 1000 s > 600 s
        self.assertIsNone(pw.judge(ev, 1099.0, 0, L, 5, {}))
        self.assertEqual(pw.judge(ev, 1101.0, 0, L, 5, {})[4], 1000.0)

    def test_the_job_is_judged_as_a_whole(self):
        # rank 0 saves alone (ticked save 500 s ago); rank 1 waits in a collective with a stale train tick
        ranks = [rank(rank=0, phase='save', tick=500.0, phase_start=500.0), rank(rank=1, tick=450.0, step=4)]
        self.assertIsNone(pw.judge(ranks, 1000.0, 0, L, 5, {}))       # the save's 600 s limit governs
        rule, r, phase, gap, lim = pw.judge(ranks, 1101.0, 0, L, 5, {})
        self.assertEqual((rule, r['rank'], r['step'], phase, gap, lim), ('quiet', 1, 4, 'save', 601.0, 600.0))

    def test_any_live_tick_counts(self):
        ranks = [rank(rank=0, tick=990.0), rank(rank=1, tick=500.0)]
        self.assertIsNone(pw.judge(ranks, 1000.0, 0, L, 5, {}))

    def test_no_live_rank_after_the_ranks_exited(self):
        self.assertEqual(pw.in_force([], L, 5, ended=True), ('final', 600.0))
        self.assertEqual(pw.judge([], 700.0, 0.0, L, 5, {}, ended=True)[2:], ('final', 700.0, 600.0))

    def test_ceilings_only_when_set(self):
        endless = [rank(phase='eval', tick=999.0, phase_start=100.0)]       # ticked a second ago
        self.assertIsNone(pw.judge(endless, 1000.0, 0, L, 5, {}))
        rule, r, phase, lasted, lim = pw.judge(endless, 1000.0, 0, L, 5, {'eval': 600})
        self.assertEqual((rule, phase, lasted, lim), ('ceiling', 'eval', 900.0, 600))

    def test_stack_dumps_go_one_rank_at_a_time(self):
        from unittest import mock
        events = []
        with mock.patch.object(pw.os, 'kill', lambda pid, sig: events.append(('kill', pid, sig))), \
                mock.patch.object(pw.time, 'sleep', lambda s: events.append(('sleep', s))):
            pw._dump_stacks({30, 10, 20})
        u = signal.SIGUSR2
        self.assertEqual(events, [('kill', 10, u), ('sleep', 1.0), ('kill', 20, u), ('sleep', 1.0), ('kill', 30, u)])

    def test_pairs_from_env_text(self):
        self.assertEqual(pw.pairs('startup=600, eval=300,save=120,final=900'),
                         {'startup': 600.0, 'eval': 300.0, 'save': 120.0, 'final': 900.0})
        self.assertEqual(pw.pairs(''), {})


class IoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.proc = Path(self.tmp.name)

    def write(self, pid, rchar, wchar, write_bytes=0):
        (self.proc / str(pid)).mkdir(exist_ok=True)
        (self.proc / str(pid) / 'io').write_text(f'rchar: {rchar}\nwchar: {wchar}\nsyscr: 1\nsyscw: 1\n'
                                                 f'read_bytes: 99999\nwrite_bytes: {write_bytes}\n'
                                                 'cancelled_write_bytes: 0\n')

    def vmstat(self, pages):
        (self.proc / 'vmstat').write_text(f'nr_dirty 5\nnr_written {pages}\nnr_dirtied 9\n')

    def test_per_pid_deltas_new_and_exited_processes(self):
        self.write('self', 1, 1)
        self.write(10, 100, 50, 10)
        self.vmstat(1000)
        io = pw.Io(self.proc)
        self.assertTrue(io.ok)
        self.assertEqual(io.moved({10: 1}), (160, 0))          # a new pid counts from zero; writeback baseline
        self.write(10, 200, 50, 10)
        self.write(11, 5, 5)                                   # a new process
        self.vmstat(1003)
        self.assertEqual(io.moved({10: 1, 11: 10, 12: 10}), (110, 3 * os.sysconf('SC_PAGESIZE')))   # 12: no file
        (self.proc / '10' / 'io').unlink()                     # exited
        self.assertEqual(io.moved({10: 1, 11: 10}), (0, 0))

    def test_a_reaped_child_is_not_counted_twice(self):
        self.write('self', 1, 1)
        self.write(7, 10, 0)                                   # the launcher
        self.write(8, 0, 600, 600)                             # a rank: 1200 counted bytes
        io = pw.Io(self.proc)
        self.assertEqual(io.moved({7: 1, 8: 7}), (1210, 0))
        self.write(8, 0, 650, 650)
        self.assertEqual(io.moved({7: 1, 8: 7}), (100, 0))
        (self.proc / '8' / 'io').unlink()                      # 50 more bytes, exit, and 7 reaps it: Linux adds
        self.write(7, 10, 675, 675)                            # the child's lifetime counters (1350) to 7's
        self.assertEqual(io.moved({7: 1}), (50, 0))            # only what the child moved since the last poll
        self.assertEqual(io.moved({7: 1}), (0, 0))

    def test_a_reap_between_the_parent_read_and_the_child_read(self):
        self.write('self', 1, 1)
        self.write(7, 10, 0)
        self.write(8, 0, 600, 600)
        io = pw.Io(self.proc)
        io.moved({7: 1, 8: 7})
        (self.proc / '8' / 'io').unlink()                      # the parent was read before it reaped the child
        self.assertEqual(io.moved({7: 1, 8: 7}), (0, 0))
        self.write(7, 10, 650, 650)                            # next poll: the child's lifetime shows in 7
        self.assertEqual(io.moved({7: 1}), (100, 0))           # only what the child moved since it was counted
        self.write(7, 10, 700, 700)
        self.assertEqual(io.moved({7: 1}), (100, 0))           # the credit is used up: real bytes count

    def test_an_unused_credit_expires(self):
        self.write('self', 1, 1)
        self.write(7, 10, 0)
        self.write(8, 0, 600, 600)
        io = pw.Io(self.proc)
        io.moved({7: 1, 8: 7})
        (self.proc / '8' / 'io').unlink()                      # gone, and 7 never takes its counters
        self.assertEqual(io.moved({7: 1}), (0, 0))
        self.assertEqual(io.moved({7: 1}), (0, 0))
        self.write(7, 10, 300, 300)
        self.assertEqual(io.moved({7: 1}), (600, 0))           # 7's own bytes are not masked

    def test_unreadable_proc_fails_open(self):
        self.assertFalse(pw.Io(self.proc / 'missing').ok)


class DumpRequestTests(unittest.TestCase):
    def test_writes_each_matching_fifo_without_blocking(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        stem = Path(tmp.name) / 'fr-'
        fifo = Path(f'{stem}0.pipe')
        os.mkfifo(fifo)
        Path(f'{stem}1.pipe').write_text('')                   # not a FIFO: skipped
        reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        self.addCleanup(os.close, reader)
        os.environ['STALL_DUMP_PIPES'] = f'{stem}*.pipe'
        self.addCleanup(os.environ.pop, 'STALL_DUMP_PIPES', None)
        self.assertEqual(pw._dump_requests(), 1)
        self.assertEqual(os.read(reader, 16), b'1\n')
        os.mkfifo(f'{stem}2.pipe')                             # no reader: ENXIO, skipped at once
        self.assertEqual(pw._dump_requests(), 1)
        os.environ.pop('STALL_DUMP_PIPES')
        self.assertEqual(pw._dump_requests(), 0)


CHILD = '''import faulthandler, os, signal, subprocess, sys, time
sys.path.insert(0, {here!r})
import progress_watch as pw
faulthandler.register(signal.SIGUSR2, all_threads=True)
mode = sys.argv[1]
if mode == 'grandchild':                                   # a worker that leads its own session
    subprocess.Popen([sys.executable, '-c', 'import os, sys, time; open(sys.argv[1], "w").write(str(os.getpid())); time.sleep(60)',
                      {mark!r}], start_new_session=True)
for step in range(1, 4) if mode != 'ranks-gone' else ():  # warming up: every gap is a new largest one
    pw.tick('train', count=step, step=step, rank=0)
    time.sleep(0.2 * step)
if mode == 'exit3':
    sys.exit(3)
if mode == 'rank1-exits':                                   # another rank ticks once and is done early
    subprocess.run([sys.executable, '-c', 'import sys; sys.path.insert(0, sys.argv[1]); import progress_watch as pw; '
                    'pw.tick("train", 1, 1, rank=1)', {here!r}], check=True)
    for step in range(4, 30):                               # rank 0 keeps going for ~3 s
        pw.tick('train', count=step, step=step, rank=0)
        time.sleep(0.1)
if mode in ('hang', 'grandchild'):
    time.sleep(60)                                          # stuck: no more ticks
if mode == 'term':
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(7))
    time.sleep(60)
if mode == 'ranks-gone':                                    # the only rank exited; the launcher hangs in its teardown
    subprocess.run([sys.executable, '-c', 'import sys; sys.path.insert(0, sys.argv[1]); import progress_watch as pw; '
                    'pw.tick("train", 1, 1, rank=1)', {here!r}], check=True)
    time.sleep(60)
if mode == 'flush':                                         # a save the writer waits on, then done
    pw.tick('save', count=0, step=3, rank=0)
    time.sleep(4)
    pw.tick('train', count=4, step=4, rank=0)
if mode == 'endless-eval':                                  # ticks forever without finishing an evaluation
    for i in range(600):
        pw.tick('eval', count=i, step=3, rank=0)
        time.sleep(0.1)
'''


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.mark = self.dir / 'grandchild-alive'
        self.child = self.dir / 'child.py'
        self.child.write_text(CHILD.format(here=str(HERE), mark=str(self.mark)))

    def start(self, mode, **env):
        return subprocess.Popen([sys.executable, str(HERE / 'progress_watch.py'), 'watch', '--',
                                 sys.executable, str(self.child), mode],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                env={**os.environ, **FAST, 'HEARTBEAT_DIR': str(self.dir / 'hb'), **env})

    def test_the_summary_includes_the_phase_a_rank_ended_in(self):
        hb = self.dir / 'hb'; hb.mkdir()
        (hb / 'rank-0.json').write_text(json.dumps(rank(phase='final', tick=130.0, phase_start=100.0, max_gap=2.0,
                                                        phase_max={'eval': 9.0}, train_ticks=40)))
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            out = pw._summary(hb, pw.read_ranks(hb))
        self.assertEqual(out, {'largest_train_gap_seconds': 2.0, 'largest_eval_gap_seconds': 0.0, 'eval_tail_seconds': 0.0,
                               'phase_seconds': {'eval': 9.0, 'final': 30.0}, 'train_ticks': 40})

    def test_a_finished_run_passes_its_exit_code_and_writes_the_summary(self):
        p = self.start('ok')
        out, _ = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 0, out)
        self.assertIn('PROGRESS ', out)
        summary = json.loads(out.split('WATCH-SUMMARY ', 1)[1].splitlines()[0])
        self.assertGreater(summary['largest_train_gap_seconds'], 0)
        self.assertEqual(summary['io_signal'], pw.Io().ok)
        self.assertIn('train', summary['quiet_seconds'])
        self.assertGreaterEqual(summary['watcher_cpu_seconds'], 0)
        self.assertEqual(json.loads((self.dir / 'hb' / 'summaries.jsonl').read_text()), summary)
        p = self.start('exit3')
        p.communicate(timeout=30)
        self.assertEqual(p.returncode, 3)

    def test_a_stall_prints_the_line_dumps_stacks_kills_the_tree_and_exits_93(self):
        p = self.start('grandchild')
        out, _ = p.communicate(timeout=60)
        self.assertEqual(p.returncode, pw.STALL_EXIT, out)
        stall = [line for line in out.splitlines() if line.startswith('STALL ')]
        self.assertEqual(len(stall), 1, out)
        self.assertRegex(stall[0], r'^STALL \S+Z rank=0 phase=train count=3 step=3 gap=\d+ limit=2 rule=quiet$')
        self.assertRegex(out.split(stall[0], 1)[1], r'(most recent call first)[\s\S]*child\.py", line \d+')   # the rank's stack
        time.sleep(0.5)
        self.assertFalse(alive(int(self.mark.read_text())), 'the session-leading grandchild survived')

    def test_no_tick_at_all_ends_at_the_startup_limit(self):
        self.child.write_text('import time; time.sleep(60)')
        p = self.start('x', STALL_LIMITS='startup=1')
        out, _ = p.communicate(timeout=30)
        self.assertEqual(p.returncode, pw.STALL_EXIT, out)
        self.assertRegex(out, r'STALL \S+ rank=- phase=startup count=0 step=0 gap=\d+ limit=1 rule=quiet')

    def test_v08_settings_are_refused(self):
        p = self.start('ok', PHASE_BUDGETS='save=120')
        out, _ = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 2, out)
        self.assertIn('WATCH-ERROR PHASE_BUDGETS: v0.8 settings', out)
        self.assertNotIn('PROGRESS', out)                      # the launcher never started

    def test_a_hung_teardown_after_every_rank_exited_is_judged_as_final(self):
        p = self.start('ranks-gone', STALL_LIMITS='train=30,final=1.5')
        out, _ = p.communicate(timeout=60)
        self.assertEqual(p.returncode, pw.STALL_EXIT, out)
        self.assertRegex(out, r'STALL \S+ rank=- phase=final count=0 step=0 gap=\d+ limit=2 rule=quiet')

    def test_a_ceiling_ends_a_phase_that_keeps_ticking(self):
        p = self.start('endless-eval', STALL_LIMITS='train=30', STALL_CEILINGS='eval=1')
        out, _ = p.communicate(timeout=60)
        self.assertEqual(p.returncode, pw.STALL_EXIT, out)
        self.assertRegex(out, r'STALL \S+ rank=0 phase=eval count=\d+ step=3 gap=\d+ limit=1 rule=ceiling')

    def test_sigterm_is_passed_to_the_launcher(self):
        p = self.start('term')
        time.sleep(1.5)
        p.send_signal(signal.SIGTERM)
        out, _ = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 7, out)
        self.assertNotIn('STALL', out)

    def test_heartbeats_of_an_earlier_start_are_cleared(self):
        hb = self.dir / 'hb'
        hb.mkdir()
        (hb / 'rank-5.json').write_text(json.dumps(rank(rank=5, tick=0.0)))   # stale: would stall at once
        p = self.start('ok')
        out, _ = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 0, out)

    def test_a_rank_that_already_exited_is_not_a_stall(self):
        p = self.start('rank1-exits')
        out, _ = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 0, out)
        self.assertNotIn('STALL', out)

    def run_with_writeback(self, mode):
        # System writeback of 100 MiB per poll from outside the job; the job's own counters are flat.
        import contextlib, io
        class Busy:
            ok = True
            def __init__(self, *a): pass
            def moved(self, tree): return 0, 100 << 20
        original, saved = pw.Io, {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
        pw.Io = Busy
        os.environ.update(FAST, HEARTBEAT_DIR=str(self.dir / 'hb'), STALL_LIMITS='train=1.5,save=1.5')
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                code = pw.watch([sys.executable, str(self.child), mode])
        finally:
            pw.Io = original
            for s, h in saved.items():
                signal.signal(s, h)
            for key in (*FAST, 'HEARTBEAT_DIR'):
                os.environ.pop(key, None)
        return code, out.getvalue()

    def test_system_writeback_counts_only_during_a_flush(self):
        code, out = self.run_with_writeback('hang')               # a training hang: writeback elsewhere is ignored
        self.assertEqual(code, pw.STALL_EXIT, out)
        code, out = self.run_with_writeback('flush')              # a save waiting on its flush stays alive
        self.assertEqual(code, 0, out)
        self.assertNotIn('STALL', out)

    def test_a_watchdog_error_fails_open(self):
        import contextlib, io
        original, saved = pw.judge, {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
        pw.judge = lambda *a: 1 / 0
        os.environ.update(FAST, HEARTBEAT_DIR=str(self.dir / 'hb'))
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                code = pw.watch([sys.executable, '-c', 'import time; time.sleep(1)'])
        finally:
            pw.judge = original
            for s, h in saved.items():
                signal.signal(s, h)
            for key in (*FAST, 'HEARTBEAT_DIR'):
                os.environ.pop(key, None)
        self.assertEqual(code, 0)
        self.assertIn('WATCH-ERROR ZeroDivisionError', out.getvalue())

    def test_alive_and_malformed_records(self):
        self.assertTrue(pw.alive(os.getpid()))
        done = subprocess.run([sys.executable, '-c', 'import os; print(os.getpid())'], capture_output=True, text=True)
        self.assertFalse(pw.alive(int(done.stdout)))
        hb = self.dir / 'hb'; hb.mkdir()
        (hb / 'rank-0.json').write_text(json.dumps({'phase': 'train'}))         # no tick, pid: ignored
        (hb / 'rank-1.json').write_text(json.dumps(rank(rank=1)))
        self.assertEqual([r['rank'] for r in pw.read_ranks(hb)], [1])

    def test_usage(self):
        self.assertEqual(pw.main(['watch']), 2)
        self.assertEqual(pw.main(['rank', '--', 'x.py']), 2)                 # rank mode is torch_rank.py now


if __name__ == '__main__':
    unittest.main()
