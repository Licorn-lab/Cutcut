#!/bin/bash
set -euo pipefail
APP_ROOT="$(cd "$(dirname "$0")" && pwd)"
printf '原生捏合测试模式：兼容性未实机确认。若退出，保留终端日志，再用正常启动.command。\n'
export WHITEBG_NATIVE_PINCH=1
exec /bin/bash "$APP_ROOT/启动.command"
