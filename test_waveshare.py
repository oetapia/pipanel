"""
Minimal display test for the Waveshare 1.44" LCD HAT (128x128, ST7735S).
Renders offscreen with pygame and pushes the frame over SPI via the profile's
display sink — the HAT has no /dev/fbN, so nothing is written to a framebuffer.

Run on the Pi with SPI enabled (dtparam=spi=on):  sudo python3 test_waveshare.py
"""
import json
import os

import pygame

from apps.display import make_sink

PROFILE = os.environ.get("PIPANEL_PROFILE", "waveshare144")

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "profiles.json")) as f:
    P = json.load(f)[PROFILE]

W, H = P["screen"]["w"], P["screen"]["h"]

os.environ["SDL_VIDEODRIVER"] = "offscreen"
pygame.init()
screen = pygame.display.set_mode((W, H))

colors = [(255,0,0),(0,255,0),(0,0,255),(255,255,0),(0,255,255),(255,0,255),(255,255,255)]
bar_w = W // len(colors)
screen.fill((0, 0, 0))
for i, c in enumerate(colors):
    pygame.draw.rect(screen, c, (i * bar_w, 0, bar_w, H // 3))

fnt = pygame.font.SysFont(None, 16)
screen.blit(fnt.render(f"{PROFILE} OK", True, (255, 255, 255)), (6, H//3 + 8))
screen.blit(fnt.render(f"{W}x{H} RGB565",  True, (180, 180, 180)), (6, H//3 + 26))
screen.blit(fnt.render("SPI -> ST7735S",   True, (180, 180, 180)), (6, H//3 + 44))

sink = make_sink(P)
sink.write(screen)
print(f"Written to {sink} — check the display.")

# Deliberately no sink.close(): cleanup stops the backlight PWM, which would
# blank the panel we just drew to.
pygame.quit()
