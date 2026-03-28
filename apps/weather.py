import io
import json
import os
import time
import threading
import requests
import pygame

def _load_profile():
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(_root, "profiles.json")) as f:
        profiles = json.load(f)
    return profiles[os.environ.get("PIPANEL_PROFILE", "35panel")]

_P  = _load_profile()
_W  = _P["weather"]
_sdl = _P["sdl"]

os.environ.setdefault("SDL_VIDEODRIVER", _sdl["videodriver"])
if _sdl.get("fbdev"):
    os.environ.setdefault("SDL_FBDEV", _sdl["fbdev"])

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

SCREEN_W, SCREEN_H = _P["screen"]["w"], _P["screen"]["h"]
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


class WeatherApp:
    def __init__(self):
        pygame.init()
        self.screen = pygame.display.set_mode((SCREEN_W, SCREEN_H))
        pygame.display.set_caption("Weather")
        pygame.mouse.set_visible(False)

        self.fnt_xl = pygame.font.SysFont(None, _W["fonts"]["xl"])
        self.fnt_lg = pygame.font.SysFont(None, _W["fonts"]["lg"])
        self.fnt_md = pygame.font.SysFont(None, _W["fonts"]["md"])
        self.fnt_sm = pygame.font.SysFont(None, _W["fonts"]["sm"])

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
                icon = pygame.transform.scale(surf, (_W["icon_size"], _W["icon_size"]))
                with self._lock:
                    self._icon = icon
            except Exception:
                pass

        # Header
        self._t("WEATHER", _W["header_x"], _W["header_y"], YELLOW, self.fnt_lg)
        self._t(self.city.upper(), _W["city_x"], _W["city_y"], WHITE, self.fnt_lg)
        pygame.draw.line(self.screen, YELLOW, (0, _W["header_line_y"]), (SCREEN_W, _W["header_line_y"]), 1)

        if loading and not data:
            self._t("Loading...", _W["header_x"], _W["temp_y"], LGREY)
        elif error and not data:
            self._t(error, _W["header_x"], _W["temp_y"], RED, self.fnt_sm, max_w=SCREEN_W - _W["header_x"] * 2)
        elif data:
            cur = data["current"]
            loc = data["location"]

            # Big temperature
            self._t(f"{cur['temp_c']}°C", _W["temp_x"], _W["temp_y"], GREEN, self.fnt_xl)

            # Condition
            self._t(cur["condition"]["text"], _W["condition_x"], _W["condition_y"], CYAN, self.fnt_md, max_w=_W["condition_max_w"])

            # Details
            y = _W["details_y"]
            for label, val in [
                ("Feels like", f"{cur['feelslike_c']}°C"),
                ("Wind",       f"{cur['wind_kph']} km/h"),
                ("Humidity",   f"{cur['humidity']}%"),
                ("Local time", loc["localtime"].split()[-1]),
            ]:
                self._t(label, _W["details_label_x"], y, LGREY, self.fnt_sm)
                self._t(val,   _W["details_val_x"],   y, WHITE, self.fnt_sm)
                y += _W["detail_step"]

            # Icon (right side)
            if icon:
                self.screen.blit(icon, (SCREEN_W - _W["icon_x_from_right"], _W["icon_y"]))

        # Footer
        pygame.draw.line(self.screen, GREY, (0, SCREEN_H - _W["footer_line_offset"]), (SCREEN_W, SCREEN_H - _W["footer_line_offset"]), 1)
        self._t("↑↓ City   R Refresh   ESC Back", _W["header_x"], SCREEN_H - _W["footer_text_offset"], CYAN, self.fnt_sm)

        pygame.display.flip()

    # ------------------------------------------------------------------
    def run(self):
        clock = pygame.time.Clock()
        while True:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    return
                elif event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_ESCAPE:
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
    try:
        WeatherApp().run()
    except KeyboardInterrupt:
        pass
    finally:
        pygame.quit()
