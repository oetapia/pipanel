"""
Shared controller profile for pipanel.

One place that knows how the pads enumerate and how their raw button/axis/hat
indices map to named inputs, so the menu, Pong, the Picar controller and the
input demo all agree.

Handles two different pads, picked per device by capability (see layout_for):
  * the dual-controller USB adapter — 12 buttons / 4 axes per controller, no
    analog triggers (L2/R2 are buttons). Per memory, it is TWO devices on the
    Raspberry Pi (device 0 = Controller 1, device 1 = Controller 2) but ONE
    merged device on macOS, with Controller 2 offset into the upper index block.
  * a genuine Xbox-style pad — 11 buttons / 6 axes, analog LT/RT.

Read state.triggers rather than the raw axes if you want throttle that works on
both: it is 0.0..1.0 either way.

Usage:
    pads = ControllerProfile()
    pads.refresh()                 # enumerate / rebuild bindings (cheap)
    st = pads.read(0)              # ControllerState for Controller 1
    if st.buttons["A"]: ...
    throttle = st.triggers["RT"]   # 0.0..1.0 on any supported pad
    for evt in pads.poll_nav():    # {'up','down','left','right','select','back'}
        ...
"""

import pygame


PLAYERS = ["Controller 1", "Controller 2"]

# Two physically different pads turn up in this project and they do NOT share an
# index map, so the profile keeps one layout per pad and picks by capability
# rather than making every call site hardcode indices.
#
# Per-controller layout in device order (confirmed on the Pi for Controller 1).
# The dual adapter has no analog triggers — it reports them as the L2/R2
# buttons — and exposes 12 buttons / 4 axes per controller.
ADAPTER_LAYOUT = {
    "buttons": {
        0: "Y", 1: "B", 2: "A", 3: "X",
        4: "L1", 5: "R1", 6: "L2", 7: "R2",
        8: "SELECT", 9: "START", 10: "L3", 11: "R3",
    },
    "axes": {"LX": 0, "LY": 1, "RX": 2, "RY": 3},
    "hat": 0,
    "analog_triggers": False,
}

# A genuine Xbox-style pad under SDL2: 6 axes with the triggers as axes 2 (LT)
# and 5 (RT), and a Guide/Home button the adapter doesn't have.
XBOX_LAYOUT = {
    "buttons": {
        0: "A", 1: "B", 2: "X", 3: "Y",
        4: "L1", 5: "R1", 6: "SELECT", 7: "START",
        8: "HOME", 9: "L3", 10: "R3",
    },
    "axes": {"LX": 0, "LY": 1, "LT": 2, "RX": 3, "RY": 4, "RT": 5},
    "hat": 0,
    "analog_triggers": True,
}

# Kept for callers that just want the adapter map by its old name.
LAYOUT = ADAPTER_LAYOUT

# Offsets applied to Controller 2 only when both share ONE merged device.
MERGED_BUTTON_STRIDE = 12
MERGED_AXIS_STRIDE   = 4


def layout_for(joy):
    """Pick a layout from what the device reports.

    Axis count is the reliable discriminator: 6+ axes means the pad has analog
    triggers, i.e. a real Xbox-style controller rather than the dual adapter."""
    try:
        return XBOX_LAYOUT if joy.get_numaxes() >= 6 else ADAPTER_LAYOUT
    except pygame.error:
        return ADAPTER_LAYOUT

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
TRIGGER_DEADZONE = 0.05  # analog trigger travel ignored as rest-position noise


def axis_filter(v):
    return 0.0 if abs(v) < AXIS_DEADZONE else round(v, 2)


def trigger_filter(v):
    """Map a raw analog trigger axis to 0.0..1.0.

    SDL reports an untouched trigger at -1.0, so the rest position has to be
    rescaled rather than deadzoned. Small values are clamped away because a
    resting trigger still jitters a unit or two."""
    v = (v + 1.0) / 2.0
    return 0.0 if v < TRIGGER_DEADZONE else round(min(1.0, v), 3)


class ControllerState:
    """A single controller's mapped inputs at one moment."""

    __slots__ = ("name", "buttons", "axes", "hat", "triggers")

    def __init__(self, name, buttons, axes, hat, triggers=None):
        self.name    = name
        self.buttons = buttons   # {"A": 0/1, ...}  (names absent on device -> 0)
        self.axes    = axes      # {"LX": float, ...}
        self.hat     = hat       # (x, y)
        # {"LT": 0.0..1.0, "RT": 0.0..1.0} — analog where the pad has analog
        # triggers, else 0.0/1.0 off the L2/R2 buttons, so callers can treat
        # both pads identically.
        self.triggers = triggers or {"LT": 0.0, "RT": 0.0}


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
                joy = self.joys[idx]
                lay = layout_for(joy)
                self.bindings.append({
                    "joy": joy, "layout": lay,
                    "btn_off": 0, "axis_off": 0, "hat": lay["hat"],
                })
            return

        j   = self.joys[0]
        lay = layout_for(j)
        self.bindings.append({"joy": j, "layout": lay, "btn_off": 0,
                              "axis_off": 0, "hat": lay["hat"]})
        # Only split one device into two players when it's actually wide enough
        # to hold both halves. A single genuine Xbox pad has one controller's
        # worth of inputs, and offsetting into it would invent a player 2 whose
        # every input reads zero.
        try:
            merged = (j.get_numbuttons() >= 2 * MERGED_BUTTON_STRIDE
                      and j.get_numaxes() >= 2 * MERGED_AXIS_STRIDE)
        except pygame.error:
            merged = False
        if merged:
            self.bindings.append({"joy": j, "layout": lay,
                                  "btn_off": MERGED_BUTTON_STRIDE,
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
        if idx >= len(self.bindings):
            return ControllerState(
                "", {name: 0 for name in ADAPTER_LAYOUT["buttons"].values()},
                {name: 0.0 for name in ADAPTER_LAYOUT["axes"]}, (0, 0))
        b   = self.bindings[idx]
        joy = b["joy"]
        lay = b["layout"]
        buttons  = {name: 0 for name in lay["buttons"].values()}
        axes     = {name: 0.0 for name in lay["axes"]}
        triggers = {"LT": 0.0, "RT": 0.0}
        hat      = (0, 0)
        try:
            nb, na, nh = (joy.get_numbuttons(), joy.get_numaxes(),
                          joy.get_numhats())
            for raw, name in lay["buttons"].items():
                r = raw + b["btn_off"]
                if r < nb:
                    buttons[name] = joy.get_button(r)
            for name, raw in lay["axes"].items():
                r = raw + b["axis_off"]
                if r >= na:
                    continue
                raw_val = joy.get_axis(r)
                axes[name] = (trigger_filter(raw_val) if name in ("LT", "RT")
                              else axis_filter(raw_val))
            if lay["analog_triggers"]:
                triggers = {"LT": axes.get("LT", 0.0), "RT": axes.get("RT", 0.0)}
            else:
                # No analog triggers on this pad: L2/R2 are plain buttons, so
                # present them as fully-on/fully-off travel.
                triggers = {"LT": float(buttons.get("L2", 0)),
                            "RT": float(buttons.get("R2", 0))}
            if b["hat"] < nh:
                hat = joy.get_hat(b["hat"])
            name = joy.get_name()
        except pygame.error:
            name = ""
        return ControllerState(name, buttons, axes, hat, triggers)

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
            btns = self.read(idx).buttons
            # HOME only exists on a pad that has a Guide button; on the adapter
            # it is absent and SELECT is the only menu key.
            cur = btns.get("SELECT", 0) or btns.get("HOME", 0)
            if cur and not self._menu_latched.get(idx):
                pressed = True
            self._menu_latched[idx] = cur
        return pressed
