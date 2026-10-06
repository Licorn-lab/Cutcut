#!/bin/bash
set -euo pipefail
APP_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_ROOT"
printf '批量白底 · Mac\n首次使用模型时，下载进度会显示在这个窗口。\n请在程序中停止处理并关闭窗口，避免直接关闭此终端。\n\n'
if [ ! -x "$APP_ROOT/.venv/bin/python" ]; then
  printf '请先运行 安装.command。\n'
  printf '按回车关闭…'; read -r _ || true
  exit 1
fi
if ! "$APP_ROOT/.venv/bin/python" -c 'import tkinter,PIL' >/dev/null 2>&1; then
  printf 'Python / Tk / Pillow 不可用，下面显示具体错误：\n'
  "$APP_ROOT/.venv/bin/python" -c 'import tkinter,PIL' || true
  printf '按回车关闭…'; read -r _ || true
  exit 1
fi
"$APP_ROOT/.venv/bin/python" -u -X faulthandler "$APP_ROOT/app.py" || {
  printf '\n启动遇到问题，请截图上方错误。\n'
  printf '按回车关闭…'; read -r _ || true
  exit 1
}
