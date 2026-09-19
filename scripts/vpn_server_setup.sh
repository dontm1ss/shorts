#!/usr/bin/env bash
# WireGuard server setup (Debian/Ubuntu), run as root on the VPS.
# Idempotent for server keys; generates a fresh client peer each run and prints its
# config (client) to stdout. Usage: vpn_server_setup.sh <public_ip_or_hostname>
set -euo pipefail

PUBLIC_IP="${1:-}"

export DEBIAN_FRONTEND=noninteractive
if ! command -v wg >/dev/null 2>&1; then
    apt-get update -qq
    apt-get install -y -qq wireguard iptables
fi

mkdir -p /etc/wireguard
chmod 700 /etc/wireguard

# Server keypair (reused on re-runs)
if [ ! -f /etc/wireguard/server.key ]; then
    wg genkey | tee /etc/wireguard/server.key | wg pubkey > /etc/wireguard/server.pub
fi
SERVER_PRIV=$(cat /etc/wireguard/server.key)
SERVER_PUB=$(cat /etc/wireguard/server.pub)

# Fresh client keypair each run
CLIENT_PRIV=$(wg genkey)
CLIENT_PUB=$(printf '%s' "$CLIENT_PRIV" | wg pubkey)

# Egress interface for NAT
IFACE=$(ip route | awk '/default/ {print $5; exit}')
IFACE=${IFACE:-ens3}

cat > /etc/wireguard/wg0.conf <<EOF
[Interface]
Address = 10.66.66.1/24
ListenPort = 51820
PrivateKey = $SERVER_PRIV
PostUp = iptables -t nat -A POSTROUTING -s 10.66.66.0/24 -o $IFACE -j MASQUERADE
PostDown = iptables -t nat -D POSTROUTING -s 10.66.66.0/24 -o $IFACE -j MASQUERADE

[Peer]
PublicKey = $CLIENT_PUB
AllowedIPs = 10.66.66.2/32
EOF
chmod 600 /etc/wireguard/wg0.conf

# IP forwarding (persistent)
sysctl -w net.ipv4.ip_forward=1 >/dev/null
grep -q '^net.ipv4.ip_forward=1' /etc/sysctl.conf || echo 'net.ipv4.ip_forward=1' >> /etc/sysctl.conf

systemctl enable wg-quick@wg0 >/dev/null 2>&1 || true
systemctl restart wg-quick@wg0

if [ -z "$PUBLIC_IP" ]; then
    PUBLIC_IP=$(curl -s -4 -m 10 ifconfig.me || true)
fi

# Client config for the phone (IPv4 only: no IPv6 inside the tunnel; Google DNS — стабильнее
# 1.1.1.1 на части сетей; keepalive держит NAT-маппинг живым)
cat <<EOF
[Interface]
PrivateKey = $CLIENT_PRIV
Address = 10.66.66.2/32
DNS = 8.8.8.8, 8.8.4.4
MTU = 1420

[Peer]
PublicKey = $SERVER_PUB
Endpoint = $PUBLIC_IP:51820
AllowedIPs = 0.0.0.0/0
PersistentKeepalive = 25
EOF
