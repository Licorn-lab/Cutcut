#!/bin/bash
set -euo pipefail
APP_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
PYTHON_RUNTIME="$APP_ROOT/.venv/bin/python"
cd "$APP_ROOT"
if [ ! -x "$PYTHON_RUNTIME" ]; then
  printf '未找到 Python 环境，请先打开“安装 CutCut”。\n'
  read -r -p '按回车关闭…' _
  exit 1
fi
export OMP_NUM_THREADS=2
export OPENBLAS_NUM_THREADS=2
export MKL_NUM_THREADS=2
export WHITEBG_DATA_HOME="$APP_ROOT/data/runtime"
"$PYTHON_RUNTIME" -u "$APP_ROOT/web_server.py"
