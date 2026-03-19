import os
import sys
import pygame

os.environ.setdefault("SDL_VIDEODRIVER", "fbcon")
os.environ.setdefault("SDL_FBDEV", "/dev/fb1")

SCREEN_W, SCREEN_H = 480, 320

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


def run():
    selected = 0
    pygame.init()

    while True:
        screen = pygame.display.set_mode((SCREEN_W, SCREEN_H))
        pygame.display.set_caption("pipanel")
        pygame.mouse.set_visible(False)

        fnt_title = pygame.font.SysFont(None, 40)
        fnt_name  = pygame.font.SysFont(None, 30)
        fnt_desc  = pygame.font.SysFont(None, 22)
        fnt_hint  = pygame.font.SysFont(None, 20)

        def draw():
            screen.fill(BLACK)
            screen.blit(fnt_title.render("Apps", True, YELLOW), (20, 16))
            pygame.draw.line(screen, YELLOW, (20, 50), (SCREEN_W - 20, 50), 1)

            y = 80
            for i, app in enumerate(APPS):
                if i == selected:
                    pygame.draw.rect(screen, HLBG, (10, y - 6, SCREEN_W - 20, 46))
                    name_col = WHITE
                    prefix   = ">"
                else:
                    name_col = GREY
                    prefix   = " "
                screen.blit(fnt_name.render(f"{prefix} {app['name']}", True, name_col), (20, y))
                screen.blit(fnt_desc.render(app["description"], True, CYAN), (44, y + 22))
                y += 70

            pygame.draw.line(screen, GREY, (0, SCREEN_H - 32), (SCREEN_W, SCREEN_H - 32), 1)
            screen.blit(fnt_hint.render("↑↓ Select   Enter Launch   ESC Quit", True, CYAN),
                        (20, SCREEN_H - 22))
            pygame.display.flip()

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
