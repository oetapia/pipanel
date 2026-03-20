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

echo "Reloading systemd daemon..."
systemctl daemon-reload

echo "Enabling services..."
systemctl enable pipanel-update.service
systemctl enable pipanel.service

echo "Restarting pipanel..."
systemctl restart pipanel.service

echo "Status:"
systemctl status pipanel.service --no-pager
