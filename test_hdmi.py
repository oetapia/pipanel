"""
Minimal display test for HDMI output (1080p, KMSDRM).
Press any key to exit.
"""
import os
import pygame

os.environ["SDL_VIDEODRIVER"] = "KMSDRM"

W, H = 1920, 1080

pygame.init()
screen = pygame.display.set_mode((W, H))
pygame.mouse.set_visible(False)

colors = [(255,0,0),(0,255,0),(0,0,255),(255,255,0),(0,255,255),(255,0,255),(255,255,255)]
bar_w = W // len(colors)
screen.fill((0, 0, 0))
for i, c in enumerate(colors):
    pygame.draw.rect(screen, c, (i * bar_w, 0, bar_w, H // 3))

fnt = pygame.font.SysFont(None, 96)
screen.blit(fnt.render("HDMI OK  1920x1080", True, (255, 255, 255)), (60, H//3 + 60))
screen.blit(fnt.render("KMSDRM driver",      True, (180, 180, 180)), (60, H//3 + 180))
screen.blit(fnt.render("press any key to exit", True, (100, 200, 100)), (60, H//3 + 300))

pygame.display.flip()

clock = pygame.time.Clock()
while True:
    for event in pygame.event.get():
        if event.type in (pygame.QUIT, pygame.KEYDOWN):
            pygame.quit()
            raise SystemExit
    clock.tick(10)
