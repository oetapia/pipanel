"""
Controller input demo — shows live which key is pressed and from which
controller, rendered to the pipanel framebuffer.

Two controllers share one USB adapter. How they enumerate is driver-dependent:
  * macOS (IOKit):   one merged joystick device with interleaved/blocked
                     indices — what controllers2.py assumed.
  * Raspberry Pi:    the adapter usually splits into TWO joystick devices
                     (Joystick(0), Joystick(1)), each with its own indices
                     starting at 0. controllers2.py only opened Joystick(0),
                     so it never saw the second controller on the Pi.

This app is device-count aware:
  * 2+ devices -> device 0 = Controller 1, device 1 = Controller 2, each using
                  the per-device LAYOUT below.
  * 1 device   -> single merged device; Controller 2 is offset into the upper
                  block of indices.

Press R to toggle a RAW view showing every device's true button/axis/hat
indices with no mapping applied.

Runs two ways:
  * As a pipanel app  -> ControllerDemoApp(P).run()  (renders to the panel)
  * Standalone        -> python apps/controller_demo.py  (uses 35panel profile)

Press any input to see it.  R toggles raw/mapped.  ESC or Q returns to menu.
"""

import json
import os
import time

import numpy as np
import pygame


PLAYERS = ["Controller 1", "Controller 2"]

# Per-controller input layout, in device order (confirmed on the Pi for
# Controller 1). Applied to each device, with offsets when a single merged
# device carries both controllers.
LAYOUT = {
    "buttons": {
        0: "Y", 1: "B", 2: "A", 3: "X",
        4: "L1", 5: "R1", 6: "L2", 7: "R2",
        8: "SELECT", 9: "START", 10: "L3", 11: "R3",
    },
    "axes": {"LX": 0, "LY": 1, "RX": 2, "RY": 3},
    "hat": 0,
}

# Offsets used only when both controllers live on ONE merged device.
MERGED_BUTTON_STRIDE = 12
MERGED_AXIS_STRIDE   = 4

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

        self.joys     = []   # opened pygame joystick objects
        self.bindings = []   # list of {name, joy, btn_off, axis_off, hat}
        self.raw_mode = False  # toggled with R; shows unmapped indices

        # Mapped-mode change tracking, keyed by (player, raw_index).
        self._last_buttons = {}
        self._last_hats    = {}
        self._last_axes    = {}

        self._last_event      = None  # (player, text)
        self._last_event_time = 0.0

        # Raw-mode change tracking, keyed by (device_index, raw_index).
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
    def _build_bindings(self):
        """Assign controllers to devices based on how many are connected."""
        self.bindings = []
        if not self.joys:
            return
        if len(self.joys) >= 2:
            # Separate device per controller (the Raspberry Pi case).
            for idx, name in enumerate(PLAYERS):
                if idx < len(self.joys):
                    self.bindings.append({
                        "name": name, "joy": self.joys[idx],
                        "btn_off": 0, "axis_off": 0, "hat": LAYOUT["hat"],
                    })
        else:
            # One merged device carries both controllers in stacked blocks.
            j = self.joys[0]
            self.bindings.append({
                "name": PLAYERS[0], "joy": j,
                "btn_off": 0, "axis_off": 0, "hat": 0,
            })
            self.bindings.append({
                "name": PLAYERS[1], "joy": j,
                "btn_off": MERGED_BUTTON_STRIDE,
                "axis_off": MERGED_AXIS_STRIDE, "hat": 1,
            })

    def _ensure_joysticks(self):
        """Open all connected joysticks; rebuild bindings on count change."""
        pygame.event.pump()
        count = pygame.joystick.get_count()
        if count == 0:
            if self.joys:
                self.joys = []
                self.bindings = []
                self._reset_state()
            return False
        if len(self.joys) == count and self.bindings:
            return True
        # (Re)initialise every device.
        self.joys = []
        for i in range(count):
            try:
                j = pygame.joystick.Joystick(i)
                j.init()
                self.joys.append(j)
            except pygame.error:
                pass
        if not self.joys:
            return False
        self._build_bindings()
        self._reset_state()
        return True

    def _record(self, player, text):
        self._last_event      = (player, text)
        self._last_event_time = time.monotonic()

    def _poll(self):
        """Read all bound devices, emit change events, return held state."""
        pygame.event.pump()
        held = {p: {"buttons": [], "axes": [], "dpad": None} for p in PLAYERS}

        for b in self.bindings:
            player = b["name"]
            joy    = b["joy"]
            nb = joy.get_numbuttons()
            na = joy.get_numaxes()
            nh = joy.get_numhats()

            # Buttons
            for local, name in LAYOUT["buttons"].items():
                raw = local + b["btn_off"]
                if raw >= nb:
                    continue
                state = joy.get_button(raw)
                key = (player, raw)
                if state:
                    held[player]["buttons"].append(name)
                if state != self._last_buttons.get(key, 0):
                    if state:
                        self._record(player, name)
                    self._last_buttons[key] = state

            # D-pad / hat
            hat_id = b["hat"]
            if hat_id < nh:
                hat = joy.get_hat(hat_id)
                key = (player, hat_id)
                if hat != (0, 0):
                    held[player]["dpad"] = HAT_DIRECTIONS.get(hat, str(hat))
                if hat != self._last_hats.get(key, (0, 0)):
                    if hat != (0, 0):
                        self._record(player, f"D-PAD {HAT_DIRECTIONS.get(hat, hat)}")
                    self._last_hats[key] = hat

            # Axes
            for name, local in LAYOUT["axes"].items():
                raw = local + b["axis_off"]
                if raw >= na:
                    continue
                value = _axis_filter(joy.get_axis(raw))
                key = (player, raw)
                if value != 0:
                    held[player]["axes"].append((name, value))
                if value != self._last_axes.get(key, 0):
                    if value != 0:
                        self._record(player, f"{name} {value:+.2f}")
                    self._last_axes[key] = value

        return held

    def _poll_raw(self):
        """Read every device by raw index, no mapping. Returns per-device
        state and records the latest raw change into self._raw_last_event."""
        pygame.event.pump()
        devices = []
        for di, joy in enumerate(self.joys):
            nb = joy.get_numbuttons()
            na = joy.get_numaxes()
            nh = joy.get_numhats()

            buttons = []
            for i in range(nb):
                s = joy.get_button(i)
                key = (di, i)
                if s:
                    buttons.append(i)
                if s != self._raw_last_buttons.get(key, 0):
                    if s:
                        self._raw_last_event = f"dev{di} button {i}"
                    self._raw_last_buttons[key] = s

            axes = []
            for i in range(na):
                v = _axis_filter(joy.get_axis(i))
                key = (di, i)
                if v != 0:
                    axes.append((i, v))
                if v != self._raw_last_axes.get(key, 0):
                    if v != 0:
                        self._raw_last_event = f"dev{di} axis {i} {v:+.2f}"
                    self._raw_last_axes[key] = v

            hats = []
            for i in range(nh):
                h = joy.get_hat(i)
                key = (di, i)
                if h != (0, 0):
                    hats.append((i, h))
                if h != self._raw_last_hats.get(key, (0, 0)):
                    if h != (0, 0):
                        self._raw_last_event = f"dev{di} hat {i} {h}"
                    self._raw_last_hats[key] = h

            devices.append({
                "idx": di,
                "name": joy.get_name(),
                "counts": (nb, na, nh),
                "buttons": buttons,
                "axes": axes,
                "hats": hats,
            })
        return devices

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
        for i, player in enumerate(PLAYERS):
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

    def _draw_raw(self, devices):
        S = self.screen
        m = self.margin
        S.fill(BLACK)

        self._t("RAW INPUT", m, self.title_y, YELLOW, self.fnt_title)
        pygame.draw.line(S, YELLOW, (m, self.divider_y),
                         (self.W - m, self.divider_y), 1)

        step = int(self.fnt_hint.get_linesize() * 1.05)
        y = self.divider_y + 6

        # Latest raw change, prominent.
        self._t("Last: " + (self._raw_last_event or "—"),
                m, y, WHITE, self.fnt_desc, max_w=self.W - 2 * m)
        y += int(self.row_h * 1.1)

        if not devices:
            self._t("No devices", m, y, LGREY, self.fnt_hint)
        for d in devices:
            nb, na, nh = d["counts"]
            self._t(f"dev{d['idx']} {d['name']}  ({nb}b {na}a {nh}h)",
                    m, y, CYAN, self.fnt_hint, max_w=self.W - 2 * m)
            y += step
            btns = " ".join(str(i) for i in d["buttons"]) or "—"
            self._t(f"  BTN {btns}", m, y, GREEN, self.fnt_hint,
                    max_w=self.W - 2 * m)
            y += step
            axes = "  ".join(f"{i}:{v:+.2f}" for i, v in d["axes"]) or "—"
            hats = "  ".join(f"{i}:{h}" for i, h in d["hats"]) or "—"
            self._t(f"  AX {axes}  HAT {hats}", m, y, GREEN, self.fnt_hint,
                    max_w=self.W - 2 * m)
            y += step + 2

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

                if not self._ensure_joysticks():
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
                    self.joys = []
                    self.bindings = []
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
