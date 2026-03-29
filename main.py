import argparse
import json
import os
import select
import sys
import termios
import time
import tty
import numpy as np
import pygame

_ROOT = os.path.dirname(os.path.abspath(__file__))

def _load_profiles():
    with open(os.path.join(_ROOT, "profiles.json")) as f:
        return json.load(f)

def _parse_args(profile_names):
    parser = argparse.ArgumentParser()
    parser.add_argument("--screen", default=profile_names[0],
                        choices=profile_names,
                        help=f"Profile name ({', '.join(profile_names)})")
    return parser.parse_args()

os.environ["SDL_VIDEODRIVER"] = "offscreen"
os.environ["SDL_AUDIODRIVER"] = "dummy"

APPS = [
    {"name": "MUSIC PLAYER", "description": "Volumio controls"},
    {"name": "WEATHER",      "description": "City weather info"},
]

BLACK  = (0,   0,   0)
WHITE  = (255, 255, 255)
YELLOW = (255, 220, 0)
CYAN   = (0,   200, 200)
GREY   = (80,  80,  80)
HLBG   = (20,  20,  60)


def fb_write(surface, fb):
    raw = pygame.surfarray.array3d(surface).transpose(1, 0, 2)
    r = (raw[:, :, 0].astype(np.uint16) >> 3) << 11
    g = (raw[:, :, 1].astype(np.uint16) >> 2) << 5
    b =  raw[:, :, 2].astype(np.uint16) >> 3
    with open(fb, "wb") as f:
        f.write((r | g | b).astype(np.uint16).tobytes())


def _read_key():
    if not select.select([sys.stdin], [], [], 0)[0]:
        return None
    ch = sys.stdin.read(1)
    if ch == '\x1b' and select.select([sys.stdin], [], [], 0.05)[0]:
        ch += sys.stdin.read(2)
    return ch


def run():
    profiles     = _load_profiles()
    profile_names = list(profiles.keys())
    args         = _parse_args(profile_names)

    screen_idx = profile_names.index(args.screen)
    selected   = 0

    pygame.init()

    fd  = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setraw(fd)

    try:
        while True:
            # Load current profile
            profile_name = profile_names[screen_idx]
            P  = profiles[profile_name]
            M  = P["main"]
            FB = P["sdl"]["fbdev"]
            W, H = P["screen"]["w"], P["screen"]["h"]

            pygame.display.quit()
            pygame.display.init()
            screen = pygame.display.set_mode((W, H))
            pygame.mouse.set_visible(False)

            fnt_title = pygame.font.SysFont(None, M["fonts"]["title"])
            fnt_name  = pygame.font.SysFont(None, M["fonts"]["name"])
            fnt_desc  = pygame.font.SysFont(None, M["fonts"]["desc"])
            fnt_hint  = pygame.font.SysFont(None, M["fonts"]["hint"])

            def draw(countdown=None):
                screen.fill(BLACK)
                # Title + screen name
                screen.blit(fnt_title.render("Apps", True, YELLOW),
                            (M["title_x"], M["title_y"]))
                screen_label = fnt_desc.render(f"[Tab] {profile_name}", True, GREY)
                screen.blit(screen_label,
                            (W - screen_label.get_width() - M["title_x"], M["title_y"] + 4))
                pygame.draw.line(screen, YELLOW,
                                 (M["title_x"], M["divider_y"]),
                                 (W - M["title_x"], M["divider_y"]), 1)

                y = M["apps_y"]
                for i, app in enumerate(APPS):
                    if i == selected:
                        pygame.draw.rect(screen, HLBG,
                                         (M["highlight_x_pad"], y - 6,
                                          W - M["highlight_w_pad"], M["highlight_h"]))
                        name_col = WHITE
                        prefix   = ">"
                    else:
                        name_col = GREY
                        prefix   = " "
                    screen.blit(fnt_name.render(f"{prefix} {app['name']}", True, name_col),
                                (M["title_x"], y))
                    screen.blit(fnt_desc.render(app["description"], True, CYAN),
                                (M["desc_x_indent"], y + M["desc_y_offset"]))
                    y += M["app_item_h"]

                pygame.draw.line(screen, GREY,
                                 (0, H - M["hint_line_offset"]),
                                 (W, H - M["hint_line_offset"]), 1)
                hint = "↑↓ Select   Enter Launch   Tab Screen   ESC Quit"
                if countdown is not None:
                    hint += f"   [{countdown}s]"
                screen.blit(fnt_hint.render(hint, True, CYAN),
                    (M["title_x"], H - M["hint_text_offset"]))
                fb_write(screen, FB)

            last_countdown = 10
            draw(last_countdown)

            launch       = None
            running      = True
            clock        = pygame.time.Clock()
            deadline     = time.monotonic() + 10
            switch_screen = False

            while running:
                key = _read_key()
                if key is not None:
                    deadline = time.monotonic() + 10
                    if key in ('\x1b', 'q', 'Q'):
                        return
                    elif key == '\x1b[A':
                        selected = (selected - 1) % len(APPS)
                        last_countdown = 10
                        draw(last_countdown)
                    elif key == '\x1b[B':
                        selected = (selected + 1) % len(APPS)
                        last_countdown = 10
                        draw(last_countdown)
                    elif key == '\t':
                        screen_idx    = (screen_idx + 1) % len(profile_names)
                        switch_screen = True
                        running       = False
                    elif key in ('\r', '\n'):
                        launch  = selected
                        running = False
                elif time.monotonic() >= deadline:
                    launch  = 0
                    running = False
                else:
                    remaining = max(0, int(deadline - time.monotonic()))
                    if remaining != last_countdown:
                        last_countdown = remaining
                        draw(last_countdown)
                clock.tick(30)

            if switch_screen:
                continue  # restart outer loop with new profile

            if launch == 0:
                _launch_volumio(P, FB)
            elif launch == 1:
                _launch_weather(P, FB)

    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _launch_volumio(P, FB):
    import threading
    from apps.volumio import socket_thread, Display
    threading.Thread(target=socket_thread, daemon=True).start()
    Display(P).run()


def _launch_weather(P, FB):
    from apps.weather import WeatherApp
    WeatherApp(P, FB).run()


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        pass
    finally:
        if pygame.get_init():
            pygame.quit()
        print("Stopped.")
