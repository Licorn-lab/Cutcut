#!/bin/bash
set -eu
APP_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_ROOT"
printf '批量白底 · Mac\n首次使用模型时，下载进度会显示在这个窗口。\n请在程序中停止处理并关闭窗口，避免直接关闭此终端。\n\n'
if [ ! -x "$APP_ROOT/.venv/bin/python" ]; then
  printf '请先运行 安装.command。\n'
  printf '按回车关闭…'; read -r _ || true
  exit 1
fi
if ! "$APP_ROOT/.venv/bin/python" -c 'import tkinter,rembg,PIL' >/dev/null 2>&1; then
  printf '运行环境不可用，文件夹可能被移动过。请重新运行 安装.command。\n'
  printf '按回车关闭…'; read -r _ || true
  exit 1
fi
"$APP_ROOT/.venv/bin/python" "$APP_ROOT/app.py" || {
  printf '\n启动遇到问题，请截图上方错误。\n'
  printf '按回车关闭…'; read -r _ || true
  exit 1
}
