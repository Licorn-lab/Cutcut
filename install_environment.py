"""Install at the final venv path, restoring the old environment on failure."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
from instance_control import ROOT, RUNTIME, lock

ENV = ROOT / '.venv'
BACKUP = ROOT / '.venv.cutcut-backup'
MARKER = RUNTIME / 'install-pending'
CHECK = 'import rembg, onnxruntime, PIL, tkinter; print("核心组件检查通过")'


def valid():
    python = ENV / 'bin/python'
    if not python.exists(): return False
    return subprocess.run([str(python), '-c', CHECK], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def recover():
    if not MARKER.exists(): return
    state = json.loads(MARKER.read_text())
    if BACKUP.exists():
        if ENV.exists(): shutil.rmtree(ENV)
        BACKUP.rename(ENV)
    elif not state['had_environment'] and ENV.exists():
        shutil.rmtree(ENV)
    MARKER.unlink()
    print('已恢复上次安装前的环境。', flush=True)


def interrupt(signum, frame):
    raise KeyboardInterrupt()


def main():
    try:
        guard = lock('install.lock')
    except BlockingIOError:
        print('CutCut 正在运行或安装，请先关闭运行终端再安装。', flush=True); return 1
    signal.signal(signal.SIGTERM, interrupt)
    try:
        recover()
        if valid():
            print('已有环境检查通过，无需重新安装。', flush=True); return 0
        if BACKUP.exists():
            print('发现旧环境备份，请先检查 .venv.cutcut-backup，未对其进行任何修改。', flush=True); return 1
        had = ENV.exists()
        temporary = RUNTIME / ('install-pending.%d.tmp' % os.getpid())
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump({'had_environment': had}, stream)
                stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, MARKER)
        finally:
            temporary.unlink(missing_ok=True)
        try:
            if had: ENV.rename(BACKUP)
            python = str(ENV / 'bin/python')
            subprocess.run([sys.executable, '-m', 'venv', str(ENV)], check=True)
            subprocess.run([python, '-m', 'pip', 'install', '--upgrade', 'pip'], check=True)
            subprocess.run([python, '-m', 'pip', 'install', '--only-binary=:all:', '-r', str(ROOT/'requirements.txt')], check=True)
            if subprocess.run([python, '-m', 'pip', 'install', '--only-binary=:all:', 'pillow-heif==0.18.0']).returncode:
                print('可选 HEIC 组件未安装，JPG/PNG 仍可使用。', flush=True)
            subprocess.run([python, '-c', CHECK], check=True)
            MARKER.unlink()
        except BaseException:
            recover()
            raise
        if BACKUP.exists(): shutil.rmtree(BACKUP)
        return 0
    finally:
        guard.close()


if __name__ == '__main__':
    try: sys.exit(main())
    except (OSError, ValueError, subprocess.CalledProcessError, KeyboardInterrupt) as exc:
        print('安装未完成：%s。请检查网络或 Python 后重试。' % exc, flush=True)
        sys.exit(1)
