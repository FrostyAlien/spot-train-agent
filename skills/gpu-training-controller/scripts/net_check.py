#!/usr/bin/env python3
"""Start check on the operator's machine: will SkyPilot's SSH readiness probe work from this network?

  python3 net_check.py [--host github.com] [--port 22] [--samples 3]

SkyPilot (direct probe) opens a TCP connection to a new VM and waits about 1 s for the SSH banner. A local
VPN or proxy in TUN mode answers the TCP handshake itself and relays the banner later, so the probe times
out for 10 minutes although `ssh` works. This measures connect and banner times to any SSH server.
Verdicts: ok | proxied (connect < 20 ms but banner > 0.8 s: a local proxy relays SSH) | slow (banner > 0.8 s).
For proxied/slow: create the control host from the cloud side, or set `<cloud>.ssh_proxy_command: nc %h %p`
in ~/.sky/config.yaml and restart the local API server (`sky api stop; sky api start`) before launching;
a cluster that failed in INIT keeps its old config until `sky down`. Workers launched from the cloud
control host are unaffected.
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import time


def probe(host: str, port: int, timeout: float = 5.0) -> dict:
    t0 = time.monotonic()
    with socket.create_connection((host, port), timeout=timeout) as s:
        connect = time.monotonic() - t0
        s.settimeout(timeout)
        banner = s.recv(64)
        return {'connect_s': round(connect, 3), 'banner_s': round(time.monotonic() - t0, 3),
                'banner': banner.split(b'\r')[0].decode(errors='replace')}


def verdict(samples: list) -> str:
    connect = min(s['connect_s'] for s in samples)
    banner = min(s['banner_s'] for s in samples)
    if banner > 0.8 and connect < 0.02:
        return 'proxied'
    return 'slow' if banner > 0.8 else 'ok'


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--host', default='github.com')
    p.add_argument('--port', type=int, default=22)
    p.add_argument('--samples', type=int, default=3)
    args = p.parse_args(argv)
    try:
        samples = [probe(args.host, args.port) for _ in range(args.samples)]
    except OSError as e:
        print(json.dumps({'verdict': 'unreachable', 'error': f'{type(e).__name__}: {e}'}))
        return 1
    v = verdict(samples)
    print(json.dumps({'verdict': v, 'host': args.host, 'samples': samples}))
    return 0 if v == 'ok' else 2


if __name__ == '__main__':
    sys.exit(main())
