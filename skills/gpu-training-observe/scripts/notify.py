#!/usr/bin/env python3
"""Send one notification line from the training worker; stdlib only, vendored in the image at /opt/gpu-skills/.

    python3 notify.py [--event EVENT] [--strict] MESSAGE...

The channel is a secret the launch supplies as environment variables (never in the task, the image or chat):
  NOTIFY_URL      the endpoint, for example an ntfy topic URL or a Slack or Discord incoming-webhook URL
  NOTIFY_FORMAT   text (default: the line as the request body, ntfy and similar), slack ({"text": ...}),
                  discord ({"content": ...}) or json ({"text", "event", "run"} for a webhook of your own)
The line is prefixed with the run id ($EXPERIMENT_RUN_ID). Best effort: one POST with a 10 s timeout; a missing
URL or a failed send prints one NOTIFY-WARN line and exits 0, so a notice never stops or slows training
(--strict exits 1 instead, for a setup check). The URL is never printed.

Every spend figure in a message is an upper bound at the on-demand price; the real cost is the provider's bill.
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

FORMATS = ('text', 'slack', 'discord', 'json')
TIMEOUT = 10.0
AGENT = 'spot-trainer-notify/1'    # some webhook fronts refuse urllib's default agent


def _payload(message: str, fmt: str, event: str, run: str) -> tuple:
    if fmt == 'slack':
        return json.dumps({'text': message}).encode(), 'application/json'
    if fmt == 'discord':
        return json.dumps({'content': message}).encode(), 'application/json'
    if fmt == 'json':
        return json.dumps({'text': message, 'event': event, 'run': run}).encode(), 'application/json'
    return message.encode(), 'text/plain; charset=utf-8'


def send(message: str, event: str = 'note', url: str = None, fmt: str = None, run: str = None,
         timeout: float = TIMEOUT) -> tuple:
    """POST one line. Returns (sent, reason); never raises for a network or HTTP failure."""
    url = os.environ.get('NOTIFY_URL', '') if url is None else url
    fmt = (os.environ.get('NOTIFY_FORMAT') or 'text') if fmt is None else fmt
    run = os.environ.get('EXPERIMENT_RUN_ID', '') if run is None else run
    if fmt not in FORMATS:
        return False, f'NOTIFY_FORMAT must be one of {", ".join(FORMATS)}'
    if not url:
        return False, 'no NOTIFY_URL: not sent'
    if not url.startswith(('https://', 'http://')) or re.search(r'[\s\x00-\x1f\x7f]', url):
        return False, 'NOTIFY_URL must be one http(s) URL without spaces or control characters (a CRLF file?)'
    line = f'[{run}] {message}' if run else message
    body, kind = _payload(line, fmt, event, run)
    try:
        req = urllib.request.Request(url, data=body, method='POST', headers={'Content-Type': kind, 'User-Agent': AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if 200 <= resp.status < 300:
                return True, 'sent'
            return False, f'HTTP {resp.status}'
    except urllib.error.HTTPError as e:
        return False, f'HTTP {e.code}'
    except Exception as e:  # noqa: BLE001 - a notice never stops training; the URL (a secret) is never echoed
        return False, type(e).__name__


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('message', nargs='+')
    ap.add_argument('--event', default='note', help='a short event name, e.g. started, resumed, eval, stall, stopped')
    ap.add_argument('--strict', action='store_true', help='exit 1 when the notice was not sent')
    args = ap.parse_args(argv)
    sent, reason = send(' '.join(args.message), args.event)
    if not sent:
        print(f'NOTIFY-WARN {reason}', flush=True)
        return 1 if args.strict else 0
    return 0


if __name__ == '__main__':
    sys.exit(main())
