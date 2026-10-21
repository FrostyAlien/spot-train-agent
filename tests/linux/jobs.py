"""Fake training jobs for the Linux watchdog tests (tests/linux/run.sh); stdlib only.

`python3 jobs.py SCENARIO` is the launcher: it starts two rank processes (`jobs.py SCENARIO --rank R`) and waits
for them, like torchrun. Ranks tick through progress_watch (PYTHONPATH) and do what the scenario needs.
"""
import os
import subprocess
import sys
import threading
import time

import progress_watch as pw

MB = 1 << 20


def ticks(phase, seconds, rank, every=0.5, start=0):
    n = start
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        n += 1
        pw.tick(phase, n, n, rank)
        time.sleep(every)
    return n


def write(path, mb, rate_mb_s=None, fsync=False):
    chunk = os.urandom(MB)
    with open(path, 'wb') as f:
        for _ in range(mb):
            f.write(chunk)
            if rate_mb_s:
                time.sleep(1 / rate_mb_s)
        if fsync:
            f.flush()
            os.fsync(f.fileno())


def rank_main(scenario, rank):
    step = ticks('train', 5, rank)
    if scenario == 't1':                 # rank 0 saves 300 MB at 5 MB/s (60 s) alone; rank 1 waits silently
        if rank == 0:
            pw.tick('save', 0, step, rank)
            write('/tmp/t1.ckpt', 300, rate_mb_s=5)
        else:
            time.sleep(62)
        ticks('train', 3, rank, start=step)
    elif scenario == 't2':               # a save writes 50 MB, then hangs
        pw.tick('save', 0, step, rank)
        if rank == 0:
            write('/tmp/t2.ckpt', 50)
            print(f'T2-LAST-WRITE {time.time():.0f}', flush=True)
        time.sleep(600)
    elif scenario == 't3':               # a training hang while a thread trickles 20 KB/s
        if rank == 0:
            def trickle():
                with open('/tmp/t3.log', 'wb') as f:
                    while True:
                        f.write(b'x' * 20000)
                        f.flush()
                        time.sleep(1)
            threading.Thread(target=trickle, daemon=True).start()
            print(f'T3-HANG {time.time():.0f}', flush=True)
        time.sleep(600)
    elif scenario == 't4':               # an async 300 MB writer (60 s) while the main thread hangs
        if rank == 0:
            def writer():
                write('/tmp/t4.ckpt', 300, rate_mb_s=5)
                print(f'T4-WRITER-DONE {time.time():.0f}', flush=True)
            threading.Thread(target=writer, daemon=True).start()
        time.sleep(600)
    elif scenario == 't5':               # an evaluation ticking every second for 90 s, then a hang in it
        pw.tick('eval', 0, step, rank)
        ticks('eval', 90, rank, every=1.0)
        print(f'T5-LAST-EVAL {time.time():.0f}', flush=True)
        time.sleep(600)
    elif scenario == 't6':               # fsync of 400 MB under a throttled disk (10 MB/s): the writer is blocked
        pw.tick('save', 0, step, rank)
        if rank == 0:
            t0 = time.time()
            write('/tmp/t6.ckpt', 400, fsync=True)
            print(f'T6-SAVE-SECONDS {time.time() - t0:.0f}', flush=True)
        else:
            time.sleep(1)
        ticks('train', 3, rank, start=step)


def main():
    scenario = sys.argv[1]
    if '--rank' in sys.argv:
        rank_main(scenario, int(sys.argv[sys.argv.index('--rank') + 1]))
        return 0
    procs = [subprocess.Popen([sys.executable, __file__, scenario, '--rank', str(r)]) for r in (0, 1)]
    return max(p.wait() for p in procs)


if __name__ == '__main__':
    sys.exit(main())
