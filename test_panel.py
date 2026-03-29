"""
Minimal display test for the 35panel (headless Pi, framebuffer).
Draws a test pattern on /dev/fb1 at 480x320.
Press any key to exit.
"""
import os
import sys

os.environ["SDL_VIDEODRIVER"] = "fbcon"
os.environ["SDL_FBDEV"]       = "/dev/fb1"

import pygame

W, H = 480, 320

pygame.init()
screen = pygame.display.set_mode((W, H))
pygame.mouse.set_visible(False)

screen.fill((0, 0, 0))

# Color bars to confirm display geometry
colors = [(255,0,0),(0,255,0),(0,0,255),(255,255,0),(0,255,255),(255,0,255),(255,255,255)]
bar_w = W // len(colors)
for i, c in enumerate(colors):
    pygame.draw.rect(screen, c, (i * bar_w, 0, bar_w, H // 3))

# Text
fnt = pygame.font.SysFont(None, 36)
screen.blit(fnt.render("35panel OK  480x320", True, (255,255,255)), (20, H//3 + 20))
screen.blit(fnt.render("fbcon  /dev/fb1",     True, (180,180,180)), (20, H//3 + 60))
screen.blit(fnt.render("press any key to exit", True, (100,200,100)), (20, H//3 + 100))

pygame.display.flip()

# Wait for any key or window close
clock = pygame.time.Clock()
while True:
    for event in pygame.event.get():
        if event.type in (pygame.QUIT, pygame.KEYDOWN):
            pygame.quit()
            sys.exit(0)
    clock.tick(10)
