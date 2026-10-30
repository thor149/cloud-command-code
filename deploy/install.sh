#!/usr/bin/env bash
# Install (or re-install) Cloud Command Code on Ubuntu 24.04. Safe to re-run.
# Run from the project checkout:  sudo bash deploy/install.sh
set -euo pipefail

APP_DIR=/opt/commandcode-bot
CONF_DIR=/etc/commandcode-bot
STATE_DIR=/var/lib/commandcode-bot
BOT_USER=ccbot
BOT_HOME=/home/$BOT_USER
SERVICE=commandcode-bot
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log() { printf '\n==> %s\n' "$*"; }

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo bash deploy/install.sh" >&2
  exit 1
fi

log "System packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q git python3 python3-venv python3-pip curl unzip ca-certificates rsync gnupg openssh-client

log "Node.js 22"
node_major=0
if command -v node >/dev/null 2>&1; then
  node_major="$(node -p 'process.versions.node.split(".")[0]')"
fi
if (( node_major < 22 )); then
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
  apt-get install -y -q nodejs
fi
node --version

log "Command Code CLI"
npm install -g --no-fund --no-audit command-code@latest >/dev/null
cmd_bin="$(command -v cmd || command -v commandcode || command -v cmdc || true)"
if [[ -z $cmd_bin ]]; then
  echo "Command Code was installed but no cmd/commandcode binary is on PATH." >&2
  exit 1
fi
echo "Command Code: $cmd_bin ($("$cmd_bin" --version 2>/dev/null || echo unknown version))"

log "Service user and directories"
if ! id "$BOT_USER" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "$BOT_HOME" --shell /bin/bash "$BOT_USER"
fi
install -d -m 750 -o "$BOT_USER" -g "$BOT_USER" "$BOT_HOME" "$BOT_HOME/repos" "$BOT_HOME/worktrees"
install -d -m 700 -o "$BOT_USER" -g "$BOT_USER" "$BOT_HOME/.ssh" "$STATE_DIR"
install -d -m 750 -o root -g "$BOT_USER" "$CONF_DIR"
# Global Command Code policy for the bot user. Deny rules here outrank any allow rule a repository
# ships and hold even in --yolo. The directory is root-owned, group-writable and sticky, so Command
# Code can keep its sessions there, but the agent (running as ccbot) cannot edit, replace or delete
# the root-owned settings.json.
install -d -m 3770 -o root -g "$BOT_USER" "$BOT_HOME/.commandcode"
install -m 644 -o root -g "$BOT_USER" "$SRC_DIR/deploy/commandcode-policy.json" "$BOT_HOME/.commandcode/settings.json"
runuser -u "$BOT_USER" -- git config --global user.name "Cloud Command Code"
runuser -u "$BOT_USER" -- git config --global user.email "ccbot@localhost"
runuser -u "$BOT_USER" -- git config --global init.defaultBranch main

log "Swap (small VMs)"
total_mb="$(awk '/MemTotal/ {print int($2 / 1024)}' /proc/meminfo)"
if (( total_mb < 2048 )) && [[ -z "$(swapon --show --noheadings)" ]]; then
  if ! fallocate -l 2G /swapfile 2>/dev/null; then
    dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none
  fi
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  echo "Added a 2 GB swap file (RAM is ${total_mb} MB)."
else
  echo "Swap not needed or already present."
fi

log "Application files"
install -d -m 755 "$APP_DIR"
if [[ "$SRC_DIR" != "$APP_DIR" ]]; then
  rsync -a --delete \
    --exclude '.venv/' --exclude '.git/' --exclude '.env' --exclude 'projects.json' \
    --exclude '__pycache__/' --exclude '.agents/' --exclude '*.zip' \
    "$SRC_DIR/" "$APP_DIR/"
fi
chown -R root:root "$APP_DIR"
chmod -R go-w "$APP_DIR"
if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
  python3 -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/python" -m pip install -q --upgrade pip
"$APP_DIR/.venv/bin/python" -m pip install -q -r "$APP_DIR/requirements.txt"

log "systemd unit"
install -m 644 "$APP_DIR/deploy/commandcode-bot.service" "/etc/systemd/system/$SERVICE.service"
systemctl daemon-reload

ready() {
  [[ -s "$CONF_DIR/env" ]] && grep -Eq '^TELEGRAM_BOT_TOKEN=.+' "$CONF_DIR/env" &&
    [[ -s "$CONF_DIR/projects.json" ]] &&
    python3 -c 'import json, sys; sys.exit(0 if json.load(open(sys.argv[1])) else 1)' "$CONF_DIR/projects.json"
}

# The unit is only enabled once it is configured, so a reboot before setup never leaves a failed unit.
if ready; then
  systemctl enable "$SERVICE" >/dev/null
  systemctl restart "$SERVICE"
  sleep 3
  systemctl --no-pager --lines=10 status "$SERVICE" || true
else
  systemctl disable "$SERVICE" >/dev/null 2>&1 || true
  cat <<EOF

Installed. Next steps (as root, from this directory):
  1. sudo bash deploy/configure.sh secrets          # bot token, your Telegram ID, Command Code API key
  2. sudo bash deploy/configure.sh add-repo <name> <git-url> [--writable] [--base main]
The service starts automatically once both are done.
EOF
fi
