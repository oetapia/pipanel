"""
Controller input demo — shows live which key is pressed and from which
controller, rendered to the pipanel framebuffer.

Device enumeration and the raw-index -> named-input mapping live in the shared
apps/controller_profile.py (two devices on the Raspberry Pi, one merged device
on macOS). This app just visualises what that profile reports.

Press R to toggle a RAW view showing every device's true button/axis/hat
indices with no mapping applied — handy for confirming indices on new hardware.

Runs two ways:
  * As a pipanel app  -> ControllerDemoApp(P).run()  (renders to the panel)
  * Standalone        -> python apps/controller_demo.py  (uses 35panel profile)

Press any input to see it.  R toggles raw/mapped.  ESC or Q returns to menu.
"""

import json
import os
import time

import pygame

if __package__:
    from .controller_profile import (
        ControllerProfile, PLAYERS, PLAYER_COLORS, HAT_DIRECTIONS)
    from .display import make_sink
else:
    from controller_profile import (
        ControllerProfile, PLAYERS, PLAYER_COLORS, HAT_DIRECTIONS)
    from display import make_sink


EVENT_HOLD = 1.5  # seconds the latest-event banner stays highlighted

BLACK  = (0,   0,   0)
WHITE  = (255, 255, 255)
YELLOW = (255, 220, 0)
GREEN  = (0,   200, 0)
CYAN   = (0,   200, 200)
GREY   = (80,  80,  80)
LGREY  = (150, 150, 150)


class ControllerDemoApp:
    """pipanel app: renders live controller input to the display sink."""

    def __init__(self, P, sink=None):
        M = P["main"]

        self.W    = P["screen"]["w"]
        self.H    = P["screen"]["h"]
        self.sink = sink or make_sink(P)

        os.environ["SDL_VIDEODRIVER"] = "offscreen"
        pygame.init()
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

        self.pads = ControllerProfile()
        self.raw_mode = False  # toggled with R; shows unmapped indices

        # Mapped-mode change tracking, keyed by (player, input-name).
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

    def _record(self, player, text):
        self._last_event      = (player, text)
        self._last_event_time = time.monotonic()

    # ------------------------------------------------------------------
    def _poll(self):
        """Read mapped state from the profile; emit change events + held set."""
        self.pads.refresh()
        held = {p: {"buttons": [], "axes": [], "dpad": None} for p in PLAYERS}

        for idx, player in enumerate(PLAYERS):
            if idx >= self.pads.count():
                continue
            st = self.pads.read(idx)

            for name, state in st.buttons.items():
                if state:
                    held[player]["buttons"].append(name)
                key = (player, name)
                if state != self._last_buttons.get(key, 0):
                    if state:
                        self._record(player, name)
                    self._last_buttons[key] = state

            hat = st.hat
            if hat != (0, 0):
                held[player]["dpad"] = HAT_DIRECTIONS.get(hat, str(hat))
            hkey = (player, "hat")
            if hat != self._last_hats.get(hkey, (0, 0)):
                if hat != (0, 0):
                    self._record(player, f"D-PAD {HAT_DIRECTIONS.get(hat, hat)}")
                self._last_hats[hkey] = hat

            for name, value in st.axes.items():
                if value != 0:
                    held[player]["axes"].append((name, value))
                key = (player, name)
                if value != self._last_axes.get(key, 0):
                    if value != 0:
                        self._record(player, f"{name} {value:+.2f}")
                    self._last_axes[key] = value

        return held

    def _poll_raw(self):
        """Read unmapped per-device state; record the latest raw change."""
        devices = self.pads.read_raw()
        present = {d["idx"] for d in devices}
        for d in devices:
            di = d["idx"]
            held_b = set(d["buttons"])
            for i in held_b:
                if not self._raw_last_buttons.get((di, i)):
                    self._raw_last_event = f"dev{di} button {i}"
            # remember full held set so releases don't re-fire
            for i in range(d["counts"][0]):
                self._raw_last_buttons[(di, i)] = 1 if i in held_b else 0

            active_ax = {i: v for i, v in d["axes"]}
            for i, v in active_ax.items():
                if self._raw_last_axes.get((di, i), 0) == 0:
                    self._raw_last_event = f"dev{di} axis {i} {v:+.2f}"
            for i in range(d["counts"][1]):
                self._raw_last_axes[(di, i)] = active_ax.get(i, 0)

            active_h = {i: h for i, h in d["hats"]}
            for i, h in active_h.items():
                if self._raw_last_hats.get((di, i), (0, 0)) == (0, 0):
                    self._raw_last_event = f"dev{di} hat {i} {h}"
            for i in range(d["counts"][2]):
                self._raw_last_hats[(di, i)] = active_h.get(i, (0, 0))
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
        self.sink.write(S)

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
        self._t("R Raw view   SELECT/ESC Back", m, self.H - self.foot_txt,
                CYAN, self.fnt_hint, max_w=self.W - 2 * m)

        self.sink.write(S)

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
        self._t("R Mapped view   SELECT/ESC Back", m, self.H - self.foot_txt,
                CYAN, self.fnt_hint, max_w=self.W - 2 * m)

        self.sink.write(S)

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

                if self.pads.refresh() == 0:
                    self._draw_waiting()
                    clock.tick(10)
                    continue

                # SELECT on either controller returns to the menu.
                if self.pads.menu_pressed():
                    return

                try:
                    if self.raw_mode:
                        self._draw_raw(self._poll_raw())
                    else:
                        self._draw(self._poll())
                except pygame.error:
                    # Disconnect mid-poll: reset and show the waiting screen.
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
