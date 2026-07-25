"""
Two-player Pong — rendered to the pipanel framebuffer.

Controls (one controller per player, per the Pi's two-device enumeration):
  * Controller 1 -> LEFT paddle   (left stick Y, or D-pad up/down)
  * Controller 2 -> RIGHT paddle  (left stick Y, or D-pad up/down)
  * Either START (button 9)       -> serve / restart after a point
  * Keyboard fallback: W/S left, ↑/↓ right, Enter serve, ESC/Q quit

Controller enumeration mirrors apps/controller_demo.py:
  * 2+ devices (Raspberry Pi) -> device 0 = P1, device 1 = P2
  * 1 merged device (macOS)   -> P2 offset into the upper index block

Runs two ways:
  * As a pipanel app  -> PongApp(P).run()
  * Standalone        -> python apps/pong.py  (uses 35panel profile)
"""

import json
import math
import os
import time

import numpy as np
import pygame


# Per-controller input layout (matches controller_demo.py; confirmed on Pi).
LAYOUT = {
    "axis_y": 1,      # left stick Y
    "hat": 0,         # D-pad
    "start": 9,       # START button
}
MERGED_BUTTON_STRIDE = 12
MERGED_AXIS_STRIDE   = 4

AXIS_DEADZONE = 0.15

BLACK  = (0,   0,   0)
WHITE  = (255, 255, 255)
YELLOW = (255, 220, 0)
GREEN  = (0,   200, 0)
CYAN   = (0,   200, 200)
GREY   = (80,  80,  80)
LGREY  = (150, 150, 150)

P1_COLOR = (90, 200, 255)
P2_COLOR = (255, 150, 90)

WIN_SCORE = 7


def fb_write(surface, fb):
    raw = pygame.surfarray.array3d(surface).transpose(1, 0, 2)
    r = (raw[:, :, 0].astype(np.uint16) >> 3) << 11
    g = (raw[:, :, 1].astype(np.uint16) >> 2) << 5
    b =  raw[:, :, 2].astype(np.uint16) >> 3
    with open(fb, "wb") as f:
        f.write((r | g | b).astype(np.uint16).tobytes())


def _axis_filter(v):
    return 0.0 if abs(v) < AXIS_DEADZONE else v


class PongApp:
    """pipanel app: two-player Pong drawn to the framebuffer."""

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

        # Fonts scaled off the profile's main-menu sizes.
        self.fnt_score = pygame.font.SysFont(None, M["fonts"]["title"])
        self.fnt_big   = pygame.font.SysFont(None, M["fonts"]["name"])
        self.fnt_hint  = pygame.font.SysFont(None, M["fonts"]["hint"])

        # Geometry scaled to screen size so it fits any profile.
        self.margin      = max(6, self.W // 40)
        self.paddle_w    = max(4, self.W // 80)
        self.paddle_h    = max(24, self.H // 5)
        self.ball_size   = max(4, self.W // 80)
        self.paddle_speed = self.H * 1.2        # px/sec (stick/keyboard)
        self.ball_speed   = self.W * 0.55       # px/sec initial
        self.ball_speedup = 1.05                # per paddle hit
        self.ball_speed_max = self.W * 1.4

        # Paddle X positions (fixed).
        self.p1_x = self.margin
        self.p2_x = self.W - self.margin - self.paddle_w

        self.joys     = []
        self.bindings = []   # [{axis_off, btn_off, hat, joy}] per player index

        self._reset_match()

    # ------------------------------------------------------------------
    # Controller handling (device-count aware, per controller_demo.py)
    # ------------------------------------------------------------------
    def _build_bindings(self):
        self.bindings = []
        if not self.joys:
            return
        if len(self.joys) >= 2:
            for idx in range(2):
                self.bindings.append({
                    "joy": self.joys[idx],
                    "axis_off": 0, "btn_off": 0, "hat": LAYOUT["hat"],
                })
        else:
            j = self.joys[0]
            self.bindings.append({"joy": j, "axis_off": 0,
                                  "btn_off": 0, "hat": 0})
            self.bindings.append({"joy": j, "axis_off": MERGED_AXIS_STRIDE,
                                  "btn_off": MERGED_BUTTON_STRIDE, "hat": 1})

    def _ensure_joysticks(self):
        pygame.event.pump()
        count = pygame.joystick.get_count()
        if count == 0:
            if self.joys:
                self.joys = []
                self.bindings = []
            return
        if len(self.joys) == count and self.bindings:
            return
        self.joys = []
        for i in range(count):
            try:
                j = pygame.joystick.Joystick(i)
                j.init()
                self.joys.append(j)
            except pygame.error:
                pass
        self._build_bindings()

    def _player_input(self, idx):
        """Return (-1..1 vertical intent, start_pressed) for player idx."""
        if idx >= len(self.bindings):
            return 0.0, False
        b   = self.bindings[idx]
        joy = b["joy"]
        move = 0.0
        try:
            axis = LAYOUT["axis_y"] + b["axis_off"]
            if axis < joy.get_numaxes():
                move = _axis_filter(joy.get_axis(axis))
            hat_id = b["hat"]
            if hat_id < joy.get_numhats():
                hy = joy.get_hat(hat_id)[1]
                if hy:
                    move = -hy  # hat up (+1) should move paddle up (-y)
            start_btn = LAYOUT["start"] + b["btn_off"]
            start = (start_btn < joy.get_numbuttons()
                     and joy.get_button(start_btn))
        except pygame.error:
            return 0.0, False
        return move, bool(start)

    # ------------------------------------------------------------------
    # Game state
    # ------------------------------------------------------------------
    def _reset_match(self):
        self.score = [0, 0]
        self.p1_y = (self.H - self.paddle_h) / 2
        self.p2_y = (self.H - self.paddle_h) / 2
        self.winner = None
        self._center_ball(direction=1)
        self.serving = True   # ball held until a START/Enter serve

    def _center_ball(self, direction):
        self.ball_x = (self.W - self.ball_size) / 2
        self.ball_y = (self.H - self.ball_size) / 2
        speed = self.ball_speed
        # Launch at a shallow angle toward `direction` (1 right, -1 left).
        self.ball_vx = direction * speed
        self.ball_vy = (speed * 0.35) * (1 if int(self.ball_y) % 2 else -1)

    def _serve(self):
        if self.winner is not None:
            self._reset_match()
        self.serving = False

    # ------------------------------------------------------------------
    def _update(self, dt, move1, move2):
        # Paddles
        self.p1_y += move1 * self.paddle_speed * dt
        self.p2_y += move2 * self.paddle_speed * dt
        self.p1_y = max(0, min(self.H - self.paddle_h, self.p1_y))
        self.p2_y = max(0, min(self.H - self.paddle_h, self.p2_y))

        if self.serving or self.winner is not None:
            return

        # Ball
        self.ball_x += self.ball_vx * dt
        self.ball_y += self.ball_vy * dt

        # Top/bottom walls
        if self.ball_y <= 0:
            self.ball_y = 0
            self.ball_vy = abs(self.ball_vy)
        elif self.ball_y + self.ball_size >= self.H:
            self.ball_y = self.H - self.ball_size
            self.ball_vy = -abs(self.ball_vy)

        # Paddle collisions
        self._bounce_paddle(self.p1_x + self.paddle_w, self.p1_y,
                            moving_left=False)
        self._bounce_paddle(self.p2_x, self.p2_y, moving_left=True)

        # Scoring
        if self.ball_x + self.ball_size < 0:
            self._point(scorer=1)
        elif self.ball_x > self.W:
            self._point(scorer=0)

    def _bounce_paddle(self, face_x, paddle_y, moving_left):
        bx1, bx2 = self.ball_x, self.ball_x + self.ball_size
        by1, by2 = self.ball_y, self.ball_y + self.ball_size
        py1, py2 = paddle_y, paddle_y + self.paddle_h

        if moving_left:
            hit = self.ball_vx < 0 and bx1 <= face_x and bx2 >= face_x
        else:
            hit = self.ball_vx > 0 and bx2 >= face_x and bx1 <= face_x
        if not hit or by2 < py1 or by1 > py2:
            return

        # Reflect and add angle based on where it struck the paddle.
        rel = ((self.ball_y + self.ball_size / 2) - (paddle_y + self.paddle_h / 2))
        rel /= (self.paddle_h / 2)  # -1..1
        speed = min(self.ball_speed_max,
                    (self.ball_vx ** 2 + self.ball_vy ** 2) ** 0.5
                    * self.ball_speedup)
        direction = 1 if moving_left is False else -1
        angle = rel * (math.pi / 3.5)  # up to ~51°
        self.ball_vx = direction * speed * math.cos(angle)
        self.ball_vy = speed * math.sin(angle)
        # Nudge out of the paddle so we don't re-collide next frame.
        if moving_left:
            self.ball_x = face_x - self.ball_size - 1
        else:
            self.ball_x = face_x + 1

    def _point(self, scorer):
        self.score[scorer] += 1
        if self.score[scorer] >= WIN_SCORE:
            self.winner = scorer
            self.serving = True
        else:
            # Serve toward the player who was just scored on.
            self._center_ball(direction=-1 if scorer == 0 else 1)
            self.serving = True

    # ------------------------------------------------------------------
    def _draw(self):
        S = self.screen
        S.fill(BLACK)

        # Center dashed net
        dash = max(6, self.H // 24)
        x = self.W // 2
        yy = 0
        while yy < self.H:
            pygame.draw.rect(S, GREY, (x - 1, yy, 2, dash))
            yy += dash * 2

        # Scores
        s1 = self.fnt_score.render(str(self.score[0]), True, P1_COLOR)
        s2 = self.fnt_score.render(str(self.score[1]), True, P2_COLOR)
        S.blit(s1, (self.W // 2 - self.W // 8 - s1.get_width() // 2, self.margin))
        S.blit(s2, (self.W // 2 + self.W // 8 - s2.get_width() // 2, self.margin))

        # Paddles + ball
        pygame.draw.rect(S, P1_COLOR,
                         (self.p1_x, self.p1_y, self.paddle_w, self.paddle_h))
        pygame.draw.rect(S, P2_COLOR,
                         (self.p2_x, self.p2_y, self.paddle_w, self.paddle_h))
        pygame.draw.rect(S, WHITE,
                         (self.ball_x, self.ball_y, self.ball_size, self.ball_size))

        # Overlays
        if self.winner is not None:
            who = "CONTROLLER 1 WINS" if self.winner == 0 else "CONTROLLER 2 WINS"
            col = P1_COLOR if self.winner == 0 else P2_COLOR
            self._center_text(who, col, self.fnt_big, dy=-self.H // 8)
            self._center_text("START / Enter to play again", LGREY,
                              self.fnt_hint, dy=self.H // 12)
        elif self.serving:
            self._center_text("START / Enter to serve", WHITE,
                              self.fnt_big, dy=-self.H // 10)

        # Footer hint
        self._t("C1: left stick / D-pad   C2: right stick / D-pad   ESC Back",
                self.margin, self.H - self.fnt_hint.get_linesize() - 2,
                CYAN, self.fnt_hint, max_w=self.W - 2 * self.margin)

        fb_write(S, self.fb)

    def _t(self, txt, x, y, col, fnt, max_w=None):
        txt = str(txt)
        if max_w:
            while txt and fnt.size(txt)[0] > max_w:
                txt = txt[:-1]
        self.screen.blit(fnt.render(txt, True, col), (x, y))

    def _center_text(self, txt, col, fnt, dy=0):
        surf = fnt.render(txt, True, col)
        self.screen.blit(surf, ((self.W - surf.get_width()) // 2,
                                (self.H - surf.get_height()) // 2 + dy))

    # ------------------------------------------------------------------
    def run(self):
        clock = pygame.time.Clock()
        last = time.monotonic()
        try:
            while True:
                # Keyboard input (fallback / dev convenience)
                key_move1 = key_move2 = 0.0
                for event in pygame.event.get():
                    if event.type == pygame.QUIT:
                        return
                    if event.type == pygame.KEYDOWN:
                        if event.key in (pygame.K_ESCAPE, pygame.K_q):
                            return
                        if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER,
                                         pygame.K_SPACE):
                            self._serve()

                keys = pygame.key.get_pressed()
                if keys[pygame.K_w]:            key_move1 -= 1
                if keys[pygame.K_s]:            key_move1 += 1
                if keys[pygame.K_UP]:           key_move2 -= 1
                if keys[pygame.K_DOWN]:         key_move2 += 1

                self._ensure_joysticks()
                move1, start1 = self._player_input(0)
                move2, start2 = self._player_input(1)
                if start1 or start2:
                    self._serve()

                # Combine controller + keyboard, clamp to [-1, 1].
                m1 = max(-1.0, min(1.0, move1 + key_move1))
                m2 = max(-1.0, min(1.0, move2 + key_move2))

                now = time.monotonic()
                dt = min(0.05, now - last)  # clamp to avoid tunneling on hitches
                last = now

                self._update(dt, m1, m2)
                self._draw()
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
        PongApp(_load_profile()).run()
    except KeyboardInterrupt:
        pass
    finally:
        pygame.quit()
