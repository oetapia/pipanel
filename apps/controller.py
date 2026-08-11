"""
Xbox controller client for Picar — racing-game style controls.

Runs two ways:
  * As a pipanel app  -> ControllerApp(P).run()  (renders a HUD to the panel)
  * Standalone in a terminal -> python apps/controller.py  (text status only)

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

import os
import sys
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
    from .display import make_sink
else:
    from picar_ws_client import PicarWsClientSync
    from display import make_sink


BLACK  = (0,   0,   0)
WHITE  = (255, 255, 255)
YELLOW = (255, 220, 0)
GREEN  = (0,   200, 0)
RED    = (220, 0,   0)
CYAN   = (0,   200, 200)
GREY   = (80,  80,  80)
LGREY  = (150, 150, 150)

# The control loop runs at ~50 Hz and the analog trigger/stick jitter by a unit
# or two even when held steady, so a raw "value changed?" test would fire many
# blocking WS commands per second. Quantizing to coarse steps means sub-step
# jitter no longer counts as a change (holding steady -> zero sends), and the
# min interval caps traffic for values that hover on a step boundary.
SPEED_STEP        = 5      # motor speed rounded to nearest 5 (0..100)
ANGLE_STEP        = 5      # servo angle rounded to nearest 5 degrees
MIN_SEND_INTERVAL = 0.05   # seconds between motor/servo sends (<=20 Hz)
# D-pad steering is incremental trim (+/-5 per step). Holding repeats at this
# interval so you can sweep, but far slower than the loop rate.
DPAD_TRIM_STEP     = 5      # degrees added/removed per d-pad step
DPAD_REPEAT_INTERVAL = 0.15  # seconds between repeats while d-pad is held


def _quantize(value, step):
    return int(round(value / step)) * step


class XboxController:
    def __init__(self):
        pygame.init()
        pygame.joystick.init()

        if pygame.joystick.get_count() == 0:
            raise RuntimeError("No controller found")

        self.joy = pygame.joystick.Joystick(0)
        self.joy.init()

    def deadzone(self, value, dz=0.15):
        if abs(value) < dz:
            return 0.0

        if value > 0:
            return (value - dz) / (1 - dz)

        return (value + dz) / (1 - dz)

    def read(self):
        pygame.event.pump()

        return {
            # buttons
            "a": self.joy.get_button(0),
            "b": self.joy.get_button(1),
            "x": self.joy.get_button(2),
            "y": self.joy.get_button(3),

            "lb": self.joy.get_button(4),
            "rb": self.joy.get_button(5),

            "select": self.joy.get_button(6),
            "start": self.joy.get_button(7),
            "home": self.joy.get_button(8),

            # stick clicks (L3/R3) -> manual lights
            "l3": self.joy.get_button(9),
            "r3": self.joy.get_button(10),

            # sticks
            "lx": self.deadzone(self.joy.get_axis(0)),
            "ly": -self.deadzone(self.joy.get_axis(1)),

            "rx": self.deadzone(self.joy.get_axis(3)),
            "ry": -self.deadzone(self.joy.get_axis(4)),

            # triggers normalized 0..1
            "lt": (self.joy.get_axis(2) + 1) / 2,
            "rt": (self.joy.get_axis(5) + 1) / 2,

            # dpad
            "dpad": self.joy.get_hat(0),
        }


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
        # Last time a motor/servo command actually went out, for rate limiting.
        self._last_motor_send = 0.0
        self._last_servo_send = 0.0
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
        if not self.client.connect():
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
        self.client.set_lights(self.light_state)
        self._log(f"\rLights: {self.light_state}" + " " * 20)

    def _send_motor(self, speed, now, force=False):
        """Send a motor command only if the (quantized) speed changed and the
        rate limit allows it. `force` bypasses the rate limit for stop/brake."""
        if speed == self.current_speed:
            return
        if not force and (now - self._last_motor_send) < MIN_SEND_INTERVAL:
            return
        self.client.set_motor(speed)
        self.current_speed = speed
        self._last_motor_send = now

    def _send_servo(self, angle, now, force=False):
        """Send a servo command only if the (quantized) angle changed and the
        rate limit allows it. `force` bypasses the rate limit (e.g. centre)."""
        if angle == self.current_angle:
            return
        if not force and (now - self._last_servo_send) < MIN_SEND_INTERVAL:
            return
        self.client.set_servo(angle)
        self.current_angle = angle
        self._last_servo_send = now

    def update(self, state):
        now = time.time()

        # RT = forward, LT = reverse (proportional). Quantize so analog jitter
        # while holding the trigger steady doesn't spam the API.
        rt = state["rt"]
        lt = state["lt"]

        if rt > 0.05:
            speed = max(10, _quantize(rt * self.base_speed, SPEED_STEP))
            self._send_motor(speed, now)
        elif lt > 0.05:
            speed = max(10, _quantize(lt * self.base_speed, SPEED_STEP))
            self._send_motor(-speed, now)
        elif self.current_speed != 0:
            # Release -> stop immediately (bypass the rate limit for safety).
            self._send_motor(0, now, force=True)

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
                angle = max(0, min(180, angle))
                self._send_servo(angle, now, force=True)
                self._last_dpad_send = now
                self._steering_mode = "dpad"
        elif lx != 0:
            angle = max(0, min(180, 90 + _quantize(lx * 90, ANGLE_STEP)))
            self._send_servo(angle, now)
            self._steering_mode = "stick"
        elif self._steering_mode == "stick" and self.current_angle != 90:
            # Recenter only after a stick release — a d-pad trim stays put.
            self._send_servo(90, now, force=True)
            self._steering_mode = "idle"

        # Button events (edge-triggered)
        if self._button_pressed(state, "a"):
            self.client.brake()
            self.current_speed = 0
            self._log("\rBRAKE" + " " * 20)

        if self._button_pressed(state, "x"):
            self.client.centre()
            self.current_angle = 90
            self._log("\rCentre" + " " * 20)

        if self._button_pressed(state, "y"):
            self.client.toggle_gear()
            self.gear_on = not self.gear_on
            self._log("\rGear toggled" + " " * 20)

        if self._button_pressed(state, "b"):
            self._cycle_lights()

        # Manual lights via stick clicks: L3 = front, R3 = back.
        if self._button_pressed(state, "l3"):
            self.light_state = "front"
            self.client.lights_front()
            self._log("\rLights: front" + " " * 20)

        if self._button_pressed(state, "r3"):
            self.light_state = "back"
            self.client.lights_back()
            self._log("\rLights: back" + " " * 20)

        if self._button_pressed(state, "lb"):
            self._adjust_speed(-5)

        if self._button_pressed(state, "rb"):
            self._adjust_speed(5)

        self._prev = state

    def shutdown(self):
        try:
            self.client.stop()
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
        S.blit(surf, (self.W - surf.get_width() - m, self.title_y + 6))

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

                state = self.input.read()
                # Start, Home (Guide), or Select returns to the menu. Home/Select
                # is the panel-wide "back to menu" button used by the other apps.
                if state["start"] or state["home"] or state["select"]:
                    break

                self.picar.update(state)
                self._draw()
                clock.tick(50)
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
