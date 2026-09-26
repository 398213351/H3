#!/usr/bin/env bash
# H3 Studio gateway — Linux/macOS 启动脚本
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q -r requirements.txt

echo "Starting H3 Studio gateway on http://${GATEWAY_HOST:-127.0.0.1}:${GATEWAY_PORT:-8787} ..."
exec python gateway.py
