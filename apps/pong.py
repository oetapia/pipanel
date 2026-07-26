"""
Two-player Pong — rendered to the pipanel framebuffer.

Controls (one controller per player, per the Pi's two-device enumeration):
  * Controller 1 -> LEFT paddle   (left stick Y, or D-pad up/down)
  * Controller 2 -> RIGHT paddle  (left stick Y, or D-pad up/down)
  * Either START (button 9)       -> serve / restart after a point
  * Keyboard fallback: W/S left, ↑/↓ right, Enter serve, ESC/Q quit

Controller enumeration/mapping comes from apps/controller_profile.py:
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

# Shared controller profile (device enumeration + index mapping). Works as a
# package submodule (apps.pong) and as a standalone script.
if __package__:
    from .controller_profile import ControllerProfile
else:
    from controller_profile import ControllerProfile

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

        self.pads = ControllerProfile()

        self._reset_match()

    # ------------------------------------------------------------------
    # Controller handling (delegated to the shared controller profile)
    # ------------------------------------------------------------------
    def _player_input(self, idx):
        """Return (-1..1 vertical intent, start_pressed) for player idx."""
        st = self.pads.read(idx)
        move = st.axes.get("LY", 0.0)
        hy = st.hat[1]
        if hy:
            move = -hy  # hat up (+1) should move paddle up (-y)
        return move, bool(st.buttons.get("START"))

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

        # Ball (remember previous X for swept collision so a fast ball can't
        # tunnel through the thin paddle in a single frame).
        prev_x = self.ball_x
        self.ball_x += self.ball_vx * dt
        self.ball_y += self.ball_vy * dt

        # Top/bottom walls
        if self.ball_y <= 0:
            self.ball_y = 0
            self.ball_vy = abs(self.ball_vy)
        elif self.ball_y + self.ball_size >= self.H:
            self.ball_y = self.H - self.ball_size
            self.ball_vy = -abs(self.ball_vy)

        # Paddle collisions. Left paddle: ball moving left, its collision face
        # is the paddle's right edge. Right paddle: ball moving right, face is
        # the paddle's left edge.
        if self.ball_vx < 0:
            self._bounce_paddle(self.p1_x + self.paddle_w, self.p1_y,
                                prev_x, is_left=True)
        elif self.ball_vx > 0:
            self._bounce_paddle(self.p2_x, self.p2_y,
                                prev_x, is_left=False)

        # Scoring
        if self.ball_x + self.ball_size < 0:
            self._point(scorer=1)
        elif self.ball_x > self.W:
            self._point(scorer=0)

    def _bounce_paddle(self, face_x, paddle_y, prev_x, is_left):
        size = self.ball_size
        py1, py2 = paddle_y, paddle_y + self.paddle_h

        # Swept crossing test along X: did the ball's leading edge pass the
        # paddle face this frame (accounts for a fast ball skipping over it)?
        if is_left:
            # Leading edge is the ball's left side, moving toward smaller x.
            prev_edge = prev_x
            new_edge  = self.ball_x
            crossed = prev_edge >= face_x and new_edge <= face_x
        else:
            # Leading edge is the ball's right side, moving toward larger x.
            prev_edge = prev_x + size
            new_edge  = self.ball_x + size
            crossed = prev_edge <= face_x and new_edge >= face_x
        if not crossed:
            return

        # Vertical overlap check at the ball's current Y.
        by1, by2 = self.ball_y, self.ball_y + size
        if by2 < py1 or by1 > py2:
            return

        # Reflect and add angle based on where it struck the paddle.
        rel = ((self.ball_y + size / 2) - (paddle_y + self.paddle_h / 2))
        rel /= (self.paddle_h / 2)  # -1..1
        rel = max(-1.0, min(1.0, rel))
        speed = min(self.ball_speed_max,
                    (self.ball_vx ** 2 + self.ball_vy ** 2) ** 0.5
                    * self.ball_speedup)
        direction = 1 if is_left else -1  # left paddle sends ball right
        angle = rel * (math.pi / 3.5)  # up to ~51°
        self.ball_vx = direction * speed * math.cos(angle)
        self.ball_vy = speed * math.sin(angle)
        # Place the ball just off the paddle face so we don't re-collide.
        if is_left:
            self.ball_x = face_x + 1
        else:
            self.ball_x = face_x - size - 1

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

                self.pads.refresh()
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
