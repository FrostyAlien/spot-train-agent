import sys as _sys
from pathlib import Path as _Path
SKILL = _Path(__file__).resolve().parents[2] / 'skills' / 'gpu-training-observe'   # the skill under test
_sys.path.insert(0, str(SKILL / 'scripts'))
import contextlib
import http.server
import io
import json
import os
import threading
import unittest
from unittest import mock

import notify


class Sink:
    """A local HTTP endpoint that records each POST (body, content type) and answers with `status`."""

    def __init__(self, status=200):
        self.posts, self.agents, outer = [], [], self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get('Content-Length', 0))
                outer.posts.append((self.path, self.headers.get('Content-Type'), self.rfile.read(n)))
                outer.agents.append(self.headers.get('User-Agent'))
                self.send_response(status)
                self.end_headers()

            def log_message(self, *a):
                pass
        self.server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.server.server_port}/topic-s3cret'

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class NotifyTests(unittest.TestCase):
    def setUp(self):
        self.sink = Sink()
        self.addCleanup(self.sink.close)

    def run_cli(self, args, **env):
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False), contextlib.redirect_stdout(out):
            code = notify.main(args)
        return code, out.getvalue()

    def test_text_format_posts_the_line_with_the_run_id(self):
        code, out = self.run_cli(['resumed', 'from', 'step', '12'], NOTIFY_URL=self.sink.url, NOTIFY_FORMAT='text',
                                 EXPERIMENT_RUN_ID='run-a')
        self.assertEqual((code, out), (0, ''))
        path, kind, body = self.sink.posts[0]
        self.assertEqual(path, '/topic-s3cret')
        self.assertEqual(self.sink.agents[0], notify.AGENT)
        self.assertTrue(kind.startswith('text/plain'))
        self.assertEqual(body.decode(), '[run-a] resumed from step 12')

    def test_json_formats(self):
        for fmt, key in (('slack', 'text'), ('discord', 'content'), ('json', 'text')):
            self.sink.posts.clear()
            sent, _ = notify.send('hello', event='eval', url=self.sink.url, fmt=fmt, run='r1')
            self.assertTrue(sent, fmt)
            payload = json.loads(self.sink.posts[0][2])
            self.assertEqual(payload[key], '[r1] hello')
            if fmt == 'json':
                self.assertEqual((payload['event'], payload['run']), ('eval', 'r1'))

    def test_no_url_is_a_warning_not_a_failure(self):
        env = {k: v for k, v in os.environ.items() if k != 'NOTIFY_URL'}
        with mock.patch.dict(os.environ, env, clear=True):
            code, out = self.run_cli(['x'])
        self.assertEqual(code, 0)
        self.assertIn('NOTIFY-WARN no NOTIFY_URL', out)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(self.run_cli(['--strict', 'x'])[0], 1)

    def test_a_failed_send_never_raises_and_never_prints_the_url(self):
        bad = Sink(status=500)
        self.addCleanup(bad.close)
        code, out = self.run_cli(['x'], NOTIFY_URL=bad.url)
        self.assertEqual(code, 0)
        self.assertIn('NOTIFY-WARN HTTP 500', out)
        dead = self.sink.url.replace(str(self.sink.server.server_port), '9')   # nothing listens on port 9
        code, out = self.run_cli(['x'], NOTIFY_URL=dead)
        self.assertEqual(code, 0)
        self.assertIn('NOTIFY-WARN', out)
        self.assertNotIn('s3cret', out)

    def test_a_crlf_url_or_a_broken_server_never_crashes_or_leaks(self):
        code, out = self.run_cli(['x'], NOTIFY_URL=self.sink.url + '\r')
        self.assertEqual(code, 0)
        self.assertIn('without spaces or control characters', out)
        self.assertNotIn('s3cret', out)
        import socket
        srv = socket.socket(); srv.bind(('127.0.0.1', 0)); srv.listen(1)
        self.addCleanup(srv.close)
        def answer():
            conn, _ = srv.accept(); conn.recv(65536); conn.sendall(b'NOT HTTP\r\n\r\n'); conn.close()
        threading.Thread(target=answer, daemon=True).start()
        code, out = self.run_cli(['x'], NOTIFY_URL=f'http://127.0.0.1:{srv.getsockname()[1]}/hook-s3cret')
        self.assertEqual(code, 0)
        self.assertIn('NOTIFY-WARN', out)
        self.assertNotIn('s3cret', out)

    def test_unknown_format_and_non_http_url_are_refused(self):
        self.assertEqual(notify.send('x', url=self.sink.url, fmt='sms')[0], False)
        self.assertEqual(notify.send('x', url='file:///etc/passwd', fmt='text'),
                         (False, 'NOTIFY_URL must be one http(s) URL without spaces or control characters (a CRLF file?)'))


if __name__ == '__main__':
    unittest.main()
