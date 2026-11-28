#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -d "venv" ]; then
    echo "Creating Python virtual environment..."
    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt
else
    source venv/bin/activate
fi

PORT="${PORT:-8080}"
HOST="${HOST:-0.0.0.0}"

echo "=========================================================="
echo " Starting Telegram Resumable Media Uploader Web App"
echo " Web UI running at: http://localhost:$PORT"
echo "=========================================================="

python3 -m uvicorn app.main:app --host "$HOST" --port "$PORT" --reload
