#!/usr/bin/env bash
# Installs the mail bridge service that Command Pi's scripts use. Run from the gmail-mcp folder.
# It generates BRIDGE_TOKEN in .env if missing and prints it once so you can add it to /etc/commandpi.env.
set -euo pipefail
cd "$(dirname "$0")"
touch .env; chmod 600 .env
if ! grep -q "^BRIDGE_TOKEN=" .env; then
  echo "BRIDGE_TOKEN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')" >> .env
  echo "Created BRIDGE_TOKEN in .env."
fi
sudo tee /etc/systemd/system/gmail-bridge.service >/dev/null <<UNIT
[Unit]
Description=Gmail bridge for Command Pi (no send, no delete)
After=network-online.target

[Service]
Type=simple
User=$(id -un)
WorkingDirectory=$PWD
EnvironmentFile=$PWD/.env
ExecStart=$PWD/.venv/bin/python bridge.py
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable gmail-bridge >/dev/null
sudo systemctl restart gmail-bridge
sleep 2
sudo systemctl --no-pager --lines=5 status gmail-bridge || true
echo
echo "Add this line to /etc/commandpi.env (then restart commandpi):"
echo "COMMANDPI_MAIL_TOKEN=$(grep '^BRIDGE_TOKEN=' .env | cut -d= -f2-)"
