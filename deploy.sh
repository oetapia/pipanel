#!/bin/bash
set -e

INSTALL_DIR="/etc/systemd/system"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "$EUID" -ne 0 ]; then
  echo "Please run as root (sudo ./deploy.sh)"
  exit 1
fi

for SERVICE_FILE in pipanel-update.service pipanel.service; do
  echo "Copying $SERVICE_FILE to $INSTALL_DIR..."
  cp "$SCRIPT_DIR/$SERVICE_FILE" "$INSTALL_DIR/$SERVICE_FILE"
  chmod 644 "$INSTALL_DIR/$SERVICE_FILE"
  chown root:root "$INSTALL_DIR/$SERVICE_FILE"
done

echo "Installing system dependencies..."
apt install -y python3-numpy python3-pygame python3-venv

echo "Setting up Python venv..."
PIPANEL_DIR="/home/pi/pipanel"
sudo -u pi python3 -m venv --system-site-packages "$PIPANEL_DIR/.venv"
sudo -u pi "$PIPANEL_DIR/.venv/bin/python3" -m ensurepip --upgrade
sudo -u pi "$PIPANEL_DIR/.venv/bin/python3" -m pip install python-socketio[client] requests

echo "Reloading systemd daemon..."
systemctl daemon-reload

echo "Enabling services..."
systemctl enable pipanel-update.service
systemctl enable pipanel.service

echo "Restarting pipanel..."
systemctl restart pipanel.service

echo "Configuring HDMI for 1080p..."
CONFIG="/boot/firmware/config.txt"
MARKER="# pipanel HDMI 1080p config"

if grep -q "$MARKER" "$CONFIG"; then
  echo "HDMI 1080p config already present in $CONFIG, skipping."
else
  cat >> "$CONFIG" <<EOF

$MARKER
hdmi_group=1
hdmi_mode=16
hdmi_drive=2
hdmi_force_hotplug=1
EOF
  echo "HDMI 1080p config added to $CONFIG."
fi

echo "Status:"
systemctl status pipanel.service --no-pager
