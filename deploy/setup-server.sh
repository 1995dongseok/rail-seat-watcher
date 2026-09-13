#!/usr/bin/env bash
# Ubuntu(Lightsail) 인스턴스에서 최초 1회 실행. 프로젝트가 /opt/rail-seat-watcher 에 있다고 가정.
#   sudo bash deploy/setup-server.sh rail.example.com
set -euo pipefail

DOMAIN="${1:-}"
APP_DIR=/opt/rail-seat-watcher
APP_USER=ubuntu

if [[ -z "$DOMAIN" ]]; then
  echo "사용법: sudo bash deploy/setup-server.sh <도메인 또는 1-2-3-4.sslip.io>"; exit 1
fi

apt-get update -y
apt-get install -y python3 python3-venv python3-pip curl debian-keyring debian-archive-keyring apt-transport-https

# Caddy (HTTPS 자동)
if ! command -v caddy >/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y && apt-get install -y caddy
fi

chown -R "$APP_USER":"$APP_USER" "$APP_DIR"
sudo -u "$APP_USER" bash -c "cd $APP_DIR && python3 -m venv venv && ./venv/bin/pip install -q --upgrade pip && ./venv/bin/pip install -q -r requirements.txt"

if [[ ! -f "$APP_DIR/.env" ]]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  echo ">>> $APP_DIR/.env 를 열어 TELEGRAM_BOT_TOKEN, INVITE_CODE 를 채우세요."
fi
chmod 600 "$APP_DIR/.env"

install -m 644 "$APP_DIR/deploy/rail-seat-watcher.service" /etc/systemd/system/rail-seat-watcher.service
sed "s/rail.example.com/$DOMAIN/" "$APP_DIR/deploy/Caddyfile" > /etc/caddy/Caddyfile

systemctl daemon-reload
systemctl enable --now rail-seat-watcher
systemctl restart caddy

echo "완료. https://$DOMAIN 으로 접속하세요. 상태: systemctl status rail-seat-watcher caddy"
