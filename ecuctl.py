#!/usr/bin/env python3
"""Send one command to the collector's control socket and print the reply.

    python ecuctl.py STATUS
    python ecuctl.py NEW_SESSION
    python ecuctl.py STOP
    python ecuctl.py MOCK silent on      (mock mode only; also: noecho on|off, badsum N)

The socket is /run/ecu_hub/control.sock, or ./mock_data/control.sock if that
exists (mock mode), or the path given with --socket.
"""
import os
import socket
import sys


def send(path, command):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(3)
        s.connect(path)
        s.sendall((command + '\n').encode())
        return s.makefile('r').readline().strip()


def main():
    words = sys.argv[1:]
    path = None
    if words[:1] == ['--socket']:
        path, words = words[1], words[2:]
    if not words:
        raise SystemExit(__doc__)
    if path is None:
        mock = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'mock_data',
                            'control.sock')
        path = mock if os.path.exists(mock) else '/run/ecu_hub/control.sock'
    try:
        print(send(path, ' '.join(words)))
    except OSError as e:
        raise SystemExit(f'cannot reach the collector at {path}: {e}')


if __name__ == '__main__':
    main()
