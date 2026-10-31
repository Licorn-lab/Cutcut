#!/bin/bash
# 兼容旧启动入口；当前产品名称为 CutCut。
set -euo pipefail
APP_ROOT="$(cd "$(dirname "$0")" && pwd)"
exec "$APP_ROOT/启动CutCut网页.command" "$@"
