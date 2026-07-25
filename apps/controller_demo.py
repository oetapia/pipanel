"""
Controller input demo — shows live which key is pressed and from which
controller, rendered to the pipanel framebuffer.

Based on the input mapping in apps/controllers2.py: a single joystick device
exposes two logical controllers via interleaved button/axis/hat indices.

Runs two ways:
  * As a pipanel app  -> ControllerDemoApp(P).run()  (renders to the panel)
  * Standalone        -> python apps/controller_demo.py  (uses 35panel profile)

Press any button / move a stick / use the D-pad to see it on screen.
ESC or Q returns to the menu.
"""

import json
import os
import time

import numpy as np
import pygame


# Input mapping copied from apps/controllers2.py (that module runs on import,
# so it can't be reused directly). One device, two logical controllers.
CONTROLLERS = {
    "Controller 1": {
        "buttons": {
            1: "Y", 3: "B", 5: "A", 7: "X",
            9: "L1", 11: "R1", 13: "L2", 15: "R2",
            17: "SELECT", 19: "START", 21: "L3", 23: "R3",
        },
        "axes": {"LX": 1, "LY": 3, "RX": 7, "RY": 5},
        "hat": 1,
    },
    "Controller 2": {
        "buttons": {
            0: "Y", 2: "B", 4: "A", 6: "X",
            8: "L1", 10: "R1", 12: "L2", 14: "R2",
            16: "SELECT", 18: "START", 20: "L3", 22: "R3",
        },
        "axes": {"LX": 0, "LY": 2, "RX": 6, "RY": 4},
        "hat": 0,
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

        # Per-index previous state, matching controllers2.py's keying.
        self._last_buttons = {}
        self._last_hats    = {}
        self._last_axes    = {}

        self._last_event      = None  # (player, text)
        self._last_event_time = 0.0

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
        self._t("Press any input   ESC Back", m, self.H - self.foot_txt,
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
                    if event.type == pygame.KEYDOWN and event.key in (
                            pygame.K_ESCAPE, pygame.K_q):
                        return

                if not self._ensure_joystick():
                    self._draw_waiting()
                    clock.tick(10)
                    continue

                # A disconnected pad raises pygame.error mid-poll; recover.
                try:
                    held = self._poll()
                except pygame.error:
                    self.joy = None
                    self._last_buttons.clear()
                    self._last_hats.clear()
                    self._last_axes.clear()
                    self._draw_waiting()
                    clock.tick(10)
                    continue

                self._draw(held)
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
