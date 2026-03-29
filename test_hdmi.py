"""
Minimal display test for HDMI output (1080p).
Renders offscreen with pygame, converts to RGB565, writes directly to /dev/fb0.
"""
import os
import pygame
import numpy as np

FB = "/dev/fb0"
W, H = 1920, 1080

os.environ["SDL_VIDEODRIVER"] = "offscreen"
pygame.init()
screen = pygame.display.set_mode((W, H))

def fb_write(surface):
    raw = pygame.surfarray.array3d(surface).transpose(1, 0, 2)  # (H, W, 3)
    r = (raw[:, :, 0].astype(np.uint16) >> 3) << 11
    g = (raw[:, :, 1].astype(np.uint16) >> 2) << 5
    b =  raw[:, :, 2].astype(np.uint16) >> 3
    with open(FB, "wb") as f:
        f.write((r | g | b).astype(np.uint16).tobytes())

colors = [(255,0,0),(0,255,0),(0,0,255),(255,255,0),(0,255,255),(255,0,255),(255,255,255)]
bar_w = W // len(colors)
screen.fill((0, 0, 0))
for i, c in enumerate(colors):
    pygame.draw.rect(screen, c, (i * bar_w, 0, bar_w, H // 3))

fnt = pygame.font.SysFont(None, 96)
screen.blit(fnt.render("HDMI OK  1920x1080", True, (255, 255, 255)), (60, H//3 + 60))
screen.blit(fnt.render("offscreen -> /dev/fb0", True, (180, 180, 180)), (60, H//3 + 180))

fb_write(screen)
print("Written to /dev/fb0 — check the display.")
pygame.quit()
