#!/bin/bash
set -e

INSTALL_DIR="/etc/systemd/system"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="/boot/firmware/config.txt"
SCREEN_CONF="$SCRIPT_DIR/screen.conf"

# Screen the panel boots into unless --screen says otherwise.
DEFAULT_SCREEN="waveshare144"

usage() {
  cat <<EOF
Usage: sudo ./deploy.sh [--screen NAME] [--reboot] [--list]

  --screen NAME   Screen profile to boot into (from profiles.json).
                  Default: $DEFAULT_SCREEN
  --reboot        Reboot when done (needed the first time SPI is enabled).
  --list          List the available screen profiles and exit.
EOF
}

# Profile names come from profiles.json so this script never drifts from it.
profile_names() {
  python3 -c 'import json,sys; print("\n".join(json.load(open(sys.argv[1])).keys()))' \
    "$SCRIPT_DIR/profiles.json"
}

SCREEN="$DEFAULT_SCREEN"
REBOOT=0
while [ $# -gt 0 ]; do
  case "$1" in
    --screen) SCREEN="$2"; shift 2 ;;
    --screen=*) SCREEN="${1#*=}"; shift ;;
    --reboot) REBOOT=1; shift ;;
    --list) profile_names; exit 0 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1"; usage; exit 1 ;;
  esac
done

if [ "$EUID" -ne 0 ]; then
  echo "Please run as root (sudo ./deploy.sh)"
  exit 1
fi

if ! profile_names | grep -qxF "$SCREEN"; then
  echo "Unknown screen '$SCREEN'. Available:"
  profile_names | sed 's/^/  /'
  exit 1
fi
echo "Installing pipanel for screen: $SCREEN"

# main.py reads this when no --screen is passed, so the service and a bare
# `python3 main.py` both come up on the installed screen. It's gitignored, so
# update.sh's `git reset --hard` can't wipe the selection.
echo "$SCREEN" > "$SCREEN_CONF"
echo "Recorded screen in $SCREEN_CONF."

for SERVICE_FILE in pipanel-update.service pipanel.service; do
  echo "Copying $SERVICE_FILE to $INSTALL_DIR..."
  cp "$SCRIPT_DIR/$SERVICE_FILE" "$INSTALL_DIR/$SERVICE_FILE"
  chmod 644 "$INSTALL_DIR/$SERVICE_FILE"
  chown root:root "$INSTALL_DIR/$SERVICE_FILE"
done

echo "Installing system dependencies (apt, no venv)..."
# ARMv6 (Pi 1B) has no prebuilt PyPI wheels, so pip would compile from source.
# Use the distro's armhf packages instead — prebuilt, low-memory, no compile.
# All are installed system-wide; the service runs /usr/bin/python3 as root.
apt update
apt install -y python3-numpy python3-pygame python3-requests \
               python3-websockets python3-socketio python3-websocket

# --- Per-screen setup ------------------------------------------------------
append_config() {
  # append_config <marker> <lines...>
  local marker="$1"; shift
  if grep -q "$marker" "$CONFIG"; then
    echo "$marker already present in $CONFIG, skipping."
  else
    printf '\n%s\n%s\n' "$marker" "$*" >> "$CONFIG"
    echo "$marker added to $CONFIG."
  fi
}

NEEDS_REBOOT=0
case "$SCREEN" in
  1080TV)
    echo "Configuring HDMI for 1080p..."
    append_config "# pipanel HDMI 1080p config" \
"hdmi_group=1
hdmi_mode=16
hdmi_drive=2
hdmi_force_hotplug=1"
    ;;
  waveshare144)
    # The Waveshare 1.44" LCD HAT is driven over SPI + GPIO by apps/display.py
    # — no framebuffer overlay, so SPI must be enabled and spidev/RPi.GPIO
    # present before the service can draw anything.
    echo "Installing Waveshare LCD HAT dependencies..."
    apt install -y python3-spidev python3-rpi.gpio
    echo "Enabling SPI..."
    append_config "# pipanel Waveshare 1.44\" LCD HAT (SPI)" "dtparam=spi=on"
    if [ ! -e /dev/spidev0.0 ]; then
      NEEDS_REBOOT=1
    fi
    ;;
  *)
    echo "No extra boot configuration needed for $SCREEN."
    ;;
esac

echo "Reloading systemd daemon..."
systemctl daemon-reload

# Enabled = the panel starts itself on every boot, on the screen recorded above.
echo "Enabling services..."
systemctl enable pipanel-update.service
systemctl enable pipanel.service

if [ "$NEEDS_REBOOT" = 1 ] && [ "$REBOOT" = 0 ]; then
  echo
  echo "SPI is not live yet (/dev/spidev0.0 missing), so pipanel cannot draw to"
  echo "the Waveshare panel until you reboot. Not starting it now; it will come"
  echo "up automatically after:  sudo reboot"
  echo "  (or re-run with --reboot to do that here)"
  exit 0
fi

echo "Restarting pipanel..."
systemctl restart pipanel.service

echo "Status:"
systemctl status pipanel.service --no-pager

if [ "$REBOOT" = 1 ]; then
  echo "Rebooting..."
  reboot
fi
