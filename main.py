import argparse
import json
import os
import numpy as np
import pygame

def _load_profile(name):
    _root = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(_root, "profiles.json")) as f:
        profiles = json.load(f)
    if name not in profiles:
        raise SystemExit(f"Unknown profile '{name}'. Available: {', '.join(profiles)}")
    return profiles[name]

def _parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--screen", default="35panel", help="Profile name (e.g. 35panel, 1080TV)")
    return parser.parse_args()

_args = _parse_args()
_P   = _load_profile(_args.screen)
_M   = _P["main"]
_sdl = _P["sdl"]

os.environ["SDL_VIDEODRIVER"] = "offscreen"

SCREEN_W, SCREEN_H = _P["screen"]["w"], _P["screen"]["h"]
FB = _sdl["fbdev"]

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


def fb_write(surface):
    raw = pygame.surfarray.array3d(surface).transpose(1, 0, 2)  # (H, W, 3)
    r = (raw[:, :, 0].astype(np.uint16) >> 3) << 11
    g = (raw[:, :, 1].astype(np.uint16) >> 2) << 5
    b =  raw[:, :, 2].astype(np.uint16) >> 3
    with open(FB, "wb") as f:
        f.write((r | g | b).astype(np.uint16).tobytes())


def run():
    selected = 0
    pygame.init()

    while True:
        screen = pygame.display.set_mode((SCREEN_W, SCREEN_H))
        pygame.mouse.set_visible(False)

        fnt_title = pygame.font.SysFont(None, _M["fonts"]["title"])
        fnt_name  = pygame.font.SysFont(None, _M["fonts"]["name"])
        fnt_desc  = pygame.font.SysFont(None, _M["fonts"]["desc"])
        fnt_hint  = pygame.font.SysFont(None, _M["fonts"]["hint"])

        def draw():
            screen.fill(BLACK)
            screen.blit(fnt_title.render("Apps", True, YELLOW), (_M["title_x"], _M["title_y"]))
            pygame.draw.line(screen, YELLOW, (_M["title_x"], _M["divider_y"]), (SCREEN_W - _M["title_x"], _M["divider_y"]), 1)

            y = _M["apps_y"]
            for i, app in enumerate(APPS):
                if i == selected:
                    pygame.draw.rect(screen, HLBG, (_M["highlight_x_pad"], y - 6, SCREEN_W - _M["highlight_w_pad"], _M["highlight_h"]))
                    name_col = WHITE
                    prefix   = ">"
                else:
                    name_col = GREY
                    prefix   = " "
                screen.blit(fnt_name.render(f"{prefix} {app['name']}", True, name_col), (_M["title_x"], y))
                screen.blit(fnt_desc.render(app["description"], True, CYAN), (_M["desc_x_indent"], y + _M["desc_y_offset"]))
                y += _M["app_item_h"]

            pygame.draw.line(screen, GREY, (0, SCREEN_H - _M["hint_line_offset"]), (SCREEN_W, SCREEN_H - _M["hint_line_offset"]), 1)
            screen.blit(fnt_hint.render("↑↓ Select   Enter Launch   ESC Quit", True, CYAN),
                        (_M["title_x"], SCREEN_H - _M["hint_text_offset"]))
            fb_write(screen)

        draw()

        launch  = None
        running = True
        clock   = pygame.time.Clock()

        while running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    pygame.quit()
                    return
                elif event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_ESCAPE:
                        pygame.quit()
                        return
                    elif event.key == pygame.K_UP:
                        selected = (selected - 1) % len(APPS)
                        draw()
                    elif event.key == pygame.K_DOWN:
                        selected = (selected + 1) % len(APPS)
                        draw()
                    elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                        launch  = selected
                        running = False
            clock.tick(30)

        if launch == 0:
            _launch_volumio()
        elif launch == 1:
            _launch_weather()


def _launch_volumio():
    import threading
    from apps.volumio import socket_thread, Display
    threading.Thread(target=socket_thread, daemon=True).start()
    Display().run()


def _launch_weather():
    from apps.weather import WeatherApp
    WeatherApp().run()


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        pass
    finally:
        if pygame.get_init():
            pygame.quit()
        print("Stopped.")
