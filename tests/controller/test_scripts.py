import sys as _sys
from pathlib import Path as _Path
SKILL = _Path(__file__).resolve().parents[2] / 'skills' / 'gpu-training-controller'   # the skill under test
_sys.path.insert(0, str(SKILL / 'scripts'))
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

SCRIPTS = SKILL / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import net_check  # noqa: E402


def ssh_like_server(delay):
    srv = socket.socket()
    srv.bind(('127.0.0.1', 0))
    srv.listen(5)
    def serve():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            time.sleep(delay)
            conn.sendall(b'SSH-2.0-test\r\n')
            conn.close()
    threading.Thread(target=serve, daemon=True).start()
    return srv


class NetCheckTests(unittest.TestCase):
    def test_verdicts(self):
        self.assertEqual(net_check.verdict([{'connect_s': 0.005, 'banner_s': 1.2}]), 'proxied')
        self.assertEqual(net_check.verdict([{'connect_s': 0.3, 'banner_s': 1.2}]), 'slow')
        self.assertEqual(net_check.verdict([{'connect_s': 0.05, 'banner_s': 0.3}]), 'ok')

    def test_probe_measures_a_delayed_banner(self):
        srv = ssh_like_server(0.9)
        try:
            r = net_check.probe('127.0.0.1', srv.getsockname()[1])
        finally:
            srv.close()
        self.assertTrue(r['banner'].startswith('SSH-2.0'))
        self.assertGreater(r['banner_s'], 0.8)
        self.assertLess(r['connect_s'], 0.1)


class ShellScriptTests(unittest.TestCase):
    def test_stage_run_and_wait_keep_the_exit_code(self):
        with tempfile.TemporaryDirectory() as d:
            env = {**os.environ, 'STAGE_DIR': d}
            sh = str(SCRIPTS / 'stage.sh')
            subprocess.run(['bash', sh, 'run', 'job', '--', 'bash', '-c', 'echo out; exit 7'], env=env, check=True,
                           capture_output=True)
            w = subprocess.run(['bash', sh, 'wait', 'job', '1'], env=env, capture_output=True, text=True)
            self.assertEqual(w.returncode, 7)
            self.assertIn('out', w.stdout)
            bad = subprocess.run(['bash', sh, 'run', 'bad name', '--', 'true'], env=env, capture_output=True)
            self.assertEqual(bad.returncode, 1)

    def test_stage_stop_and_a_dead_stage_are_reported(self):
        with tempfile.TemporaryDirectory() as d:
            env = {**os.environ, 'STAGE_DIR': d}
            sh = str(SCRIPTS / 'stage.sh')
            subprocess.run(['bash', sh, 'run', 'slow', '--', 'sleep', '300'], env=env, check=True, capture_output=True)
            s = subprocess.run(['bash', sh, 'stop', 'slow'], env=env, capture_output=True, text=True)
            self.assertIn('stopped slow', s.stdout)
            w = subprocess.run(['bash', sh, 'wait', 'slow', '1'], env=env, capture_output=True, text=True)
            self.assertEqual(w.returncode, 143)
            # A stage whose process vanished without writing an exit code (killed from outside, host reboot).
            Path(d, 'gone.pid').write_text('999999')
            Path(d, 'gone.log').write_text('')
            st = subprocess.run(['bash', sh, 'status'], env=env, capture_output=True, text=True).stdout
            self.assertIn('gone: died (no exit code)', st)
            w = subprocess.run(['bash', sh, 'wait', 'gone', '1'], env=env, capture_output=True, text=True)
            self.assertEqual(w.returncode, 125)


if __name__ == '__main__':
    unittest.main()
