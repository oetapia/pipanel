"""
Xbox controller client for Picar — racing-game style controls.

Runs two ways:
  * As a pipanel app  -> ControllerApp(P).run()  (renders a HUD to the panel)
  * Standalone in a terminal -> python apps/controller.py  (text status only)

Controls:
    RT (hold):          Accelerate forward (proportional to pressure)
    LT (hold):          Reverse (proportional to pressure)
    Left stick X-axis:  Proportional steering
    D-pad Left/Right:   Steer left/right (fixed angles)
    LB/RB:              Decrease/Increase max speed
    A:                  Brake (hard stop)
    X:                  Centre steering
    Y:                  Toggle gear
    B:                  Cycle lights (off -> front -> back -> both)
    Start:              Quit (back to menu)

Usage:
    python apps/controller.py [--ip IP] [--port PORT] [--speed SPEED]
"""

import os
import sys
import time
from pathlib import Path

import numpy as np
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
try:
    from .picar_ws_client import PicarWsClientSync
except ImportError:
    from picar_ws_client import PicarWsClientSync


BLACK  = (0,   0,   0)
WHITE  = (255, 255, 255)
YELLOW = (255, 220, 0)
GREEN  = (0,   200, 0)
RED    = (220, 0,   0)
CYAN   = (0,   200, 200)
GREY   = (80,  80,  80)
LGREY  = (150, 150, 150)


def fb_write(surface, fb):
    raw = pygame.surfarray.array3d(surface).transpose(1, 0, 2)
    r = (raw[:, :, 0].astype(np.uint16) >> 3) << 11
    g = (raw[:, :, 1].astype(np.uint16) >> 2) << 5
    b =  raw[:, :, 2].astype(np.uint16) >> 3
    with open(fb, "wb") as f:
        f.write((r | g | b).astype(np.uint16).tobytes())


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

    def update(self, state):
        # RT = forward, LT = reverse (proportional)
        rt = state["rt"]
        lt = state["lt"]

        if rt > 0.05:
            speed = int(rt * self.base_speed)
            speed = max(10, speed)
            if speed != self.current_speed:
                self.client.set_motor(speed)
                self.current_speed = speed
        elif lt > 0.05:
            speed = int(lt * self.base_speed)
            speed = max(10, speed)
            if -speed != self.current_speed:
                self.client.set_motor(-speed)
                self.current_speed = -speed
        elif self.current_speed != 0:
            self.client.stop()
            self.current_speed = 0

        # Left stick X = proportional steering
        lx = state["lx"]
        if lx != 0:
            angle = 90 + int(lx * 90)
            angle = max(0, min(180, angle))
            if angle != self.current_angle:
                self.client.set_servo(angle)
                self.current_angle = angle
        elif self.current_angle != 90:
            self.client.set_servo(90)
            self.current_angle = 90

        # D-pad left/right: fixed-angle steering
        dpad_x = state["dpad"][0]
        if dpad_x == -1:
            self.client.set_servo(self.left_angle)
            self.current_angle = self.left_angle
        elif dpad_x == 1:
            self.client.set_servo(self.right_angle)
            self.current_angle = self.right_angle

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
        print(f"  Left stick L/R:  Proportional steering")
        print(f"  D-pad L/R:       Fixed-angle steering")
        print(f"  RB/LB:           Speed up/down (max: {self.base_speed})")
        print(f"  A:               Brake")
        print(f"  X:               Centre steering")
        print(f"  Y:               Toggle gear")
        print(f"  B:               Cycle lights")
        print(f"  Start:           Quit")
        print("=" * 60)

        try:
            while True:
                state = controller.read()

                if state["start"]:
                    break

                self.update(state)
                time.sleep(0.02)

        except KeyboardInterrupt:
            pass

        self.shutdown()
        pygame.quit()
        print("\nDisconnected. Goodbye.")


class ControllerApp:
    """pipanel app: drives the Picar and renders a HUD to the framebuffer."""

    def __init__(self, P, ip=PICAR_IP, port=5000, base_speed=75):
        M   = P["main"]
        sdl = P["sdl"]

        self.W = P["screen"]["w"]
        self.H = P["screen"]["h"]
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
        self.t("RT/LT Drive  Stick Steer  A Brake  Y Gear  B Lights  Start Quit",
               m, self.H - self.foot_txt, CYAN, self.fnt_hint,
               max_w=self.W - 2 * m)

        fb_write(S, self.fb)

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
                if state["start"]:
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
