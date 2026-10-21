#!/usr/bin/env python3
"""Rank mode for a PyTorch training script without hooks; stdlib only, vendored in the image at /opt/gpu-skills/.

The launcher starts this helper in each rank in place of the script. Any launcher that runs a Python script per
rank works: torchrun, accelerate launch, deepspeed, python -m torch.distributed.run, mpirun python.

    torchrun --nproc_per_node=N /opt/gpu-skills/torch_rank.py --step-hook -- train.py <args>
    torchrun --nproc_per_node=N /opt/gpu-skills/torch_rank.py --step-hook -- -m package.module <args>

It registers the stack dump on SIGUSR2 for the stall watchdog (progress_watch.py). With --step-hook it ticks
'train' after every optimizer step, from PyTorch's global optimizer step hook (PyTorch >= 2.0, every
torch.optim.Optimizer subclass). Then it runs the script as `python train.py` would, with no edit to it. Arguments
a launcher adds before `--` (`--local_rank=N`) are handed on to the script. The heartbeat is keyed by RANK, else
the pid. Ranks that a script spawns itself (torch.multiprocessing.spawn) do not run this helper.
"""
import faulthandler
import os
import runpy
import signal
import sys
from pathlib import Path

sys.path.insert(1, str(Path(__file__).resolve().parent))   # with PYTHONSAFEPATH=1 the helper's directory is absent
import progress_watch as pw      # noqa: E402 - the same module the script gets from `import progress_watch`


def step_hook():
    """Tick 'train' after every optimizer step of this process."""
    try:
        from torch.optim.optimizer import register_optimizer_step_post_hook
    except ImportError as e:
        print(f'WATCH-WARN --step-hook needs PyTorch >= 2.0 ({e}): no train ticks from this rank', file=sys.stderr, flush=True)
        return
    r = os.environ.get('RANK', '')
    key, steps = int(r) if r.isdigit() else os.getpid(), [0]

    def hook(_optimizer, _args, _kwargs):
        steps[0] += 1
        pw.tick('train', count=steps[0], step=steps[0], rank=key)
    register_optimizer_step_post_hook(hook)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if '--' not in argv:
        print(__doc__, file=sys.stderr)
        return 2
    j = argv.index('--')
    hook, inserted, target = '--step-hook' in argv[:j], [a for a in argv[:j] if a != '--step-hook'], argv[j + 1:]
    if not target or (target[0] == '-m' and len(target) < 2):
        print(__doc__, file=sys.stderr)
        return 2
    faulthandler.register(signal.SIGUSR2, all_threads=True)
    if hook:
        step_hook()
    # sys.path first gets what `python script.py` (the script's directory) or `python -m` (the working directory)
    # would put there; this helper's directory stays behind it, also for spawned children.
    if target[0] == '-m':
        sys.argv = [target[1], *inserted, *target[2:]]
        sys.path.insert(0, os.getcwd())
        runpy.run_module(target[1], run_name='__main__', alter_sys=True)
    else:
        sys.argv = [target[0], *inserted, *target[1:]]
        sys.path.insert(0, str(Path(target[0]).resolve().parent))
        runpy.run_path(target[0], run_name='__main__')
    return 0


if __name__ == '__main__':
    sys.exit(main())
