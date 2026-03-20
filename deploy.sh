#!/bin/bash
set -e

SERVICE_NAME="pipanel"
SERVICE_FILE="pipanel.service"
INSTALL_DIR="/etc/systemd/system"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "$EUID" -ne 0 ]; then
  echo "Please run as root (sudo ./deploy.sh)"
  exit 1
fi

echo "Copying $SERVICE_FILE to $INSTALL_DIR..."
cp "$SCRIPT_DIR/$SERVICE_FILE" "$INSTALL_DIR/$SERVICE_FILE"

echo "Setting permissions on service file..."
chmod 644 "$INSTALL_DIR/$SERVICE_FILE"
chown root:root "$INSTALL_DIR/$SERVICE_FILE"

echo "Reloading systemd daemon..."
systemctl daemon-reload

echo "Enabling $SERVICE_NAME to start on boot..."
systemctl enable "$SERVICE_NAME"

echo "Restarting $SERVICE_NAME..."
systemctl restart "$SERVICE_NAME"

echo "Status:"
systemctl status "$SERVICE_NAME" --no-pager
