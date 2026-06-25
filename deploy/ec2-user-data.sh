#!/usr/bin/env bash
# EC2 user-data bootstrap — Amazon Linux 2023 / Ubuntu 22.04
# Run once on first boot to install, configure, and enable the bot service.
set -euo pipefail

REPO="https://github.com/trshipesdev/tothemoon"
INSTALL_DIR="/home/ubuntu/cryptobot"
SERVICE_FILE="/etc/systemd/system/cryptobot.service"

# ── 1. System packages ──────────────────────────────────────────
apt-get update -y
apt-get install -y python3 python3-pip python3-venv git

# ── 2. Clone repo ───────────────────────────────────────────────
if [ -d "$INSTALL_DIR/.git" ]; then
    git -C "$INSTALL_DIR" pull --ff-only
else
    git clone "$REPO" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"

# ── 3. Python venv + deps ───────────────────────────────────────
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
deactivate

# ── 4. State directory ──────────────────────────────────────────
mkdir -p "$INSTALL_DIR/data"
chown -R ubuntu:ubuntu "$INSTALL_DIR"

# ── 5. Systemd service ──────────────────────────────────────────
# ExecStart uses the venv python so all deps are available.
cat > "$SERVICE_FILE" <<'EOF'
[Unit]
Description=Crypto Bot (tothemoon)
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/ubuntu/cryptobot
EnvironmentFile=/home/ubuntu/cryptobot/.env
ExecStart=/home/ubuntu/cryptobot/.venv/bin/python /home/ubuntu/cryptobot/src/bot/bot_full.py
Restart=always
RestartSec=5
User=ubuntu
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/home/ubuntu/cryptobot/data
Environment=STATE_PATH=/home/ubuntu/cryptobot/data/state_default.json

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable cryptobot

# ── 6. Reminder ─────────────────────────────────────────────────
echo ""
echo "======================================================"
echo " Bootstrap complete."
echo " Create /home/ubuntu/cryptobot/.env before starting:"
echo "   cp .env.example .env && nano .env"
echo " Then: systemctl start cryptobot"
echo " Logs: journalctl -fu cryptobot"
echo "======================================================"
