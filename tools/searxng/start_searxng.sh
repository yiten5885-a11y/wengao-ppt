#!/usr/bin/env bash
# 一键启动 SearXNG 本地兼容服务
set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
ROOT_DIR="$( cd "$DIR/../.." && pwd )"

PORT=8890
PID_FILE="$DIR/searxng_service.pid"
LOG_FILE="$DIR/searxng_service.log"

if lsof -Pi :$PORT -sTCP:LISTEN -t >/dev/null ; then
    echo "[SearXNG] 端口 $PORT 已有进程监听中。"
    exit 0
fi

echo "[SearXNG] 正在启动 127.0.0.1:$PORT 服务..."
export PYTHONPATH="$ROOT_DIR"
nohup /Users/huangyingteng/.local/bin/uv run python "$DIR/searxng_service.py" $PORT > "$LOG_FILE" 2>&1 &
PID=$!
echo $PID > "$PID_FILE"
sleep 1

if lsof -Pi :$PORT -sTCP:LISTEN -t >/dev/null ; then
    echo "[SearXNG] 启动成功，PID: $PID，监听端口: $PORT"
else
    echo "[SearXNG] 启动异常，查看日志: $LOG_FILE"
    exit 1
fi
