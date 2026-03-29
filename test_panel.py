"""
Minimal display test for the 35panel (SPI TFT, /dev/fb1).
Renders offscreen with pygame, converts to RGB565, writes directly to /dev/fb1.
"""
import os
import pygame
import numpy as np

FB = "/dev/fb1"
W, H = 480, 320

os.environ["SDL_VIDEODRIVER"] = "offscreen"
pygame.init()
screen = pygame.display.set_mode((W, H))

def fb_write(surface):
    """Convert pygame RGB888 surface to RGB565 and write to framebuffer."""
    raw = pygame.surfarray.array3d(surface).transpose(1, 0, 2)  # (H, W, 3)
    r = (raw[:, :, 0].astype(np.uint16) >> 3) << 11
    g = (raw[:, :, 1].astype(np.uint16) >> 2) << 5
    b =  raw[:, :, 2].astype(np.uint16) >> 3
    with open(FB, "wb") as f:
        f.write((r | g | b).astype(np.uint16).tobytes())

# Draw test pattern
colors = [(255,0,0),(0,255,0),(0,0,255),(255,255,0),(0,255,255),(255,0,255),(255,255,255)]
bar_w = W // len(colors)
screen.fill((0, 0, 0))
for i, c in enumerate(colors):
    pygame.draw.rect(screen, c, (i * bar_w, 0, bar_w, H // 3))

fnt = pygame.font.SysFont(None, 36)
screen.blit(fnt.render("35panel OK  480x320", True, (255, 255, 255)), (20, H//3 + 20))
screen.blit(fnt.render("fbdev -> /dev/fb1",   True, (180, 180, 180)), (20, H//3 + 60))

fb_write(screen)
print("Written to /dev/fb1 — check the display.")
pygame.quit()
