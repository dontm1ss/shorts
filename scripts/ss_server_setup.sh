#!/usr/bin/env bash
# Shadowsocks (shadowsocks-libev) setup on the VPS. TCP+UDP proxy, robust on Android.
# Prints a SIP002 ss:// URI for the phone client. Usage: ss_server_setup.sh <public_ip>
set -euo pipefail

PUBLIC_IP="${1:-}"
export DEBIAN_FRONTEND=noninteractive
apt-get install -y -qq shadowsocks-libev >/dev/null 2>&1

# Prefer 443 (looks like HTTPS, rarely blocked); fall back to 8388 if busy
PORT=8388
if ! ss -tlnp 2>/dev/null | grep -q ':443 '; then PORT=443; fi

PASSWORD=$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 24)
METHOD="chacha20-ietf-poly1305"

mkdir -p /etc/shadowsocks-libev
cat > /etc/shadowsocks-libev/config.json <<EOF
{
    "server": ["0.0.0.0"],
    "server_port": $PORT,
    "password": "$PASSWORD",
    "method": "$METHOD",
    "mode": "tcp_and_udp",
    "fast_open": false
}
EOF

systemctl enable shadowsocks-libev >/dev/null 2>&1 || true
systemctl restart shadowsocks-libev
sleep 1
systemctl is-active shadowsocks-libev >/dev/null || { echo "SERVICE FAILED"; systemctl status shadowsocks-libev --no-pager | tail -5; exit 1; }

# SIP002 ss:// URI (base64url of method:password)
USERINFO=$(printf '%s:%s' "$METHOD" "$PASSWORD" | base64 -w0 | tr '+/' '-_' | tr -d '=')
echo "PORT=$PORT"
echo "METHOD=$METHOD"
echo "PASSWORD=$PASSWORD"
echo "SS_URI=ss://${USERINFO}@${PUBLIC_IP}:${PORT}#VPS"
