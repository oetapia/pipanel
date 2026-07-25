"""
Controller input demo — shows live which key is pressed and from which
controller, rendered to the pipanel framebuffer.

A single USB device merges two controllers. Button/axis enumeration order is
driver-dependent: the indices differ between macOS (IOKit) and the Raspberry
Pi (Linux evdev), which is the deployment target here — so CONTROLLERS below
holds the Pi's ordering. Press R to toggle a RAW view that shows the true
button/axis/hat indices with no mapping applied; use it to confirm the numbers
on the Pi and update CONTROLLERS if any input is mislabeled.

Runs two ways:
  * As a pipanel app  -> ControllerDemoApp(P).run()  (renders to the panel)
  * Standalone        -> python apps/controller_demo.py  (uses 35panel profile)

Press any button / move a stick / use the D-pad to see it on screen.
R toggles raw/mapped view.  ESC or Q returns to the menu.
"""

import json
import os
import time

import numpy as np
import pygame


# One physical USB device merges two controllers with NO overlapping inputs.
# On the Raspberry Pi (Linux evdev) they map to contiguous blocks of raw
# indices, NOT the interleaved (odd/even) layout controllers2.py used for macOS:
#   Controller 1 -> buttons 0-11, axes 0-3, hat 0
#   Controller 2 -> buttons 12-23, axes 4-7, hat 1
# Button/axis names are in device order (Y,B,A,X,...  LX,LY,RX,RY).
CONTROLLERS = {
    "Controller 1": {
        "buttons": {
            0: "Y", 1: "B", 2: "A", 3: "X",
            4: "L1", 5: "R1", 6: "L2", 7: "R2",
            8: "SELECT", 9: "START", 10: "L3", 11: "R3",
        },
        "axes": {"LX": 0, "LY": 1, "RX": 2, "RY": 3},
        "hat": 0,
    },
    "Controller 2": {
        "buttons": {
            12: "Y", 13: "B", 14: "A", 15: "X",
            16: "L1", 17: "R1", 18: "L2", 19: "R2",
            20: "SELECT", 21: "START", 22: "L3", 23: "R3",
        },
        "axes": {"LX": 4, "LY": 5, "RX": 6, "RY": 7},
        "hat": 1,
    },
}

HAT_DIRECTIONS = {
    (0, 1): "UP", (0, -1): "DOWN", (-1, 0): "LEFT", (1, 0): "RIGHT",
    (1, 1): "UP-RIGHT", (-1, 1): "UP-LEFT",
    (1, -1): "DOWN-RIGHT", (-1, -1): "DOWN-LEFT",
}

AXIS_DEADZONE = 0.15
EVENT_HOLD = 1.5  # seconds the latest-event banner stays highlighted

BLACK  = (0,   0,   0)
WHITE  = (255, 255, 255)
YELLOW = (255, 220, 0)
GREEN  = (0,   200, 0)
CYAN   = (0,   200, 200)
GREY   = (80,  80,  80)
LGREY  = (150, 150, 150)

# One accent colour per controller so it's obvious which one fired.
PLAYER_COLORS = {
    "Controller 1": (90, 200, 255),
    "Controller 2": (255, 150, 90),
}


def fb_write(surface, fb):
    raw = pygame.surfarray.array3d(surface).transpose(1, 0, 2)
    r = (raw[:, :, 0].astype(np.uint16) >> 3) << 11
    g = (raw[:, :, 1].astype(np.uint16) >> 2) << 5
    b =  raw[:, :, 2].astype(np.uint16) >> 3
    with open(fb, "wb") as f:
        f.write((r | g | b).astype(np.uint16).tobytes())


def _axis_filter(v):
    return 0.0 if abs(v) < AXIS_DEADZONE else round(v, 2)


class ControllerDemoApp:
    """pipanel app: renders live controller input to the framebuffer."""

    def __init__(self, P):
        M   = P["main"]
        sdl = P["sdl"]

        self.W  = P["screen"]["w"]
        self.H  = P["screen"]["h"]
        self.fb = sdl["fbdev"]

        os.environ["SDL_VIDEODRIVER"] = "offscreen"
        pygame.init()
        pygame.joystick.init()
        self.screen = pygame.display.set_mode((self.W, self.H))
        pygame.mouse.set_visible(False)

        self.fnt_title = pygame.font.SysFont(None, M["fonts"]["title"])
        self.fnt_name  = pygame.font.SysFont(None, M["fonts"]["name"])
        self.fnt_desc  = pygame.font.SysFont(None, M["fonts"]["desc"])
        self.fnt_hint  = pygame.font.SysFont(None, M["fonts"]["hint"])

        self.margin    = M["title_x"]
        self.title_y   = M["title_y"]
        self.divider_y = M["divider_y"]
        self.foot_line = M["hint_line_offset"]
        self.foot_txt  = M["hint_text_offset"]
        self.row_h     = int(self.fnt_desc.get_linesize() * 1.15)

        self.joy = None
        self.raw_mode = False  # toggled with R; shows unmapped indices

        # Per-index previous state, matching controllers2.py's keying.
        self._last_buttons = {}
        self._last_hats    = {}
        self._last_axes    = {}

        self._last_event      = None  # (player, text)
        self._last_event_time = 0.0

        # Raw-mode change tracking (keyed by raw index, no mapping).
        self._raw_last_buttons = {}
        self._raw_last_axes    = {}
        self._raw_last_hats    = {}
        self._raw_last_event   = None  # text of most recent raw change

    def _reset_state(self):
        self._last_buttons.clear()
        self._last_hats.clear()
        self._last_axes.clear()
        self._raw_last_buttons.clear()
        self._raw_last_axes.clear()
        self._raw_last_hats.clear()

    # ------------------------------------------------------------------
    def _ensure_joystick(self):
        if self.joy is not None:
            return True
        pygame.event.pump()
        if pygame.joystick.get_count() == 0:
            return False
        try:
            self.joy = pygame.joystick.Joystick(0)
            self.joy.init()
        except pygame.error:
            self.joy = None
            return False
        return True

    def _record(self, player, text):
        self._last_event      = (player, text)
        self._last_event_time = time.monotonic()

    def _poll(self):
        """Read the device, emit change events, return live-held state."""
        pygame.event.pump()
        held = {p: {"buttons": [], "axes": [], "dpad": None} for p in CONTROLLERS}

        num_buttons = self.joy.get_numbuttons()
        num_axes    = self.joy.get_numaxes()
        num_hats    = self.joy.get_numhats()

        for player, cfg in CONTROLLERS.items():
            # Buttons (edge-triggered events + live held set)
            for button, name in cfg["buttons"].items():
                if button >= num_buttons:
                    continue
                state = self.joy.get_button(button)
                if state:
                    held[player]["buttons"].append(name)
                if state != self._last_buttons.get(button, 0):
                    if state:
                        self._record(player, name)
                    self._last_buttons[button] = state

            # D-pad / hat
            hat_id = cfg["hat"]
            if hat_id < num_hats:
                hat = self.joy.get_hat(hat_id)
                if hat != (0, 0):
                    held[player]["dpad"] = HAT_DIRECTIONS.get(hat, str(hat))
                if hat != self._last_hats.get(hat_id, (0, 0)):
                    if hat != (0, 0):
                        self._record(player, f"D-PAD {HAT_DIRECTIONS.get(hat, hat)}")
                    self._last_hats[hat_id] = hat

            # Axes
            for name, axis in cfg["axes"].items():
                if axis >= num_axes:
                    continue
                value = _axis_filter(self.joy.get_axis(axis))
                if value != 0:
                    held[player]["axes"].append((name, value))
                if value != self._last_axes.get(axis, 0):
                    if value != 0:
                        self._record(player, f"{name} {value:+.2f}")
                    self._last_axes[axis] = value

        return held

    def _poll_raw(self):
        """Read every input by raw index, no mapping. Returns held state and
        records the latest raw change into self._raw_last_event."""
        pygame.event.pump()
        num_buttons = self.joy.get_numbuttons()
        num_axes    = self.joy.get_numaxes()
        num_hats    = self.joy.get_numhats()

        buttons = []
        for i in range(num_buttons):
            s = self.joy.get_button(i)
            if s:
                buttons.append(i)
            if s != self._raw_last_buttons.get(i, 0):
                if s:
                    self._raw_last_event = f"button {i}"
                self._raw_last_buttons[i] = s

        axes = []
        for i in range(num_axes):
            v = _axis_filter(self.joy.get_axis(i))
            if v != 0:
                axes.append((i, v))
            if v != self._raw_last_axes.get(i, 0):
                if v != 0:
                    self._raw_last_event = f"axis {i} {v:+.2f}"
                self._raw_last_axes[i] = v

        hats = []
        for i in range(num_hats):
            h = self.joy.get_hat(i)
            if h != (0, 0):
                hats.append((i, h))
            if h != self._raw_last_hats.get(i, (0, 0)):
                if h != (0, 0):
                    self._raw_last_event = f"hat {i} {h}"
                self._raw_last_hats[i] = h

        return {
            "name": self.joy.get_name(),
            "counts": (num_buttons, num_axes, num_hats),
            "buttons": buttons,
            "axes": axes,
            "hats": hats,
        }

    # ------------------------------------------------------------------
    def _t(self, txt, x, y, col, fnt=None, max_w=None):
        fnt = fnt or self.fnt_desc
        txt = str(txt)
        if max_w:
            while txt and fnt.size(txt)[0] > max_w:
                txt = txt[:-1]
        self.screen.blit(fnt.render(txt, True, col), (x, y))

    def _draw_waiting(self):
        S = self.screen
        S.fill(BLACK)
        self._t("CONTROLLER DEMO", self.margin, self.title_y, YELLOW, self.fnt_title)
        pygame.draw.line(S, YELLOW, (self.margin, self.divider_y),
                         (self.W - self.margin, self.divider_y), 1)
        self._t("Waiting for controller...", self.margin,
                self.divider_y + self.row_h * 2, LGREY, self.fnt_name)
        self._t("ESC to go back", self.margin, self.H - self.foot_txt,
                CYAN, self.fnt_hint)
        fb_write(S, self.fb)

    def _draw(self, held):
        S = self.screen
        m = self.margin
        S.fill(BLACK)

        self._t("CONTROLLER DEMO", m, self.title_y, YELLOW, self.fnt_title)
        pygame.draw.line(S, YELLOW, (m, self.divider_y),
                         (self.W - m, self.divider_y), 1)

        # Latest-event banner: which controller + which key, big.
        y = self.divider_y + self.row_h
        if self._last_event:
            player, text = self._last_event
            fresh = (time.monotonic() - self._last_event_time) < EVENT_HOLD
            accent = PLAYER_COLORS.get(player, WHITE)
            self._t(player, m, y, accent if fresh else GREY, self.fnt_desc)
            self._t(text, m, y + self.row_h,
                    WHITE if fresh else LGREY, self.fnt_title,
                    max_w=self.W - 2 * m)
        else:
            self._t("Press a button on either controller",
                    m, y, LGREY, self.fnt_name, max_w=self.W - 2 * m)

        # Two columns showing what each controller is holding right now.
        col_top = y + self.row_h * 3
        col_w   = (self.W - 2 * m) // 2
        for i, (player, cfg) in enumerate(CONTROLLERS.items()):
            cx = m + i * col_w
            accent = PLAYER_COLORS.get(player, WHITE)
            self._t(player, cx, col_top, accent, self.fnt_name, max_w=col_w - 8)

            lines = []
            btns = held[player]["buttons"]
            if btns:
                lines.append(("BTN", " ".join(btns)))
            if held[player]["dpad"]:
                lines.append(("DPAD", held[player]["dpad"]))
            for name, value in held[player]["axes"]:
                lines.append((name, f"{value:+.2f}"))

            ly = col_top + self.row_h
            if not lines:
                self._t("—", cx, ly, GREY, self.fnt_desc)
            for label, val in lines:
                self._t(f"{label}: {val}", cx, ly, GREEN,
                        self.fnt_desc, max_w=col_w - 8)
                ly += self.row_h

        pygame.draw.line(S, GREY, (0, self.H - self.foot_line),
                         (self.W, self.H - self.foot_line), 1)
        self._t("R Raw view   ESC Back", m, self.H - self.foot_txt,
                CYAN, self.fnt_hint, max_w=self.W - 2 * m)

        fb_write(S, self.fb)

    def _draw_raw(self, raw):
        S = self.screen
        m = self.margin
        S.fill(BLACK)

        self._t("RAW INPUT", m, self.title_y, YELLOW, self.fnt_title)
        pygame.draw.line(S, YELLOW, (m, self.divider_y),
                         (self.W - m, self.divider_y), 1)

        nb, na, nh = raw["counts"]
        y = self.divider_y + int(self.row_h * 0.4)
        self._t(f"{raw['name']}", m, y, LGREY, self.fnt_hint,
                max_w=self.W - 2 * m)
        y += self.row_h
        self._t(f"{nb} buttons   {na} axes   {nh} hats",
                m, y, GREY, self.fnt_hint, max_w=self.W - 2 * m)

        # Latest raw change, big.
        y += int(self.row_h * 1.3)
        self._t("Last:", m, y, LGREY, self.fnt_desc)
        self._t(self._raw_last_event or "—",
                m + self.fnt_desc.size("Last: ")[0], y,
                WHITE, self.fnt_desc, max_w=self.W - 2 * m)

        # Live held raw inputs.
        y += self.row_h
        btns = " ".join(str(i) for i in raw["buttons"]) or "—"
        self._t("BTN:", m, y, LGREY, self.fnt_desc)
        self._t(btns, m + self.fnt_desc.size("BTN: ")[0], y, GREEN,
                self.fnt_desc, max_w=self.W - 2 * m)

        y += self.row_h
        axes = "  ".join(f"{i}:{v:+.2f}" for i, v in raw["axes"]) or "—"
        self._t("AXIS:", m, y, LGREY, self.fnt_desc)
        self._t(axes, m + self.fnt_desc.size("AXIS: ")[0], y, GREEN,
                self.fnt_desc, max_w=self.W - 2 * m)

        y += self.row_h
        hats = "  ".join(f"{i}:{h}" for i, h in raw["hats"]) or "—"
        self._t("HAT:", m, y, LGREY, self.fnt_desc)
        self._t(hats, m + self.fnt_desc.size("HAT: ")[0], y, GREEN,
                self.fnt_desc, max_w=self.W - 2 * m)

        pygame.draw.line(S, GREY, (0, self.H - self.foot_line),
                         (self.W, self.H - self.foot_line), 1)
        self._t("R Mapped view   ESC Back", m, self.H - self.foot_txt,
                CYAN, self.fnt_hint, max_w=self.W - 2 * m)

        fb_write(S, self.fb)

    # ------------------------------------------------------------------
    def run(self):
        clock = pygame.time.Clock()
        try:
            while True:
                # Keyboard fallback exit (works when a console is attached).
                for event in pygame.event.get():
                    if event.type == pygame.QUIT:
                        return
                    if event.type == pygame.KEYDOWN:
                        if event.key in (pygame.K_ESCAPE, pygame.K_q):
                            return
                        if event.key == pygame.K_r:
                            self.raw_mode = not self.raw_mode

                if not self._ensure_joystick():
                    self._draw_waiting()
                    clock.tick(10)
                    continue

                # A disconnected pad raises pygame.error mid-poll; recover.
                try:
                    if self.raw_mode:
                        self._draw_raw(self._poll_raw())
                    else:
                        self._draw(self._poll())
                except pygame.error:
                    self.joy = None
                    self._reset_state()
                    self._draw_waiting()
                    clock.tick(10)
                    continue

                clock.tick(60)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    def _load_profile():
        _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(_root, "profiles.json")) as f:
            profiles = json.load(f)
        return profiles[os.environ.get("PIPANEL_PROFILE", "35panel")]

    try:
        ControllerDemoApp(_load_profile()).run()
    except KeyboardInterrupt:
        pass
    finally:
        pygame.quit()
