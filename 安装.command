#!/bin/bash
set -eu
APP_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_ROOT"
pause_end() { printf '\n按回车关闭此窗口…'; read -r _ || true; }
fail() { printf '\n安装未完成：%s\n' "$1"; pause_end; exit 1; }
trap 'fail "请查看上方错误。修复网络或 Python 后，再次运行安装.command。"' ERR
printf '批量白底 · Mac 安装程序\n\n'
if [ "$(uname -s)" != "Darwin" ]; then fail "这个安装入口只适用于 Mac。"; fi
MAC_MAJOR="$(sw_vers -productVersion | cut -d. -f1)"
if [ "$MAC_MAJOR" -lt 13 ]; then fail "此版本需要 macOS 13 或更新系统。请把你的系统版本告诉我，以便制作兼容版。"; fi
PYTHON_BIN=""
for candidate in \
  /Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13 \
  /Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12 \
  /Library/Frameworks/Python.framework/Versions/3.11/bin/python3.11 \
  /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 \
  /usr/local/bin/python3.13 /usr/local/bin/python3.12 \
  python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c \
    'import sys,tkinter; assert (3,11) <= sys.version_info[:2] <= (3,13); assert tkinter.TkVersion >= 8.6' >/dev/null 2>&1; then
    PYTHON_BIN="$candidate"
    break
  fi
done
if [ -z "$PYTHON_BIN" ]; then
  printf '未找到带 Tk 图形组件的 Python 3.11–3.13。\n'
  printf '请到 https://www.python.org/downloads/macos/ 安装 Python 3.13 的 macOS 64-bit universal2 installer，安装后重新运行本文件。\n'
  printf '不要选 Python 3.14。无需安装 Homebrew。\n'
  pause_end
  exit 1
fi
printf '已找到：%s\n' "$PYTHON_BIN"
"$PYTHON_BIN" --version
printf '\n正在建立独立运行环境，首次安装需要联网…\n'
"$PYTHON_BIN" -m venv --clear "$APP_ROOT/.venv"
"$APP_ROOT/.venv/bin/python" -m pip install --upgrade pip
"$APP_ROOT/.venv/bin/python" -m pip install --only-binary=:all: -r "$APP_ROOT/requirements.txt"
printf '\n尝试安装可选 HEIC 组件…\n'
if ! "$APP_ROOT/.venv/bin/python" -m pip install --only-binary=:all: 'pillow-heif==0.18.0'; then
  printf '可选 HEIC 组件未安装。JPG/PNG 等仍可使用；HEIC 请先导出为 JPG。\n'
fi
"$APP_ROOT/.venv/bin/python" -c 'import rembg,onnxruntime,PIL,tkinter; print("核心组件检查通过")'
chmod u+x "$APP_ROOT/启动CutCut网页.command" "$APP_ROOT/安装.command"
printf '\n安装完成！今后双击 启动CutCut网页.command 即可。\n'
printf '首次处理会另外下载模型；下载完成后，同一模型可以离线使用。\n'
pause_end
