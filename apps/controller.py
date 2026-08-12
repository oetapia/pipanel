"""
Xbox controller client for Picar — racing-game style controls.

Runs two ways:
  * As a pipanel app  -> ControllerApp(P).run()  (renders a HUD to the panel)
  * Standalone in a terminal -> python apps/controller.py  (text status only)

Input mapping comes from apps/controller_profile.py, which detects whether the
pad has analog triggers, so this works on both a real Xbox pad and the 4-axis
dual adapter.

Sending is decoupled from input: the loop only updates a desired state and
ControlLink pushes it to the car at a fixed 20 Hz ceiling, fire-and-forget,
coalescing to the newest value. Nothing in the input path waits on the network.
Watch the "tx/s" figure on the HUD to see what the link is actually sending.

Controls:
    RT (hold):          Accelerate forward (proportional to pressure)
    LT (hold):          Reverse (proportional to pressure)
    Left stick X-axis:  Proportional steering (auto-centres on release)
    D-pad Left/Right:   Trim steering -/+5 deg per step (holds position)
    LB/RB:              Decrease/Increase max speed
    A:                  Brake (hard stop)
    X:                  Centre steering
    Y:                  Toggle gear
    B:                  Cycle lights (off -> front -> back -> both)
    L3 (stick click):   Front lights
    R3 (stick click):   Back lights
    Start:              Quit (back to menu)

Usage:
    python apps/controller.py [--ip IP] [--port PORT] [--speed SPEED]
"""

import math
import os
import sys
import threading
import time
from pathlib import Path

import pygame

try:
    parent_dir = Path(__file__).parent.parent
    if str(parent_dir) not in sys.path:
        sys.path.insert(0, str(parent_dir))
    import config
    PICAR_IP = config.car_ip
except (ImportError, AttributeError):
    print("Warning: Could not import config.py, using default IP")
    PICAR_IP = "192.168.178.59"

# Works both as a package submodule (apps.controller) and as a standalone script.
# Pick the import by load context so an error *inside* picar_ws_client (e.g. a
# missing dependency) propagates with its real message instead of being masked.
if __package__:
    from .picar_ws_client import PicarWsClientSync
    from .controller_profile import ControllerProfile
    from .display import make_sink
else:
    from picar_ws_client import PicarWsClientSync
    from controller_profile import ControllerProfile
    from display import make_sink


BLACK  = (0,   0,   0)
WHITE  = (255, 255, 255)
YELLOW = (255, 220, 0)
GREEN  = (0,   200, 0)
RED    = (220, 0,   0)
CYAN   = (0,   200, 200)
GREY   = (80,  80,  80)
LGREY  = (150, 150, 150)

# Input sampling is fast and free; network sends are neither. The loop below
# only ever updates a *desired state*, and ControlLink pushes that state to the
# car at its own modest cadence. So these constants are about the wire, not the
# input: see ControlLink for why each one exists.
CONTROL_HZ      = 20     # ceiling on control frames per second
CONTROL_TICK    = 1.0 / CONTROL_HZ
KEEPALIVE       = 0.25   # resend unchanged state so a dropped frame self-heals
SPEED_HYSTERESIS = 4     # motor % a value must move before it's worth a frame
ANGLE_HYSTERESIS = 3     # servo degrees likewise
SLEW_DEG_PER_S  = 240    # cap servo travel so a stick flick can't slam it
TRIGGER_ON      = 0.05   # trigger travel below this counts as released

STEER_EXPO = 2.0   # >1 softens the centre: fine trim near 0, full lock at the end

# Input polling is cheap, drawing is not, so they get separate rates.
INPUT_HZ      = 50
DRAW_HZ       = 12
DRAW_INTERVAL = 1.0 / DRAW_HZ

# D-pad steering is incremental trim (+/-5 per step). Holding repeats at this
# interval so you can sweep, but far slower than the loop rate.
DPAD_TRIM_STEP     = 5      # degrees added/removed per d-pad step
DPAD_REPEAT_INTERVAL = 0.15  # seconds between repeats while d-pad is held


def _expo(value, curve=STEER_EXPO):
    """Shape a -1..1 axis so small deflections stay small.

    Linear steering spends most of the stick's travel in angles that are all
    too sharp to be useful, which is what makes you saw at the stick — and
    every correction used to cost a network command."""
    return math.copysign(abs(value) ** curve, value)


class XboxController:
    """Reads one pad through the shared ControllerProfile.

    Exists so the control logic keeps its dict-shaped input, but the index map
    now comes from apps/controller_profile.py instead of being hardcoded here —
    this class used to assume 6 axes and would read a resting right stick as a
    half-pressed trigger on the 4-axis dual adapter."""

    def __init__(self):
        pygame.init()
        pygame.joystick.init()

        self.pads = ControllerProfile()
        if self.pads.refresh() == 0:
            raise RuntimeError("No controller found")

    @property
    def joy(self):
        return self.pads.bindings[0]["joy"] if self.pads.bindings else None

    def read(self):
        if self.pads.refresh() == 0:
            raise RuntimeError("Controller disconnected")
        st = self.pads.read(0)

        return {
            "a": st.buttons.get("A", 0),
            "b": st.buttons.get("B", 0),
            "x": st.buttons.get("X", 0),
            "y": st.buttons.get("Y", 0),

            "lb": st.buttons.get("L1", 0),
            "rb": st.buttons.get("R1", 0),

            "select": st.buttons.get("SELECT", 0),
            "start": st.buttons.get("START", 0),
            "home": st.buttons.get("HOME", 0),

            # stick clicks (L3/R3) -> manual lights
            "l3": st.buttons.get("L3", 0),
            "r3": st.buttons.get("R3", 0),

            # sticks
            "lx": st.axes.get("LX", 0.0),
            "ly": -st.axes.get("LY", 0.0),

            "rx": st.axes.get("RX", 0.0),
            "ry": -st.axes.get("RY", 0.0),

            # triggers 0..1 — analog on an Xbox pad, digital L2/R2 on the adapter
            "lt": st.triggers.get("LT", 0.0),
            "rt": st.triggers.get("RT", 0.0),

            "dpad": st.hat,
        }


class ControlLink(threading.Thread):
    """Owns all throttle/steering sending, decoupled from input sampling.

    The input loop only assigns target_speed / target_angle; this thread decides
    what actually goes on the wire. That split is the whole point:

      * nothing in the input path can block on the network, so input keeps being
        sampled at full rate even when the link is slow;
      * updates coalesce — only the newest desired state is ever sent, so a fast
        stick sweep costs one frame per tick instead of one per sample;
      * hysteresis means a resting thumb sends nothing at all, where quantizing
        an absolute value would flip back and forth across a step boundary
        forever on a unit of jitter.

    Sends are fire-and-forget. Throttle and steering are idempotent state, not
    events, so a dropped frame is corrected by the next one — and KEEPALIVE
    guarantees there is a next one even when the input is unchanged.
    """

    def __init__(self, client):
        super().__init__(daemon=True, name="ControlLink")
        self.client = client
        # Written by the input loop, read here. Single scalar assignments, so no
        # lock is needed: a reader can only ever see an old or a new value.
        self.target_speed = 0
        self.target_angle = 90
        # What the car was last told, and what we believe it is now doing.
        self.sent_speed = 0
        self.sent_angle = 90
        self.frames_sent = 0
        # Control frames/second over the last window, for the HUD — the number
        # to watch if you suspect the link is being flooded again.
        self.fps = 0.0
        self._rate_t0 = 0.0
        self._rate_n = 0
        self._slewed_angle = 90.0
        # NB: not `_stop` — threading.Thread already uses that name internally
        # for its own bookkeeping, and shadowing it breaks join().
        self._stopped = threading.Event()

    def stop(self):
        self._stopped.set()

    def _slew(self, target, dt):
        """Rate-limit servo travel toward the target."""
        limit = SLEW_DEG_PER_S * dt
        delta = target - self._slewed_angle
        if abs(delta) > limit:
            delta = math.copysign(limit, delta)
        self._slewed_angle += delta
        return int(round(self._slewed_angle))

    def _worth_sending(self, speed, angle):
        # Direction changes and full stops must never be held back by
        # hysteresis — those are the frames that matter most.
        if (speed == 0) != (self.sent_speed == 0):
            return True
        if (speed > 0) != (self.sent_speed > 0):
            return True
        return (abs(speed - self.sent_speed) >= SPEED_HYSTERESIS
                or abs(angle - self.sent_angle) >= ANGLE_HYSTERESIS)

    def run(self):
        last_send = 0.0
        last_tick = time.monotonic()
        while not self._stopped.is_set():
            now = time.monotonic()
            dt, last_tick = now - last_tick, now

            speed = self.target_speed
            angle = self._slew(self.target_angle, dt)

            if self._worth_sending(speed, angle) or (now - last_send) >= KEEPALIVE:
                self.client.post_control(speed, angle)
                self.sent_speed, self.sent_angle = speed, angle
                self.frames_sent += 1
                self._rate_n += 1
                last_send = now

            if now - self._rate_t0 >= 1.0:
                self.fps = self._rate_n / (now - self._rate_t0) if self._rate_t0 else 0.0
                self._rate_t0, self._rate_n = now, 0

            self._stopped.wait(CONTROL_TICK)

    def flush(self, speed, angle):
        """Push a state change immediately, bypassing tick and hysteresis.

        For brake / centre / disconnect, where waiting up to a tick is wrong."""
        self.target_speed, self.target_angle = speed, angle
        self._slewed_angle = float(angle)
        self.sent_speed, self.sent_angle = speed, angle
        self.client.post_control(speed, angle)


class PicarXboxController:
    """Control logic: maps controller state onto Picar commands.

    Holds the current control state (speed, angle, gear, lights) so a HUD can
    render it. Set quiet=True to suppress the terminal status prints.
    """

    def __init__(self, ip=PICAR_IP, port=5000, base_speed=75,
                 left_angle=45, right_angle=135, quiet=False):
        self.client = PicarWsClientSync(ip, port)
        # Lights are driven manually (L3/R3); disable the WS client's auto-lights
        # so motor commands don't piggyback a light command (which raced the
        # response queue).
        self.client.auto_lights = False
        self.base_speed = base_speed
        self.left_angle = left_angle
        self.right_angle = right_angle
        self.current_speed = 0
        self.current_angle = 90
        self.gear_on = False
        self.light_state = "off"
        self._light_cycle = ["off", "front", "back", "both"]
        self._prev = {}
        self.quiet = quiet
        # All throttle/steering traffic goes through here, on its own thread.
        self.link = ControlLink(self.client)
        # Steering source: the stick auto-recenters on release, the d-pad trim
        # holds. Track which one last moved so we only recenter after the stick.
        self._steering_mode = "idle"
        # Last time a d-pad trim step fired, for hold-to-repeat.
        self._last_dpad_send = 0.0

    def _log(self, msg):
        if not self.quiet:
            print(msg, end="")

    def connect(self):
        print("Connecting to Picar...")
        connected = self.client.connect()
        # Start the sender either way: posts are dropped harmlessly while the
        # link is down, and the WS client reconnects in the background — if the
        # thread only started on a successful first connect, driving would
        # silently do nothing after a later reconnect.
        self.link.start()
        if not connected:
            time.sleep(3)
            if not self.client.connected:
                print("Could not connect to Picar. Is the Pico running?")
                return False

        try:
            s = self.client.status()
            if s.get('success'):
                self.gear_on = bool(s.get('gear_on'))
                gear_str = "LOW" if self.gear_on else "OFF"
                print(f"Connected. Motor: {s['motor_speed']}, "
                      f"Servo: {s['servo_angle']}, Gear: {gear_str}")
        except Exception as e:
            print(f"Connected but status error: {e}")

        return True

    def _button_pressed(self, state, key):
        return state.get(key) and not self._prev.get(key)

    def _adjust_speed(self, delta):
        self.base_speed = max(0, min(100, self.base_speed + delta))
        self._log(f"\rBase speed: {self.base_speed}" + " " * 20)

    def _cycle_lights(self):
        idx = self._light_cycle.index(self.light_state)
        self.light_state = self._light_cycle[(idx + 1) % len(self._light_cycle)]
        self.client.post_lights(self.light_state)
        self._log(f"\rLights: {self.light_state}" + " " * 20)

    def update(self, state):
        """Translate one input sample into desired vehicle state. Never blocks.

        Nothing here touches the network: assignments to link.target_* are
        picked up by the ControlLink thread, which decides what is worth
        sending. So this can be called as fast as the pad can be polled."""
        now = time.time()

        # RT = forward, LT = reverse (proportional on an analog trigger, on/off
        # on the adapter's L2/R2 buttons — the profile normalises both to 0..1).
        rt = state["rt"]
        lt = state["lt"]

        if rt > TRIGGER_ON:
            self.current_speed = max(10, int(round(rt * self.base_speed)))
        elif lt > TRIGGER_ON:
            self.current_speed = -max(10, int(round(lt * self.base_speed)))
        else:
            self.current_speed = 0
        self.link.target_speed = self.current_speed

        # Steering. The d-pad is incremental trim: each step adds +/-5 to the
        # *current* angle and holds there. Holding repeats slowly. The left
        # stick is absolute/proportional and auto-recenters on release; the
        # d-pad trim must NOT be undone by that recenter, so we only recenter
        # when the stick was the last thing to move.
        lx = state["lx"]
        dpad_x = state["dpad"][0]
        prev_dpad_x = self._prev.get("dpad", (0, 0))[0]

        if dpad_x in (-1, 1):
            # Fire on the initial press, then repeat at a slow interval while held.
            new_press = dpad_x != prev_dpad_x
            if new_press or (now - self._last_dpad_send) >= DPAD_REPEAT_INTERVAL:
                angle = self.current_angle + dpad_x * DPAD_TRIM_STEP
                self.current_angle = max(0, min(180, angle))
                self.link.target_angle = self.current_angle
                self._last_dpad_send = now
                self._steering_mode = "dpad"
        elif lx != 0:
            self.current_angle = max(0, min(180, int(round(90 + _expo(lx) * 90))))
            self.link.target_angle = self.current_angle
            self._steering_mode = "stick"
        elif self._steering_mode == "stick" and self.current_angle != 90:
            # Recenter only after a stick release — a d-pad trim stays put.
            self.current_angle = 90
            self.link.target_angle = 90
            self._steering_mode = "idle"

        # Button events (edge-triggered)
        if self._button_pressed(state, "a"):
            self.current_speed = 0
            self.link.flush(0, self.current_angle)   # immediate, skips the tick
            self.client.post({"c": "b"})             # active brake
            self._log("\rBRAKE" + " " * 20)

        if self._button_pressed(state, "x"):
            self.current_angle = 90
            self.link.flush(self.current_speed, 90)
            self._log("\rCentre" + " " * 20)

        # Discrete commands are posted too — the local state is updated
        # optimistically either way, so there is nothing to wait for, and a
        # blocking call here would stall the input loop for a whole RTT.
        if self._button_pressed(state, "y"):
            self.client.post({"c": "g", "v": "toggle"})
            self.gear_on = not self.gear_on
            self._log("\rGear toggled" + " " * 20)

        if self._button_pressed(state, "b"):
            self._cycle_lights()

        # Manual lights via stick clicks: L3 = front, R3 = back.
        if self._button_pressed(state, "l3"):
            self.light_state = "front"
            self.client.post_lights("front")
            self._log("\rLights: front" + " " * 20)

        if self._button_pressed(state, "r3"):
            self.light_state = "back"
            self.client.post_lights("back")
            self._log("\rLights: back" + " " * 20)

        if self._button_pressed(state, "lb"):
            self._adjust_speed(-5)

        if self._button_pressed(state, "rb"):
            self._adjust_speed(5)

        self._prev = state

    def shutdown(self):
        try:
            # Stop the sender before the final commands so it can't re-push a
            # stale target on top of them.
            self.link.stop()
            if self.link.is_alive():
                self.link.join(timeout=1)
            self.client.stop()          # blocking on purpose: must land
            self.client.lights_off()
            self.client.disconnect()
        except Exception:
            pass

    def run(self):
        """Standalone terminal loop (no HUD)."""
        if not self.connect():
            return

        try:
            controller = XboxController()
        except RuntimeError as e:
            print(f"Controller error: {e}")
            return

        print(f"\nController: {controller.joy.get_name()}")
        self.client.send_text("Xbox Ready")

        print("\n" + "=" * 60)
        print("PICAR XBOX CONTROLLER (Racing Mode)")
        print("=" * 60)
        print(f"\n  RT (hold):       Accelerate (proportional)")
        print(f"  LT (hold):       Reverse (proportional)")
        print(f"  Left stick L/R:  Proportional steering (auto-centres)")
        print(f"  D-pad L/R:       Trim steering -/+5 (holds)")
        print(f"  RB/LB:           Speed up/down (max: {self.base_speed})")
        print(f"  A:               Brake")
        print(f"  X:               Centre steering")
        print(f"  Y:               Toggle gear")
        print(f"  B:               Cycle lights")
        print(f"  L3/R3:           Front/Back lights")
        print(f"  Start:           Quit")
        print("=" * 60)

        try:
            while True:
                state = controller.read()

                if state["start"] or state["home"] or state["select"]:
                    break

                self.update(state)
                time.sleep(0.02)

        except KeyboardInterrupt:
            pass

        self.shutdown()
        pygame.quit()
        print("\nDisconnected. Goodbye.")


class ControllerApp:
    """pipanel app: drives the Picar and renders a HUD to the display sink."""

    def __init__(self, P, ip=PICAR_IP, port=5000, base_speed=75, sink=None):
        M = P["main"]

        self.W = P["screen"]["w"]
        self.H = P["screen"]["h"]
        self.sink = sink or make_sink(P)

        os.environ["SDL_VIDEODRIVER"] = "offscreen"
        pygame.init()
        pygame.joystick.init()
        self.screen = pygame.display.set_mode((self.W, self.H))
        pygame.mouse.set_visible(False)

        self.fnt_title = pygame.font.SysFont(None, M["fonts"]["title"])
        self.fnt_name  = pygame.font.SysFont(None, M["fonts"]["name"])
        self.fnt_desc  = pygame.font.SysFont(None, M["fonts"]["desc"])
        self.fnt_hint  = pygame.font.SysFont(None, M["fonts"]["hint"])

        self.margin     = M["title_x"]
        self.title_y    = M["title_y"]
        self.divider_y  = M["divider_y"]
        self.row_h      = int(self.fnt_name.get_linesize() * 1.35)
        self.foot_line  = M["hint_line_offset"]
        self.foot_txt   = M["hint_text_offset"]

        self.picar = PicarXboxController(ip=ip, port=port,
                                         base_speed=base_speed, quiet=True)
        self.input = None

    def _ensure_input(self):
        if self.input is not None:
            return True
        pygame.event.pump()
        if pygame.joystick.get_count() == 0:
            return False
        try:
            self.input = XboxController()
        except RuntimeError:
            return False
        return True

    def t(self, txt, x, y, col, fnt=None, max_w=None):
        fnt = fnt or self.fnt_desc
        txt = str(txt)
        if max_w:
            while txt and fnt.size(txt)[0] > max_w:
                txt = txt[:-1]
        self.screen.blit(fnt.render(txt, True, col), (x, y))

    def _draw(self, status_msg=None):
        p = self.picar
        S = self.screen
        m = self.margin
        S.fill(BLACK)

        self.t("PICAR CONTROLLER", m, self.title_y, YELLOW, self.fnt_title)

        online   = p.client.connected
        conn_txt = "● ONLINE" if online else "○ OFFLINE"
        conn_col = GREEN if online else RED
        surf = self.fnt_desc.render(conn_txt, True, conn_col)
        conn_x = self.W - surf.get_width() - m
        S.blit(surf, (conn_x, self.title_y + 6))

        # Outbound control-frame rate. This is the number that used to run away:
        # it should sit near 4/s idle (keepalive only) and cap at CONTROL_HZ.
        tx = self.fnt_hint.render(f"{p.link.fps:.0f} tx/s", True, GREY)
        S.blit(tx, (conn_x - tx.get_width() - 12, self.title_y + 8))

        pygame.draw.line(S, YELLOW, (m, self.divider_y),
                         (self.W - m, self.divider_y), 1)

        if status_msg:
            self.t(status_msg, m, self.divider_y + self.row_h, LGREY, self.fnt_name)
        else:
            spd = p.current_speed
            direction = "FWD" if spd > 0 else ("REV" if spd < 0 else "IDLE")
            rows = [
                ("Speed",     f"{abs(spd)}%  {direction}"),
                ("Steering",  f"{p.current_angle}°"),
                ("Max speed", f"{p.base_speed}%"),
                ("Gear",      "LOW" if p.gear_on else "OFF"),
                ("Lights",    p.light_state.upper()),
            ]
            val_x = self.margin + (self.W - 2 * self.margin) // 2
            y = self.divider_y + self.row_h
            for label, val in rows:
                self.t(label, m, y, LGREY, self.fnt_name)
                self.t(val, val_x, y, WHITE, self.fnt_name)
                y += self.row_h

        pygame.draw.line(S, GREY, (0, self.H - self.foot_line),
                         (self.W, self.H - self.foot_line), 1)
        self.t("RT/LT Drive  Stick/Dpad Steer  A Brake  Y Gear  L3/R3 Lights  Start/Home Menu",
               m, self.H - self.foot_txt, CYAN, self.fnt_hint,
               max_w=self.W - 2 * m)

        self.sink.write(S)

    def run(self):
        self._draw("Connecting to Picar...")
        self.picar.connect()
        if self.picar.client.connected:
            self.picar.client.send_text("Xbox Ready")

        clock = pygame.time.Clock()
        last_draw = 0.0
        try:
            while True:
                # Keyboard fallback exit (works when a console is attached).
                for event in pygame.event.get():
                    if event.type == pygame.QUIT:
                        return
                    if event.type == pygame.KEYDOWN and event.key in (
                            pygame.K_ESCAPE, pygame.K_q):
                        return

                if not self._ensure_input():
                    self._draw("Waiting for controller... (ESC to go back)")
                    clock.tick(10)
                    continue

                try:
                    state = self.input.read()
                except RuntimeError:
                    # Pad unplugged mid-drive: stop the car, then wait for it.
                    self.picar.link.flush(0, 90)
                    self.input = None
                    continue
                # Start, Home (Guide), or Select returns to the menu. Home/Select
                # is the panel-wide "back to menu" button used by the other apps.
                if state["start"] or state["home"] or state["select"]:
                    break

                self.picar.update(state)

                # Redraw far slower than we sample. A full frame here is an
                # RGB565 repack plus a ~300 KB framebuffer write (see
                # display.py), which at input rate would starve the input.
                now = time.monotonic()
                if now - last_draw >= DRAW_INTERVAL:
                    self._draw()
                    last_draw = now

                clock.tick(INPUT_HZ)
        except KeyboardInterrupt:
            pass
        finally:
            self.picar.shutdown()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Picar Xbox controller")
    parser.add_argument("--ip", type=str, default=PICAR_IP,
                        help=f"Pico IP address (default: {PICAR_IP})")
    parser.add_argument("--port", type=int, default=5000,
                        help="Pico port (default: 5000)")
    parser.add_argument("--speed", type=int, default=75,
                        help="Base motor speed (0-100, default 75)")
    parser.add_argument("--left-angle", type=int, default=45,
                        help="Servo angle for left (default 45)")
    parser.add_argument("--right-angle", type=int, default=135,
                        help="Servo angle for right (default 135)")
    args = parser.parse_args()

    ctrl = PicarXboxController(
        ip=args.ip,
        port=args.port,
        base_speed=args.speed,
        left_angle=args.left_angle,
        right_angle=args.right_angle,
    )
    ctrl.run()


if __name__ == "__main__":
    main()
