#!/usr/bin/env bash
# Installs the gmail-watcher background service. Run from the gmail-mcp folder after adding the WATCH_* lines to .env.
set -euo pipefail
cd "$(dirname "$0")"
.venv/bin/pip install -q -r requirements.txt
for v in WATCH_TOPIC WATCH_SUBSCRIPTION JARVIS_NOTIFY_TOKEN; do
  grep -q "^$v=" .env || { echo "Missing $v in .env (see watcher.py header)."; exit 1; }
done
[ -f pubsub-key.json ] || { echo "Missing pubsub-key.json here (copy or link the service account key)."; exit 1; }
chmod 600 pubsub-key.json
sudo tee /etc/systemd/system/gmail-watcher.service >/dev/null <<EOF
[Unit]
Description=Gmail new-mail watcher for Jarvis
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$(id -un)
WorkingDirectory=$PWD
EnvironmentFile=$PWD/.env
ExecStart=$PWD/.venv/bin/python watcher.py
Restart=always
RestartSec=10
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable gmail-watcher >/dev/null
sudo systemctl restart gmail-watcher
sleep 3
sudo systemctl --no-pager --lines=15 status gmail-watcher || true
