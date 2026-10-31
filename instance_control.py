"""Private per-project server and installer coordination (stdlib only)."""
import fcntl
import http.client
import json
import os
from pathlib import Path
import time
import webbrowser

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / 'data' / 'runtime'


def lock(name, exclusive=True, blocking=False):
    RUNTIME.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = RUNTIME / name
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    os.fchmod(fd, 0o600)
    stream = os.fdopen(fd, 'a+')
    try:
        fcntl.flock(stream, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | (0 if blocking else fcntl.LOCK_NB))
    except BaseException:
        stream.close()
        raise
    return stream


def publish(port, token):
    path = RUNTIME / 'server.json'
    temporary = RUNTIME / ('server.%d.tmp' % os.getpid())
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump({'port': port, 'token': token, 'root': str(ROOT)}, stream)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def reopen(timeout=0, open_browser=True):
    deadline = time.monotonic() + timeout
    while True:
        try:
            data = json.loads((RUNTIME / 'server.json').read_text())
            port, token = data['port'], data['token']
            if data['root'] != str(ROOT) or not isinstance(port, int) or not 1 <= port <= 65535 or not isinstance(token, str):
                raise ValueError('invalid server record')
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=1)
            try:
                conn.request('GET', '/api/instance', headers={'Cookie': 'whiteflow=' + token})
                response = conn.getresponse()
                body = json.loads(response.read())
                if response.status != 200 or body != {'root': str(ROOT)}:
                    raise ValueError('not this instance')
            finally:
                conn.close()
            if open_browser:
                webbrowser.open('http://127.0.0.1:%d/?token=%s' % (port, token))
            return True
        except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
            if time.monotonic() >= deadline:
                return False
            time.sleep(.15)


if __name__ == '__main__':
    import subprocess
    import sys
    if sys.argv[1:] == ['--reopen']:
        try:
            probe = lock('server.lock')
        except BlockingIOError:
            sys.exit(0 if reopen(8) else 1)
        else:
            probe.close()
            sys.exit(1)
    if sys.argv[1:] == ['--launch']:
        # Serialise double-clicks until the first server has published its URL.
        with lock('launcher.lock', blocking=True):
            try:
                probe = lock('server.lock')
            except BlockingIOError:
                if reopen(8): sys.exit(0)
                sys.exit(1)
            else:
                probe.close()
            script = ROOT / 'CutCut.app/Contents/Resources/start.command'
            result = subprocess.run(['/usr/bin/open', '-a', 'Terminal', str(script)])
            if result.returncode: sys.exit(result.returncode)
            reopen(15, open_browser=False)
            sys.exit(0)
    sys.exit(2)
