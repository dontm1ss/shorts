#!/usr/bin/env bash
# Панель онлайн (macOS): запустить локальную панель, если не запущена, и держать обратный
# SSH-туннель на сервер с переподключением. См. docs/panel-deploy.md.
#   bash scripts/panel_online.sh [SERVER] [USER] [PORT]
set -uo pipefail
cd "$(dirname "$0")/.."
SERVER="${1:-195.133.56.48}"
TUNNEL_USER="${2:-shortstunnel}"
PORT="${3:-8765}"
KEY=".ssh/shorts_tunnel"

[ -f "$KEY" ] || { echo "Нет ключа туннеля $KEY — см. docs/panel-deploy.md"; exit 1; }
chmod 600 "$KEY"

if ! nc -z 127.0.0.1 "$PORT" 2>/dev/null; then
    mkdir -p output/logs
    nohup venv/bin/python -m panel >> output/logs/panel.log 2>&1 &
    sleep 3
fi

while true; do
    echo "$(date +%H:%M:%S) туннель → $TUNNEL_USER@$SERVER (127.0.0.1:$PORT)"
    ssh -i "$KEY" -N -o BatchMode=yes -o ExitOnForwardFailure=yes \
        -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
        -R "127.0.0.1:${PORT}:127.0.0.1:${PORT}" "$TUNNEL_USER@$SERVER"
    echo "Туннель оборвался, переподключение через 10 с"
    sleep 10
done
