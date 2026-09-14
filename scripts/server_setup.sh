#!/usr/bin/env bash
# Панель на сервере (Ubuntu): HTTPS-вход (Caddy в Docker, сертификат Let's Encrypt) + пользователь
# только для туннеля. Идемпотентно — можно запускать повторно. Другие сервисы не трогает:
# занимает только порты 80/443, фаервол не меняет.
#
#   ssh root@SERVER 'bash -s' -- DOMAIN "ssh-ed25519 AAAA... shorts-tunnel" [PORT] < scripts/server_setup.sh
set -euo pipefail

DOMAIN="$1"
TUNNEL_KEY="$2"
PORT="${3:-8765}"
TUNNEL_USER="shortstunnel"

# 1. Пользователь только для туннеля: без оболочки, ключ умеет лишь открыть 127.0.0.1:PORT на сервере
id "$TUNNEL_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$TUNNEL_USER"
install -d -m 700 -o "$TUNNEL_USER" -g "$TUNNEL_USER" "/home/$TUNNEL_USER/.ssh"
echo "restrict,port-forwarding,permitlisten=\"127.0.0.1:${PORT}\" ${TUNNEL_KEY}" > "/home/$TUNNEL_USER/.ssh/authorized_keys"
chown "$TUNNEL_USER:$TUNNEL_USER" "/home/$TUNNEL_USER/.ssh/authorized_keys"
chmod 600 "/home/$TUNNEL_USER/.ssh/authorized_keys"

# 2. HTTPS-прокси: Caddy сам получает и продлевает сертификат
mkdir -p /opt/shorts-panel
cat > /opt/shorts-panel/Caddyfile <<EOF
${DOMAIN} {
	encode gzip
	reverse_proxy 127.0.0.1:${PORT}
	handle_errors {
		respond "Панель недоступна: компьютер с конвейером выключен или туннель не запущен." 502
	}
}
EOF
docker rm -f shorts-panel-caddy >/dev/null 2>&1 || true
docker run -d --name shorts-panel-caddy --restart unless-stopped --network host \
  -v /opt/shorts-panel/Caddyfile:/etc/caddy/Caddyfile:ro \
  -v shorts_caddy_data:/data -v shorts_caddy_config:/config \
  caddy:2-alpine >/dev/null

echo "Готово: https://${DOMAIN} → 127.0.0.1:${PORT} (туннель — пользователь ${TUNNEL_USER})"
