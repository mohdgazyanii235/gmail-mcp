#!/usr/bin/env bash
# One-time setup on the Raspberry Pi: Python environment, secret, and a service that starts on boot.
# Run from inside the gmail-mcp folder:   ./setup.sh
set -euo pipefail
cd "$(dirname "$0")"

echo "== 1/5 Checking Python"
command -v python3 >/dev/null || { echo "python3 is missing. Run: sudo apt install -y python3 python3-venv"; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || { echo "Python 3.10 or newer is required. Use Raspberry Pi OS Bookworm or newer."; exit 1; }

echo "== 2/5 Creating the virtual environment and installing packages (a few minutes)"
python3 -m venv .venv || { echo "Run: sudo apt install -y python3-venv   then run ./setup.sh again"; exit 1; }
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -r requirements.txt -q
mkdir -p tokens
chmod 700 tokens

echo "== 3/5 Finding this Pi's Tailscale address"
command -v tailscale >/dev/null || { echo "Tailscale is not installed yet. Do the Tailscale step of the guide first."; exit 1; }
HOST="$(tailscale status --json | python3 -c 'import sys, json; print(json.load(sys.stdin).get("Self", {}).get("DNSName", "").rstrip("."))')"
if [ -z "$HOST" ]; then
  echo "Could not read the Tailscale name. Make sure 'sudo tailscale up' has completed and MagicDNS is enabled in the Tailscale admin console."
  exit 1
fi
echo "   $HOST"

echo "== 4/5 Writing the settings file"
if [ -f .env ]; then
  echo "   .env already exists, keeping it"
  grep -q '^PUBLIC_HOSTNAME=' .env || { echo "   .env has no PUBLIC_HOSTNAME line. Delete .env and run ./setup.sh again."; exit 1; }
else
  cat > .env <<EOF
PUBLIC_HOSTNAME=$HOST
PORT=8765
ALLOW_SEND=true
EOF
  chmod 600 .env
fi
PORT="$(grep '^PORT=' .env | cut -d= -f2)"

echo "== 5/5 Installing the background service (asks for your Pi password)"
sudo tee /etc/systemd/system/gmail-mcp.service >/dev/null <<EOF
[Unit]
Description=Gmail MCP server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$(id -un)
WorkingDirectory=$PWD
EnvironmentFile=$PWD/.env
ExecStart=$PWD/.venv/bin/uvicorn server:app --host 127.0.0.1 --port ${PORT} --no-access-log
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable gmail-mcp >/dev/null
sudo systemctl restart gmail-mcp
sleep 3
sudo systemctl --no-pager --lines=0 status gmail-mcp | head -5 || true

echo
echo "Setup finished. Next, choose your sign-in password and authenticator app:"
echo "   .venv/bin/python manage_auth.py setup"
