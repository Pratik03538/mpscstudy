#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/Pratik03538/mpscstudy.git"
APP_DIR="/opt/mpscstudy"
APP_USER="$(id -un)"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ "$EUID" -eq 0 ]]; then
  echo "Run this script as the default Ubuntu user, not with sudo."
  exit 1
fi

if [[ "$APP_USER" != "ubuntu" ]]; then
  echo "Expected the Oracle Ubuntu login user 'ubuntu'; detected '$APP_USER'."
  echo "Review the systemd User/Group in deploy/oracle before proceeding."
  exit 1
fi

sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
  git python3 python3-venv python3-pip nginx certbot python3-certbot-nginx

if [[ ! -d "$APP_DIR/.git" ]]; then
  sudo mkdir -p "$APP_DIR"
  sudo chown "$APP_USER:$APP_USER" "$APP_DIR"
  git clone --branch main --depth 1 "$REPO_URL" "$APP_DIR"
else
  git -C "$APP_DIR" pull --ff-only origin main
fi

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/python" -m pip install --upgrade pip
"$APP_DIR/.venv/bin/python" -m pip install -r "$APP_DIR/telegram_bot/requirements.txt"
"$APP_DIR/.venv/bin/python" -m pip install -r "$APP_DIR/telegram_bot/miniapp/requirements.txt"

mkdir -p "$APP_DIR/telegram_bot/database" "$APP_DIR/telegram_bot/data" /var/lib/mpsc-study
sudo chown -R "$APP_USER:$APP_USER" "$APP_DIR" /var/lib/mpsc-study

sudo install -m 0644 "$SCRIPT_DIR/mpsc-bot.service" /etc/systemd/system/mpsc-bot.service
sudo install -m 0644 "$SCRIPT_DIR/mpsc-miniapp.service" /etc/systemd/system/mpsc-miniapp.service

if [[ ! -f /etc/mpsc-study.env ]]; then
  sudo install -o root -g root -m 0600 "$SCRIPT_DIR/mpsc-study.env.example" /etc/mpsc-study.env
else
  sudo chown root:root /etc/mpsc-study.env
  sudo chmod 0600 /etc/mpsc-study.env
fi

sudo systemctl daemon-reload

echo
echo "Base installation complete."
echo "Next:"
echo "  1. Upload mpsc_subject_only.db to $APP_DIR/telegram_bot/database/"
echo "  2. Edit /etc/mpsc-study.env and set your real values."
echo "  3. Then enable/start with:"
echo "     sudo systemctl enable --now mpsc-bot mpsc-miniapp"
echo "  4. Check logs with:"
echo "     sudo journalctl -u mpsc-bot -f"
echo "     sudo journalctl -u mpsc-miniapp -f"
