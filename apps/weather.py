import io
import json
import os
import time
import threading
import numpy as np
import requests
import pygame


# Load .env from project root (one level above this file)
def _load_env():
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip().strip("'\""))
    except FileNotFoundError:
        pass


_load_env()

WEATHER_API_KEY = os.environ.get("WEATHER_API_KEY", "")
BASE_URL = "http://api.weatherapi.com/v1/current.json"

CITIES = ["Berlin", "Quito", "Seoul", "Madrid", "Raleigh", "NYC", "Bali"]
UPDATE_INTERVAL = 300  # seconds

BLACK  = (0,   0,   0)
WHITE  = (255, 255, 255)
YELLOW = (255, 220, 0)
GREEN  = (0,   200, 0)
RED    = (220, 0,   0)
CYAN   = (0,   200, 200)
GREY   = (80,  80,  80)
LGREY  = (150, 150, 150)


def fb_write(surface, fb):
    raw = pygame.surfarray.array3d(surface).transpose(1, 0, 2)
    r = (raw[:, :, 0].astype(np.uint16) >> 3) << 11
    g = (raw[:, :, 1].astype(np.uint16) >> 2) << 5
    b =  raw[:, :, 2].astype(np.uint16) >> 3
    with open(fb, "wb") as f:
        f.write((r | g | b).astype(np.uint16).tobytes())


class WeatherApp:
    def __init__(self, P, FB=None):
        self.W = P["weather"]
        sdl = P["sdl"]

        self.screen_w = P["screen"]["w"]
        self.screen_h = P["screen"]["h"]
        self.fb = FB or sdl["fbdev"]

        os.environ["SDL_VIDEODRIVER"] = "offscreen"
        pygame.init()
        self.screen = pygame.display.set_mode((self.screen_w, self.screen_h))
        pygame.mouse.set_visible(False)

        self.fnt_xl = pygame.font.SysFont(None, self.W["fonts"]["xl"])
        self.fnt_lg = pygame.font.SysFont(None, self.W["fonts"]["lg"])
        self.fnt_md = pygame.font.SysFont(None, self.W["fonts"]["md"])
        self.fnt_sm = pygame.font.SysFont(None, self.W["fonts"]["sm"])

        self.cities     = CITIES
        self.city_index = 0
        self.city       = CITIES[0]

        self._lock         = threading.Lock()
        self._data         = None
        self._icon         = None
        self._icon_bytes   = None  # raw bytes, loaded to surface on main thread
        self._error        = ""
        self._loading      = True   # set before thread starts to avoid duplicate fetches
        self._last_update  = time.time()

        self._fetch_async()

    # ------------------------------------------------------------------
    def _fetch_async(self):
        threading.Thread(target=self._fetch, daemon=True).start()

    def _fetch(self):
        city = self.city
        with self._lock:
            self._loading = True
            self._error   = ""
        try:
            # Use (connect, read) timeout tuple — plain int doesn't cover DNS hangs
            r = requests.get(BASE_URL,
                             params={"key": WEATHER_API_KEY, "q": city, "aqi": "no"},
                             timeout=(5, 8))
            r.raise_for_status()
            result = r.json()

            icon_url = result["current"]["condition"]["icon"]
            if icon_url.startswith("//"):
                icon_url = "https:" + icon_url
            icon_bytes = None
            try:
                ir = requests.get(icon_url, timeout=(5, 5))
                ir.raise_for_status()
                icon_bytes = ir.content
            except Exception:
                pass

            with self._lock:
                self._data        = result
                self._icon        = None  # will be built on main thread
                self._icon_bytes  = icon_bytes
                self._loading     = False
                self._last_update = time.time()
        except Exception as e:
            with self._lock:
                self._loading = False
                self._error   = str(e)

    # ------------------------------------------------------------------
    def _t(self, txt, x, y, col, fnt=None, max_w=None):
        fnt = fnt or self.fnt_md
        txt = str(txt)
        if max_w:
            while txt and fnt.size(txt)[0] > max_w:
                txt = txt[:-1]
        self.screen.blit(fnt.render(txt, True, col), (x, y))

    def _draw(self):
        W = self.W
        SCREEN_W, SCREEN_H = self.screen_w, self.screen_h
        self.screen.fill(BLACK)

        with self._lock:
            data       = self._data
            loading    = self._loading
            error      = self._error
            icon       = self._icon
            icon_bytes = self._icon_bytes

        # Build icon surface on main thread (pygame is not thread-safe)
        if icon_bytes and icon is None:
            try:
                surf = pygame.image.load(io.BytesIO(icon_bytes))
                icon = pygame.transform.scale(surf, (W["icon_size"], W["icon_size"]))
                with self._lock:
                    self._icon = icon
            except Exception:
                pass

        # Header
        self._t("WEATHER", W["header_x"], W["header_y"], YELLOW, self.fnt_lg)
        self._t(self.city.upper(), W["city_x"], W["city_y"], WHITE, self.fnt_lg)
        pygame.draw.line(self.screen, YELLOW, (0, W["header_line_y"]), (SCREEN_W, W["header_line_y"]), 1)

        if loading and not data:
            self._t("Loading...", W["header_x"], W["temp_y"], LGREY)
        elif error and not data:
            self._t(error, W["header_x"], W["temp_y"], RED, self.fnt_sm, max_w=SCREEN_W - W["header_x"] * 2)
        elif data:
            cur = data["current"]
            loc = data["location"]

            # Big temperature
            self._t(f"{cur['temp_c']}°C", W["temp_x"], W["temp_y"], GREEN, self.fnt_xl)

            # Condition
            self._t(cur["condition"]["text"], W["condition_x"], W["condition_y"], CYAN, self.fnt_md, max_w=W["condition_max_w"])

            # Details
            y = W["details_y"]
            for label, val in [
                ("Feels like", f"{cur['feelslike_c']}°C"),
                ("Wind",       f"{cur['wind_kph']} km/h"),
                ("Humidity",   f"{cur['humidity']}%"),
                ("Local time", loc["localtime"].split()[-1]),
            ]:
                self._t(label, W["details_label_x"], y, LGREY, self.fnt_sm)
                self._t(val,   W["details_val_x"],   y, WHITE, self.fnt_sm)
                y += W["detail_step"]

            # Icon (right side)
            if icon:
                self.screen.blit(icon, (SCREEN_W - W["icon_x_from_right"], W["icon_y"]))

        # Footer
        pygame.draw.line(self.screen, GREY, (0, SCREEN_H - W["footer_line_offset"]), (SCREEN_W, SCREEN_H - W["footer_line_offset"]), 1)
        self._t("↑↓ City   R Refresh   ESC Back", W["header_x"], SCREEN_H - W["footer_text_offset"], CYAN, self.fnt_sm)

        fb_write(self.screen, self.fb)

    # ------------------------------------------------------------------
    def run(self):
        clock = pygame.time.Clock()
        while True:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    return
                elif event.type == pygame.KEYDOWN:
                    if event.key in (pygame.K_ESCAPE, pygame.K_q):
                        return
                    elif event.key in (pygame.K_DOWN, pygame.K_RIGHT):
                        self.city_index = (self.city_index + 1) % len(self.cities)
                        self.city = self.cities[self.city_index]
                        self._fetch_async()
                    elif event.key in (pygame.K_UP, pygame.K_LEFT):
                        self.city_index = (self.city_index - 1) % len(self.cities)
                        self.city = self.cities[self.city_index]
                        self._fetch_async()
                    elif event.key == pygame.K_r:
                        self._fetch_async()

            if time.time() - self._last_update > UPDATE_INTERVAL and not self._loading:
                self._fetch_async()

            self._draw()
            clock.tick(10)


if __name__ == "__main__":
    def _load_profile():
        _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(_root, "profiles.json")) as f:
            profiles = json.load(f)
        return profiles[os.environ.get("PIPANEL_PROFILE", "35panel")]

    try:
        WeatherApp(_load_profile()).run()
    except KeyboardInterrupt:
        pass
    finally:
        pygame.quit()
