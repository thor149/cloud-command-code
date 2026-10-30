#!/usr/bin/env bash
# Update an installed bot from a newer copy of this project. Tests run against the new code
# before anything is replaced; on failure the running service is left untouched.
# Run from the new checkout:  sudo bash deploy/update.sh
set -euo pipefail

APP_DIR=/opt/commandcode-bot
SERVICE=commandcode-bot
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

[[ $EUID -eq 0 ]] || { echo "Run as root: sudo bash deploy/update.sh" >&2; exit 1; }
[[ -x $APP_DIR/.venv/bin/python ]] || { echo "Not installed yet; run deploy/install.sh" >&2; exit 1; }
[[ $SRC_DIR != "$APP_DIR" ]] || { echo "Run this from the new checkout, not from $APP_DIR." >&2; exit 1; }

echo "==> Installing requirements"
"$APP_DIR/.venv/bin/python" -m pip install -q -r "$SRC_DIR/requirements.txt"

echo "==> Testing the new code"
if ! (cd "$SRC_DIR" && "$APP_DIR/.venv/bin/python" -m unittest discover -s tests); then
  echo "Tests failed; the running service was not changed." >&2
  exit 1
fi

echo "==> Replacing $APP_DIR"
rsync -a --delete \
  --exclude '.venv/' --exclude '.git/' --exclude '.env' --exclude 'projects.json' \
  --exclude '__pycache__/' --exclude '.agents/' --exclude '*.zip' \
  "$SRC_DIR/" "$APP_DIR/"
chown -R root:root "$APP_DIR"
chmod -R go-w "$APP_DIR"
install -m 644 "$APP_DIR/deploy/commandcode-bot.service" "/etc/systemd/system/$SERVICE.service"
systemctl daemon-reload

echo "==> Restarting"
systemctl restart "$SERVICE"
sleep 3
systemctl --no-pager --lines=15 status "$SERVICE"
