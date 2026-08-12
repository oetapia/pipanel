#!/bin/bash
set -e

INSTALL_DIR="/etc/systemd/system"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="/boot/firmware/config.txt"
ENV_FILE="$SCRIPT_DIR/.env"
LEGACY_SCREEN_CONF="$SCRIPT_DIR/screen.conf"

# Screen the panel boots into when this is a fresh install and --screen is not
# given. An existing .env (or a pre-.env screen.conf) wins over this, so
# re-running deploy.sh on a device never silently moves it to another display.
FALLBACK_SCREEN="waveshare144"

# Profile names come from profiles.json so this script never drifts from it.
profile_names() {
  python3 -c 'import json,sys; print("\n".join(json.load(open(sys.argv[1])).keys()))' \
    "$SCRIPT_DIR/profiles.json"
}

# PIPANEL_SCREEN as already configured on this device, if anything is.
current_screen() {
  if [ -f "$ENV_FILE" ]; then
    local name
    name="$(sed -n 's/^[[:space:]]*\(export[[:space:]]\{1,\}\)\{0,1\}PIPANEL_SCREEN[[:space:]]*=[[:space:]]*//p' \
              "$ENV_FILE" | tail -n 1 | tr -d "\"' " )"
    if [ -n "$name" ]; then echo "$name"; return; fi
  fi
  # Pre-.env installs recorded it here; migrate that value forward.
  if [ -f "$LEGACY_SCREEN_CONF" ]; then
    tr -d "\"' " < "$LEGACY_SCREEN_CONF" | head -n 1
  fi
}

# Set PIPANEL_SCREEN in .env, leaving any other keys (WEATHER_API_KEY, ...) and
# their order untouched. .env is gitignored, so update.sh's `git reset --hard`
# cannot wipe the selection.
write_env_screen() {
  local screen="$1" tmp
  tmp="$(mktemp)"
  if [ -f "$ENV_FILE" ]; then
    awk -v val="$screen" '
      /^[[:space:]]*(export[[:space:]]+)?PIPANEL_SCREEN[[:space:]]*=/ {
        if (!done) { print "PIPANEL_SCREEN=" val; done = 1 }
        next
      }
      { print }
      END { if (!done) print "PIPANEL_SCREEN=" val }
    ' "$ENV_FILE" > "$tmp"
  else
    printf '# pipanel device config — see .env.example.\nPIPANEL_SCREEN=%s\n' \
      "$screen" > "$tmp"
  fi
  cat "$tmp" > "$ENV_FILE"        # in place: keeps existing owner/permissions
  rm -f "$tmp"
  # New file would otherwise be root-only; match the repo so pi can edit it.
  chown --reference="$SCRIPT_DIR/profiles.json" "$ENV_FILE" 2>/dev/null || true
}

DEFAULT_SCREEN="$(current_screen)"
DEFAULT_SCREEN="${DEFAULT_SCREEN:-$FALLBACK_SCREEN}"

usage() {
  cat <<EOF
Usage: sudo ./deploy.sh [--screen NAME] [--reboot] [--list]

  --screen NAME   Screen profile to boot into (from profiles.json). Recorded as
                  PIPANEL_SCREEN in .env, which the panel reads at startup.
                  Default: $DEFAULT_SCREEN
  --reboot        Reboot when done (needed the first time SPI is enabled).
  --list          List the available screen profiles and exit.
EOF
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

# main.py (and the apps run standalone) read PIPANEL_SCREEN from .env, so the
# service and a bare `python3 main.py` both come up on the installed screen.
write_env_screen "$SCREEN"
echo "Recorded PIPANEL_SCREEN=$SCREEN in $ENV_FILE."
if [ -f "$LEGACY_SCREEN_CONF" ]; then
  echo "Note: $LEGACY_SCREEN_CONF is superseded by .env and can be deleted."
fi

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
