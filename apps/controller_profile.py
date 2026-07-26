"""
Shared controller profile for pipanel.

One place that knows how the dual-controller USB adapter enumerates and how its
raw button/axis/hat indices map to named inputs, so the menu, Pong, and the
input demo all agree. See memory: the adapter is TWO devices on the Raspberry
Pi (device 0 = Controller 1, device 1 = Controller 2) but ONE merged device on
macOS (Controller 2 offset into the upper index block).

Usage:
    pads = ControllerProfile()
    pads.refresh()                 # enumerate / rebuild bindings (cheap)
    st = pads.read(0)              # ControllerState for Controller 1
    if st.buttons["A"]: ...
    for evt in pads.poll_nav():    # {'up','down','left','right','select','back'}
        ...
"""

import pygame


PLAYERS = ["Controller 1", "Controller 2"]

# Per-controller layout in device order (confirmed on the Pi for Controller 1).
LAYOUT = {
    "buttons": {
        0: "Y", 1: "B", 2: "A", 3: "X",
        4: "L1", 5: "R1", 6: "L2", 7: "R2",
        8: "SELECT", 9: "START", 10: "L3", 11: "R3",
    },
    "axes": {"LX": 0, "LY": 1, "RX": 2, "RY": 3},
    "hat": 0,
}

# Offsets applied to Controller 2 only when both share ONE merged device.
MERGED_BUTTON_STRIDE = 12
MERGED_AXIS_STRIDE   = 4

HAT_DIRECTIONS = {
    (0, 1): "UP", (0, -1): "DOWN", (-1, 0): "LEFT", (1, 0): "RIGHT",
    (1, 1): "UP-RIGHT", (-1, 1): "UP-LEFT",
    (1, -1): "DOWN-RIGHT", (-1, -1): "DOWN-LEFT",
}

# Colour per controller so UIs can tell them apart consistently.
PLAYER_COLORS = {
    "Controller 1": (90, 200, 255),
    "Controller 2": (255, 150, 90),
}

AXIS_DEADZONE = 0.15
NAV_THRESHOLD = 0.5  # stick deflection treated as a discrete D-pad press


def axis_filter(v):
    return 0.0 if abs(v) < AXIS_DEADZONE else round(v, 2)


class ControllerState:
    """A single controller's mapped inputs at one moment."""

    __slots__ = ("name", "buttons", "axes", "hat")

    def __init__(self, name, buttons, axes, hat):
        self.name    = name
        self.buttons = buttons   # {"A": 0/1, ...}  (names absent on device -> 0)
        self.axes    = axes      # {"LX": float, ...}
        self.hat     = hat       # (x, y)


class ControllerProfile:
    """Enumerates the controllers and maps raw indices to named inputs.

    Device-count aware: 2+ devices -> one controller each; 1 device -> both
    controllers on it via the merged offsets.
    """

    def __init__(self):
        pygame.joystick.init()
        self.joys     = []
        self.bindings = []   # per player idx: {joy, btn_off, axis_off, hat}
        self._nav_latched = {}  # (idx, direction) -> bool, for edge detection
        self._prev_btn    = {}  # (idx, name) -> 0/1, for edge detection
        self._menu_latched = {}  # idx -> 0/1, for the SELECT-to-menu edge

    # ------------------------------------------------------------------
    def _build_bindings(self):
        self.bindings = []
        if not self.joys:
            return
        if len(self.joys) >= 2:
            for idx in range(2):
                self.bindings.append({
                    "joy": self.joys[idx],
                    "btn_off": 0, "axis_off": 0, "hat": LAYOUT["hat"],
                })
        else:
            j = self.joys[0]
            self.bindings.append({"joy": j, "btn_off": 0,
                                  "axis_off": 0, "hat": 0})
            self.bindings.append({"joy": j, "btn_off": MERGED_BUTTON_STRIDE,
                                  "axis_off": MERGED_AXIS_STRIDE, "hat": 1})

    def refresh(self):
        """Open all connected joysticks; rebuild bindings on count change.

        Returns the number of controllers available (0, 1, or 2)."""
        pygame.event.pump()
        count = pygame.joystick.get_count()
        if count == 0:
            if self.joys:
                self.joys = []
                self.bindings = []
                self._nav_latched.clear()
                self._prev_btn.clear()
                self._menu_latched.clear()
            return 0
        if len(self.joys) == count and self.bindings:
            return len(self.bindings)
        self.joys = []
        for i in range(count):
            try:
                j = pygame.joystick.Joystick(i)
                j.init()
                self.joys.append(j)
            except pygame.error:
                pass
        self._build_bindings()
        self._nav_latched.clear()
        self._prev_btn.clear()
        self._menu_latched.clear()
        return len(self.bindings)

    def count(self):
        return len(self.bindings)

    # ------------------------------------------------------------------
    def read(self, idx):
        """Return a ControllerState for player `idx` (mapped, named inputs).

        A missing device or disconnect yields an all-neutral state."""
        buttons = {name: 0 for name in LAYOUT["buttons"].values()}
        axes    = {name: 0.0 for name in LAYOUT["axes"]}
        hat     = (0, 0)
        if idx >= len(self.bindings):
            return ControllerState("", buttons, axes, hat)
        b   = self.bindings[idx]
        joy = b["joy"]
        try:
            nb, na, nh = (joy.get_numbuttons(), joy.get_numaxes(),
                          joy.get_numhats())
            for raw, name in LAYOUT["buttons"].items():
                r = raw + b["btn_off"]
                if r < nb:
                    buttons[name] = joy.get_button(r)
            for name, raw in LAYOUT["axes"].items():
                r = raw + b["axis_off"]
                if r < na:
                    axes[name] = axis_filter(joy.get_axis(r))
            if b["hat"] < nh:
                hat = joy.get_hat(b["hat"])
            name = joy.get_name()
        except pygame.error:
            name = ""
        return ControllerState(name, buttons, axes, hat)

    def read_raw(self):
        """Return an unmapped per-device snapshot for diagnostics.

        [{idx, name, counts:(nb,na,nh), buttons:[i,...],
          axes:[(i,val),...], hats:[(i,(x,y)),...]}]"""
        pygame.event.pump()
        devices = []
        for di, joy in enumerate(self.joys):
            try:
                nb, na, nh = (joy.get_numbuttons(), joy.get_numaxes(),
                              joy.get_numhats())
                buttons = [i for i in range(nb) if joy.get_button(i)]
                axes    = [(i, axis_filter(joy.get_axis(i))) for i in range(na)]
                axes    = [(i, v) for i, v in axes if v != 0]
                hats    = [(i, joy.get_hat(i)) for i in range(nh)]
                hats    = [(i, h) for i, h in hats if h != (0, 0)]
                devices.append({
                    "idx": di, "name": joy.get_name(),
                    "counts": (nb, na, nh),
                    "buttons": buttons, "axes": axes, "hats": hats,
                })
            except pygame.error:
                pass
        return devices

    # ------------------------------------------------------------------
    def poll_nav(self):
        """Aggregate edge-triggered navigation events across all controllers.

        Sticks are treated as discrete D-pad presses (must recentre before
        re-triggering). Returns a set drawn from:
            'up', 'down', 'left', 'right', 'select', 'back'
        A/START -> select, B -> back. Call once per frame."""
        self.refresh()
        events = set()
        for idx in range(self.count()):
            st = self.read(idx)
            hx, hy = st.hat
            lx = st.axes.get("LX", 0.0)
            ly = st.axes.get("LY", 0.0)
            directions = {
                "up":    hy == 1  or ly <= -NAV_THRESHOLD,
                "down":  hy == -1 or ly >= NAV_THRESHOLD,
                "left":  hx == -1 or lx <= -NAV_THRESHOLD,
                "right": hx == 1  or lx >= NAV_THRESHOLD,
            }
            for name, active in directions.items():
                key = (idx, name)
                if active and not self._nav_latched.get(key):
                    events.add(name)
                self._nav_latched[key] = active

            for btn, evt in (("A", "select"), ("START", "select"),
                             ("B", "back")):
                key = (idx, btn)
                cur = st.buttons.get(btn, 0)
                if cur and not self._prev_btn.get(key):
                    events.add(evt)
                self._prev_btn[key] = cur
        return events

    def menu_pressed(self):
        """Edge-triggered "return to menu" press from any controller.

        The dual adapter has no Home/Guide button, so SELECT is the menu
        button (on a standard Xbox pad that same physical button is Guide).
        Returns True once per press. Call once per frame.

        Assumes controllers are already enumerated this frame (e.g. after a
        read()/poll_nav()/refresh() call) so it can be used in a poll loop
        without forcing an extra refresh."""
        pressed = False
        for idx in range(self.count()):
            cur = self.read(idx).buttons.get("SELECT", 0)
            if cur and not self._menu_latched.get(idx):
                pressed = True
            self._menu_latched[idx] = cur
        return pressed
