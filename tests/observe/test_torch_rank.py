import sys as _sys
from pathlib import Path as _Path
SKILL = _Path(__file__).resolve().parents[2] / 'skills' / 'gpu-training-observe'   # the skill under test
_sys.path.insert(0, str(SKILL / 'scripts'))
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = SKILL / 'scripts'

SCRIPT = '''import sys
import progress_watch, helper_mod
assert progress_watch is sys.modules['progress_watch'] and helper_mod.ok
print('ARGV', sys.argv[1:], progress_watch.__file__ == {pw!r})
class Item:                                                 # pickled to a spawned child by reference to __main__
    pass
if __name__ == '__main__':
    import multiprocessing as mp, pickle
    ctx = mp.get_context('spawn')
    with ctx.Pool(1) as pool:
        print('CHILD', pool.apply(len, (pickle.dumps(Item()),)) > 0)
    try:
        import torch.optim.optimizer as o
    except ImportError:
        sys.exit(5)
    import time
    opt = o.Optimizer()
    for i in range(3):                                      # growing gaps: each tick is written
        opt.step()
        time.sleep(0.15 * (i + 1))
    sys.exit(4)
'''
FAKE_TORCH = '''_hooks = []
def register_optimizer_step_post_hook(hook):
    _hooks.append(hook)
class Optimizer:
    def step(self):
        for h in _hooks:
            h(self, (), {{}})
'''


class RankModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        (self.dir / 'train.py').write_text(SCRIPT.format(pw=str(HERE / 'progress_watch.py')))
        (self.dir / 'helper_mod.py').write_text('ok = True\n')    # found next to the script, as with python train.py
        pkg = self.dir / 'fake' / 'torch' / 'optim'
        pkg.mkdir(parents=True)
        for d in (pkg.parent, pkg):
            (d / '__init__.py').write_text('')
        (pkg / 'optimizer.py').write_text(FAKE_TORCH.format())

    def run_rank(self, *argv, torch=True, **env):
        path = str(self.dir / 'fake') if torch else ''
        return subprocess.run([sys.executable, str(HERE / 'torch_rank.py'), *argv], capture_output=True, text=True,
                              cwd=self.dir, timeout=60,
                              env={**os.environ, 'PYTHONPATH': path, 'HEARTBEAT_DIR': str(self.dir / 'hb'), **env})

    def test_runs_the_script_as_python_would_with_launcher_arguments_handed_on(self):
        r = self.run_rank('--local_rank=1', '--', 'train.py', '--lr', '1', torch=False)
        self.assertEqual(r.returncode, 5, r.stderr)
        self.assertIn("ARGV ['--local_rank=1', '--lr', '1'] True", r.stdout)
        self.assertIn('CHILD True', r.stdout)
        self.assertFalse((self.dir / 'hb').exists())          # no --step-hook: no ticks

    def test_step_hook_ticks_train_keyed_by_rank_or_pid(self):
        r = self.run_rank('--step-hook', '--', 'train.py', RANK='3')
        self.assertEqual(r.returncode, 4, r.stderr)
        rec = json.loads((self.dir / 'hb' / 'rank-3.json').read_text())
        self.assertEqual((rec['phase'], rec['step'], rec['train_ticks']), ('train', 3, 3))
        env = {k: v for k, v in os.environ.items() if k != 'RANK'}
        r = subprocess.run([sys.executable, str(HERE / 'torch_rank.py'), '--step-hook', '--', 'train.py'],
                           capture_output=True, text=True, cwd=self.dir, timeout=60,
                           env={**env, 'PYTHONPATH': str(self.dir / 'fake'), 'HEARTBEAT_DIR': str(self.dir / 'hb2')})
        self.assertEqual(r.returncode, 4, r.stderr)
        files = [f.name for f in (self.dir / 'hb2').glob('rank-*.json')]
        self.assertEqual(len(files), 1)
        self.assertNotEqual(files, ['rank-0.json'])             # the pid, not a shared 0

    def test_a_module_target_and_a_missing_torch(self):
        r = self.run_rank('--step-hook', '--', '-m', 'train', '--x', torch=False)
        self.assertEqual(r.returncode, 5, r.stderr)
        self.assertIn("ARGV ['--x'] True", r.stdout)
        self.assertIn('WATCH-WARN --step-hook needs PyTorch', r.stderr)


    def test_the_helper_finds_progress_watch_under_pythonsafepath(self):
        r = self.run_rank('--', 'train.py', torch=False, PYTHONSAFEPATH='1')
        self.assertEqual(r.returncode, 5, r.stderr)                      # the script ran; no torch: its exit 5
        self.assertIn("ARGV [] True", r.stdout)                           # the helper's own progress_watch

    def test_usage(self):
        import torch_rank
        self.assertEqual(torch_rank.main(['train.py']), 2)                   # no --
        self.assertEqual(torch_rank.main(['--', '-m']), 2)


if __name__ == '__main__':
    unittest.main()
