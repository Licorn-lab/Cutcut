#!/bin/bash
set -euo pipefail
APP_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_ROOT"
if [ ! -x "$APP_ROOT/.venv/bin/python" ]; then
  printf '未找到原工具运行环境，请先运行安装.command。\n'
  read -r _ || true
  exit 1
fi
if "$APP_ROOT/.venv/bin/python" -m pip install --only-binary=:all: 'pyobjc-core==12.2.2' 'pyobjc-framework-Cocoa==12.2.2'; then
  printf '\nMac双指捏合组件已安装。重新启动工具后生效。\n'
else
  printf '\n组件未安装成功，请保留上方错误。现有功能仍可用，暂用⌘+滚动缩放。\n'
fi
printf '按回车关闭…'
read -r _ || true
