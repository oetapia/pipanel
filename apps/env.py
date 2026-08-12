"""
Device-local configuration for pipanel, read from a .env file.

Which screen a board drives is a property of the board, not of the code: one Pi
carries the Waveshare 1.44" LCD HAT, another the 3.5" SPI panel, another an HDMI
TV — all running this same repo. So the choice lives in a .env file at the
project root (copy .env.example), which is gitignored and therefore survives
update.sh's `git reset --hard`:

    PIPANEL_SCREEN=waveshare144

deploy.sh writes that line at install time (`sudo ./deploy.sh --screen NAME`).
Real environment variables take priority over the file, so a one-off run can
override the installed screen without editing anything:

    PIPANEL_SCREEN=1080TV python3 main.py
"""

import json
import os

ROOT          = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH      = os.path.join(ROOT, ".env")
PROFILES_PATH = os.path.join(ROOT, "profiles.json")

# Installs made before .env recorded the screen here instead. Still read as a
# fallback so a Pi that hasn't been re-deployed keeps the screen it was set up
# with; .env wins whenever both exist.
_LEGACY_SCREEN_CONF = os.path.join(ROOT, "screen.conf")


def load_env(path=ENV_PATH):
    """Load KEY=VALUE lines from .env into os.environ.

    Already-set environment variables win, so an explicit `FOO=bar python3 ...`
    is never clobbered by the file. Returns the parsed pairs. A missing .env is
    not an error — every value it holds has a default somewhere.
    """
    values = {}
    try:
        with open(path) as f:
            lines = f.readlines()
    except OSError:
        return values

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):          # tolerate shell-style lines
            key = key[len("export "):].strip()
        if not key:
            continue
        values[key] = val = val.strip().strip("'\"")
        os.environ.setdefault(key, val)
    return values


def load_profiles(path=PROFILES_PATH):
    """All screen profiles, keyed by name (see profiles.json)."""
    with open(path) as f:
        return json.load(f)


def screen_name(profile_names):
    """Which profile in profile_names this device should use.

    PIPANEL_SCREEN (environment or .env) first, then PIPANEL_PROFILE for the
    older per-app variable, then the legacy screen.conf, then the first profile.
    """
    load_env()
    name = (os.environ.get("PIPANEL_SCREEN")
            or os.environ.get("PIPANEL_PROFILE")
            or "").strip()

    if not name:
        try:
            with open(_LEGACY_SCREEN_CONF) as f:
                name = f.read().strip()
        except OSError:
            name = ""

    if name and name not in profile_names:
        print(f"Ignoring unknown screen {name!r}; using {profile_names[0]}. "
              f"Set PIPANEL_SCREEN in {ENV_PATH} to one of: "
              f"{', '.join(profile_names)}")
        name = ""
    return name or profile_names[0]


def load_profile():
    """(name, profile) for this device's screen — used by the standalone apps."""
    profiles = load_profiles()
    name     = screen_name(list(profiles.keys()))
    return name, profiles[name]
