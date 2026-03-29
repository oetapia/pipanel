import json
import os
import re
import sys
import time
import threading
import socketio
import requests
import pygame

# --- Profile ---
def _load_profile():
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(_root, "profiles.json")) as f:
        profiles = json.load(f)
    return profiles[os.environ.get("PIPANEL_PROFILE", "35panel")]

_P  = _load_profile()
_V  = _P["volumio"]
_sdl = _P["sdl"]

# --- Display setup ---
os.environ.setdefault("SDL_VIDEODRIVER", _sdl["videodriver"])
if _sdl.get("fbdev"):
    os.environ.setdefault("SDL_FBDEV", _sdl["fbdev"])

# --- Config ---
VOLUMIO_HOST = "http://volumio.local"
GENIUS_URL   = "http://volumio.local:4000/api/genius"
LRCLIB_URL   = "http://volumio.local:4000/api/lrclib"
TIDAL_URL    = "http://volumio.local:4000/api/tidal"

SCREEN_W, SCREEN_H = _P["screen"]["w"], _P["screen"]["h"]

# Column layout
COL1_X, COL1_W = _V["col1_x"], _V["col1_w"]   # Lyrics
COL2_X, COL2_W = _V["col2_x"], _V["col2_w"]   # Genius
DIV_COL  = _V["div_col"]                        # Vertical divider
DIV_BAR  = _V["div_bar"]                        # Horizontal divider above status bar

# Status bar
BAR_H    = SCREEN_H - DIV_BAR - 1
BAR_Y1   = DIV_BAR + _V["bar_y1_offset"]
BAR_Y2   = DIV_BAR + _V["bar_y2_offset"]

# Lyrics
LYRIC_TOP    = _V["lyric_top"]
LYRIC_LINE_H = _V["lyric_line_h"]
LYRIC_VISIBLE = int((DIV_BAR - LYRIC_TOP) / LYRIC_LINE_H)
LYRIC_CENTRE  = LYRIC_VISIBLE // 2

# --- Colours ---
BLACK    = (0,   0,   0)
WHITE    = (255, 255, 255)
YELLOW   = (255, 220, 0)
GREEN    = (0,   200, 0)
RED      = (220, 0,   0)
CYAN     = (0,   200, 200)
GREY     = (80,  80,  80)
LGREY    = (150, 150, 150)
ORANGE   = (255, 165, 0)
PURPLE   = (170, 90,  255)
DIMWHITE = (180, 180, 180)
HLBG     = (25,  25,  70)


# ===================================================================
# Shared state
# ===================================================================
def make_state(**defaults):
    defaults["dirty"] = True
    return defaults, threading.Lock()

vol_state, vol_lock = make_state(
    title="Waiting...", artist="", album="",
    bitrate="", status="stop", volume=0,
    seek=0, connected=False, error="", uri=""
)
gen_state, gen_lock = make_state(
    year="", samples=[], sampled_in=[],
    loading=False, error="", last_key=""
)
lyr_state, lyr_lock = make_state(
    lines=[], plain="", current_ms=0,
    loading=False, error="", last_key=""
)
queue_state, queue_lock = make_state(
    items=[], position=0
)
# "genius" | "similar" | "album"
tidal_state, tidal_lock = make_state(
    mode="genius", tracks=[], loading=False, error="",
    show_lyrics=True, show_right=True
)

def update(lock, state, **kwargs):
    with lock:
        for k, v in kwargs.items():
            state[k] = v
        state["dirty"] = True

def snapshot(lock, state):
    with lock:
        state["dirty"] = False
        return dict(state)

def is_dirty(lock, state):
    with lock:
        return state["dirty"]


# ===================================================================
# Seek ticker
# ===================================================================
def seek_tick():
    while True:
        time.sleep(1)
        with vol_lock:
            if vol_state["status"] == "play":
                vol_state["seek"] = vol_state.get("seek", 0) + 1000
                vol_state["dirty"] = True
        with lyr_lock:
            with vol_lock:
                ms = vol_state.get("seek", 0)
            if vol_state["status"] == "play":
                lyr_state["current_ms"] = ms
                lyr_state["dirty"] = True

threading.Thread(target=seek_tick, daemon=True).start()


# ===================================================================
# Genius fetch
# ===================================================================
def norm(s):
    s = s.lower()
    s = re.sub(r'\s*\(.*?\)\s*', ' ', s)
    s = re.sub(r'[^\w\s]', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()

def sort_hits(hits, title, artist):
    nt, na = norm(title), norm(artist)
    scored = []
    for h in hits:
        ht = norm(h["result"]["title"])
        ha = norm(h["result"]["primary_artist"]["name"])
        score = 0
        if ha == na: score += 3
        elif na in ha or ha in na: score += 1
        words = [w for w in nt.split() if len(w) > 2]
        if words:
            score += sum(1 for w in words if w in ht) / len(words) * 2
        scored.append((score, h))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [h for _, h in scored]

def fetch_genius(title, artist):
    update(gen_lock, gen_state, loading=True, error="", year="", samples=[], sampled_in=[])
    try:
        r = requests.get(f"{GENIUS_URL}/search",
                         params={"q": f"{title} {artist}"}, timeout=6)
        r.raise_for_status()
        hits = r.json().get("response", {}).get("hits", [])
        if not hits:
            update(gen_lock, gen_state, loading=False, error="Not found")
            return

        song_id = sort_hits(hits, title, artist)[0]["result"]["id"]
        r2 = requests.get(f"{GENIUS_URL}/songs", params={"q": song_id}, timeout=6)
        r2.raise_for_status()
        song = r2.json().get("response", {}).get("song", {})
        if not song:
            update(gen_lock, gen_state, loading=False, error="No detail")
            return

        year = song.get("release_date_for_display", "")
        samples, sampled_in = [], []
        for rel in song.get("song_relationships", []):
            if not rel.get("songs"):
                continue
            entries = [f"{s['title']} · {s['primary_artist']['name']}"
                       for s in rel["songs"]]
            if rel["type"] == "samples":
                samples = entries
            elif rel["type"] == "sampled_in":
                sampled_in = entries

        update(gen_lock, gen_state, loading=False, error="",
               year=year, samples=samples, sampled_in=sampled_in)
    except Exception as e:
        update(gen_lock, gen_state, loading=False, error="Genius error")
        print(f"Genius: {e}")


# ===================================================================
# Lyrics fetch
# ===================================================================
def parse_lrc(synced):
    lines = []
    pattern = re.compile(r'\[(\d+):(\d+\.\d+)\](.*)')
    for raw in synced.split('\n'):
        m = pattern.match(raw.strip())
        if m:
            mins = int(m.group(1))
            secs = float(m.group(2))
            text = m.group(3).strip()
            lines.append({"time_ms": int((mins * 60 + secs) * 1000), "text": text})
    return lines

def fetch_lyrics(title, artist, album, duration):
    update(lyr_lock, lyr_state, loading=True, error="", lines=[], plain="")
    try:
        params = {"track_name": title, "artist_name": artist}
        if album:    params["album_name"] = album
        if duration: params["duration"]   = duration
        r = requests.get(f"{LRCLIB_URL}/search", params=params, timeout=6)
        r.raise_for_status()
        data = r.json()

        if not isinstance(data, list) or not data:
            update(lyr_lock, lyr_state, loading=False, error="No lyrics found")
            return

        hit = data[0]
        if hit.get("syncedLyrics"):
            lines = parse_lrc(hit["syncedLyrics"])
            update(lyr_lock, lyr_state, loading=False, error="", lines=lines)
        elif hit.get("plainLyrics"):
            lines = [{"time_ms": -1, "text": l}
                     for l in hit["plainLyrics"].split('\n')]
            update(lyr_lock, lyr_state, loading=False, error="", lines=lines)
        else:
            update(lyr_lock, lyr_state, loading=False, error="No lyrics found")
    except Exception as e:
        update(lyr_lock, lyr_state, loading=False, error="Lyrics error")
        print(f"Lyrics: {e}")

# ===================================================================
# Tidal fetch
# ===================================================================
def tidal_track_id():
    """Extract numeric Tidal track ID from Volumio URI (e.g. 'tidal/track/94401408')."""
    with vol_lock:
        uri = vol_state.get("uri", "")
    m = re.search(r'(\d+)$', uri)
    return m.group(1) if m else None

def _parse_tidal_tracks(json_data):
    """Extract {id, label} dicts from a Tidal relationship response."""
    included = {item["id"]: item for item in json_data.get("included", [])}
    tracks = []
    for item in json_data.get("data", []):
        tid = item.get("id", "")
        attrs = included.get(tid, {}).get("attributes", {})
        title = attrs.get("title") or tid
        artists = attrs.get("artists", [])
        artist = artists[0].get("name", "") if artists else ""
        label = title + (f" · {artist}" if artist else "")
        tracks.append({"id": tid, "label": label, "title": title, "artist": artist})
    return tracks

def queue_tidal_tracks(tracks):
    """Emit addToQueue for each track dict via Volumio socket."""
    with vol_lock:
        uri_template = vol_state.get("uri", "")
    for track in tracks:
        uri = re.sub(r'\d+$', track["id"], uri_template) if re.search(r'\d+$', uri_template) \
              else f"tidal://track/{track['id']}"
        safe_emit("addToQueue", {
            "uri": uri,
            "service": "tidal",
            "title": track["title"],
            "artist": track["artist"],
            "type": "song",
        })


def fetch_similar_tracks(track_id):
    update(tidal_lock, tidal_state, loading=True, error="", tracks=[], mode="similar", show_right=True)
    try:
        r = requests.get(f"{TIDAL_URL}/similar-tracks", params={"trackId": track_id}, timeout=8)
        r.raise_for_status()
        tracks = _parse_tidal_tracks(r.json())
        update(tidal_lock, tidal_state, loading=False, tracks=tracks,
               error="" if tracks else "No similar tracks")
    except Exception as e:
        update(tidal_lock, tidal_state, loading=False, error="Tidal error")
        print(f"Tidal similar: {e}")

def fetch_album_tracks(track_id):
    update(tidal_lock, tidal_state, loading=True, error="", tracks=[], mode="album", show_right=True)
    try:
        r = requests.get(f"{TIDAL_URL}/album-tracks", params={"trackId": track_id}, timeout=8)
        r.raise_for_status()
        tracks = _parse_tidal_tracks(r.json())
        update(tidal_lock, tidal_state, loading=False, tracks=tracks,
               error="" if tracks else "No album tracks")
    except Exception as e:
        update(tidal_lock, tidal_state, loading=False, error="Tidal error")
        print(f"Tidal album: {e}")


def maybe_fetch(title, artist, album="", duration=None):
    key = f"{title}|{artist}"
    with gen_lock:
        last = gen_state["last_key"]
    if key == last:
        return
    with gen_lock:
        gen_state["last_key"] = key
    with lyr_lock:
        lyr_state["last_key"] = key
    threading.Thread(target=fetch_genius, args=(title, artist), daemon=True).start()
    threading.Thread(target=fetch_lyrics,
                     args=(title, artist, album, duration), daemon=True).start()


# ===================================================================
# Socket.IO
# ===================================================================
sio = socketio.Client(reconnection=True, reconnection_attempts=0)

@sio.event
def connect():
    update(vol_lock, vol_state, connected=True, error="")
    sio.emit("getState", "")
    sio.emit("getQueue", "")

@sio.event
def disconnect():
    update(vol_lock, vol_state, connected=False, error="Disconnected")

def safe_emit(event, data=""):
    with vol_lock:
        connected = vol_state.get("connected", False)
    if connected:
        try:
            sio.emit(event, data)
        except Exception:
            pass

@sio.on("pushState")
def on_push_state(data):
    title    = data.get("title",    "")
    artist   = data.get("artist",   "")
    album    = data.get("album",    "")
    duration = data.get("duration", None)
    seek_raw = data.get("seek", 0) or 0
    seek_ms  = seek_raw * 1000 if seek_raw < 10000 else seek_raw
    status   = data.get("status", "stop")

    update(vol_lock, vol_state,
           title=title, artist=artist, album=album,
           bitrate=data.get("bitrate", ""),
           status=status, volume=data.get("volume", 0),
           seek=seek_ms, error="", uri=data.get("uri", ""))
    update(queue_lock, queue_state, position=data.get("position", 0) or 0)
    update(lyr_lock, lyr_state, current_ms=seek_ms)

    if title and artist and status == "play":
        maybe_fetch(title, artist, album, duration)

@sio.on("pushQueue")
def on_push_queue(data):
    items = data if isinstance(data, list) else []
    with vol_lock:
        pos = vol_state.get("position", 0) or 0
    update(queue_lock, queue_state, items=items, position=pos)

def socket_thread():
    while True:
        try:
            sio.connect(VOLUMIO_HOST, transports=["websocket"])
            sio.wait()
        except Exception as e:
            update(vol_lock, vol_state, connected=False, error="Cannot reach Volumio")
            print(f"Socket: {e}")
        time.sleep(5)


# ===================================================================
# Display
# ===================================================================
class Display:
    def __init__(self, P=None, FB=None):
        if P is None:
            P = _P
        V = P["volumio"]
        W, H = P["screen"]["w"], P["screen"]["h"]
        self.FB  = FB or _sdl.get("fbdev")
        self.W   = W
        self.H   = H
        self.COL1_X = V["col1_x"];  self.COL1_W = V["col1_w"]
        self.COL2_X = V["col2_x"];  self.COL2_W = V["col2_w"]
        self.DIV_COL = V["div_col"]
        self.DIV_BAR = V["div_bar"]
        self.BAR_Y1  = V["div_bar"] + V["bar_y1_offset"]
        self.BAR_Y2  = V["div_bar"] + V["bar_y2_offset"]
        self.LYRIC_TOP     = V["lyric_top"]
        self.LYRIC_LINE_H  = V["lyric_line_h"]
        self.LYRIC_VISIBLE = int((V["div_bar"] - V["lyric_top"]) / V["lyric_line_h"])
        self.LYRIC_CENTRE  = self.LYRIC_VISIBLE // 2
        self.QUEUE_ITEM_H  = V["queue_item_h"]

        pygame.display.quit()
        pygame.display.init()
        self.screen = pygame.display.set_mode((W, H))
        pygame.mouse.set_visible(False)
        self.fnt_lg  = pygame.font.SysFont(None, V["fonts"]["lg"])
        self.fnt_md  = pygame.font.SysFont(None, V["fonts"]["md"])
        self.fnt_sm  = pygame.font.SysFont(None, V["fonts"]["sm"])
        self.fnt_lyr = pygame.font.SysFont(None, V["fonts"]["lyr"])

    def _fb_write(self):
        import numpy as np
        raw = pygame.surfarray.array3d(self.screen).transpose(1, 0, 2)
        r = (raw[:, :, 0].astype(np.uint16) >> 3) << 11
        g = (raw[:, :, 1].astype(np.uint16) >> 2) << 5
        b =  raw[:, :, 2].astype(np.uint16) >> 3
        with open(self.FB, "wb") as f:
            f.write((r | g | b).astype(np.uint16).tobytes())

    def t(self, txt, x, y, col, fnt=None, max_w=None):
        fnt = fnt or self.fnt_md
        txt = str(txt)
        if max_w:
            while txt and fnt.size(txt)[0] > max_w:
                txt = txt[:-1]
        self.screen.blit(fnt.render(txt, True, col), (x, y))

    # ------------------------------------------------------------------
    def draw_lyrics(self, l):
        x, w = self.COL1_X + 4, self.COL1_W - 8

        if l["loading"]:
            self.t("Loading lyrics...", x, self.H // 2, LGREY, self.fnt_sm)
            return
        if not l["lines"]:
            if l["error"]:
                self.t(l["error"], x, self.H // 2, GREY, self.fnt_sm)
            return

        lines     = l["lines"]
        cur_ms    = l["current_ms"]
        is_synced = lines[0]["time_ms"] >= 0

        cur_idx = 0
        if is_synced:
            for i, ln in enumerate(lines):
                if ln["time_ms"] <= cur_ms:
                    cur_idx = i

        start   = max(0, cur_idx - self.LYRIC_CENTRE)
        visible = lines[start: start + self.LYRIC_VISIBLE]

        for i, ln in enumerate(visible):
            abs_idx = start + i
            is_cur  = (abs_idx == cur_idx) and is_synced
            col     = WHITE if is_cur else GREY
            fnt     = self.fnt_lg if is_cur else self.fnt_lyr
            line_y  = self.LYRIC_TOP + i * self.LYRIC_LINE_H

            if is_cur:
                pygame.draw.rect(self.screen, HLBG,
                                 (self.COL1_X, line_y - 2, self.COL1_W, self.LYRIC_LINE_H + 2))

            self.t(ln["text"] or " ", x, line_y, col, fnt, max_w=w)

    # ------------------------------------------------------------------
    def draw_genius(self, g):
        x, w = self.COL2_X + 4, self.COL2_W - 8
        y = 4

        self.t("GENIUS", x, y, PURPLE, self.fnt_md)
        pygame.draw.line(self.screen, GREY, (x, y + 18), (x + w, y + 18), 1)
        y += 24

        if g["loading"]:
            self.t("Searching...", x, y, LGREY, self.fnt_sm)
            return
        if g["error"]:
            self.t(g["error"], x, y, ORANGE, self.fnt_sm, max_w=w)
            return

        if g["year"]:
            self.t(g["year"], x, y, LGREY, self.fnt_sm)
            y += 18

        if g["samples"]:
            self.t("Samples:", x, y, YELLOW, self.fnt_sm)
            y += 16
            for s in g["samples"][:5]:
                self.t(s, x, y, DIMWHITE, self.fnt_sm, max_w=w)
                y += 14
                if y > self.DIV_BAR - 10: break

        if g["sampled_in"] and y < self.DIV_BAR - 10:
            y += 4
            self.t("Sampled in:", x, y, YELLOW, self.fnt_sm)
            y += 16
            for s in g["sampled_in"][:5]:
                self.t(s, x, y, DIMWHITE, self.fnt_sm, max_w=w)
                y += 14
                if y > self.DIV_BAR - 10: break

        if not g["samples"] and not g["sampled_in"] and not g["loading"] and not g["error"]:
            self.t("No samples", x, y, GREY, self.fnt_sm)

    # ------------------------------------------------------------------
    def draw_tidal(self, t):
        x, w = self.COL2_X + 4, self.COL2_W - 8
        y = 4

        label = "SIMILAR" if t["mode"] == "similar" else "ALBUM"
        self.t(label, x, y, CYAN, self.fnt_md)
        pygame.draw.line(self.screen, GREY, (x, y + 18), (x + w, y + 18), 1)
        y += 24

        if t["loading"]:
            self.t("Loading...", x, y, LGREY, self.fnt_sm)
            return
        if t["error"]:
            self.t(t["error"], x, y, ORANGE, self.fnt_sm, max_w=w)
            return
        for track in t["tracks"]:
            self.t(track["label"], x, y, DIMWHITE, self.fnt_sm, max_w=w)
            y += 14
            if y > self.DIV_BAR - 10:
                break

    # ------------------------------------------------------------------
    def draw_queue(self, q):
        x, w = self.COL1_X + 4, self.COL1_W - 8
        y = 4
        items   = q["items"]
        pos     = q["position"]

        self.t("QUEUE", x, y, LGREY, self.fnt_md)
        pygame.draw.line(self.screen, GREY, (x, y + 18), (x + w, y + 18), 1)
        y += 24

        if not items:
            self.t("Queue is empty", x, y, GREY, self.fnt_sm)
            return

        # Centre the current track, same logic as lyrics
        item_h   = self.QUEUE_ITEM_H
        visible  = int((self.DIV_BAR - y) / item_h)
        centre   = visible // 2
        start    = max(0, pos - centre)

        for i, item in enumerate(items[start: start + visible]):
            abs_idx = start + i
            is_cur  = abs_idx == pos
            if is_cur:
                pygame.draw.rect(self.screen, HLBG,
                                 (self.COL1_X, y - 2, self.COL1_W, item_h))

            title  = item.get("title",  "") or ""
            artist = item.get("artist", "") or ""
            col_t  = WHITE if is_cur else DIMWHITE
            col_a  = LGREY if is_cur else GREY
            self.t(title,  x, y,      col_t, self.fnt_sm, max_w=w)
            self.t(artist, x, y + 14, col_a, self.fnt_sm, max_w=w)
            y += item_h
            if y > self.DIV_BAR - 4:
                break

    # ------------------------------------------------------------------
    def draw_statusbar(self, v):
        x, w = 4, self.W - 8

        # Status icon
        if v["status"] == "play":
            icon, col = "▶", GREEN
        elif v["status"] == "pause":
            icon, col = "⏸", YELLOW
        else:
            icon, col = "■", RED
        self.t(icon, x, self.BAR_Y1, col, self.fnt_md)

        # Title · Artist · Album
        info = " · ".join(filter(None, [v["title"], v["artist"], v["album"]]))
        self.t(info, x + 20, self.BAR_Y1, WHITE, self.fnt_sm, max_w=w - 60)

        # Bitrate (right aligned)
        if v["bitrate"]:
            bw = self.fnt_sm.size(v["bitrate"])[0]
            self.t(v["bitrate"], self.W - bw - 4, self.BAR_Y1, GREY, self.fnt_sm)

        # Error / connection status on second line
        if not v["connected"]:
            self.t(v["error"] or "Connecting...", x, self.BAR_Y2, ORANGE, self.fnt_sm)

    # ------------------------------------------------------------------
    def draw(self, v, g, l, t, q):
        self.screen.fill(BLACK)

        pygame.draw.line(self.screen, GREY, (0, self.DIV_BAR), (self.W, self.DIV_BAR), 1)

        show_left = t["show_lyrics"]
        if show_left and t["show_right"]:
            pygame.draw.line(self.screen, GREY, (self.DIV_COL, 0), (self.DIV_COL, self.DIV_BAR), 1)

        if show_left:
            self.draw_lyrics(l)
        else:
            self.draw_queue(q)

        if t["show_right"]:
            if t["mode"] == "genius":
                self.draw_genius(g)
            else:
                self.draw_tidal(t)

        self.draw_statusbar(v)

        self._fb_write()

    def run(self):
        print("Display running. Ctrl+C to quit.")
        clock = pygame.time.Clock()
        v_snap = snapshot(vol_lock, vol_state)
        g_snap = snapshot(gen_lock, gen_state)
        l_snap = snapshot(lyr_lock, lyr_state)
        t_snap = snapshot(tidal_lock, tidal_state)
        q_snap = snapshot(queue_lock, queue_state)

        while True:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    return
                elif event.type == pygame.KEYDOWN:
                    k = event.key
                    if k == pygame.K_ESCAPE:
                        return
                    elif k == pygame.K_SPACE:
                        safe_emit("toggle")
                    elif k == pygame.K_RIGHT:
                        safe_emit("next")
                    elif k == pygame.K_LEFT:
                        safe_emit("prev")
                    elif k == pygame.K_UP:
                        with vol_lock:
                            vol = min(100, vol_state["volume"] + 5)
                        safe_emit("volume", vol)
                    elif k == pygame.K_DOWN:
                        with vol_lock:
                            vol = max(0, vol_state["volume"] - 5)
                        safe_emit("volume", vol)
                    elif k == pygame.K_l:
                        with tidal_lock:
                            tidal_state["show_lyrics"] = not tidal_state["show_lyrics"]
                            tidal_state["dirty"] = True
                    elif k == pygame.K_g:
                        with tidal_lock:
                            if tidal_state["show_right"] and tidal_state["mode"] == "genius":
                                tidal_state["show_right"] = False
                            else:
                                tidal_state["show_right"] = True
                                tidal_state["mode"] = "genius"
                            tidal_state["dirty"] = True
                    elif k == pygame.K_s:
                        tid = tidal_track_id()
                        if tid:
                            threading.Thread(target=fetch_similar_tracks,
                                             args=(tid,), daemon=True).start()
                        else:
                            update(tidal_lock, tidal_state,
                                   mode="similar", show_right=True, tracks=[],
                                   loading=False, error="No Tidal track ID")
                    elif k == pygame.K_a:
                        tid = tidal_track_id()
                        if tid:
                            threading.Thread(target=fetch_album_tracks,
                                             args=(tid,), daemon=True).start()
                        else:
                            update(tidal_lock, tidal_state,
                                   mode="album", show_right=True, tracks=[],
                                   loading=False, error="No Tidal track ID")
                    elif k == pygame.K_q:
                        with tidal_lock:
                            mode = tidal_state["mode"]
                            tracks = list(tidal_state["tracks"])
                        if mode in ("similar", "album") and tracks:
                            threading.Thread(target=queue_tidal_tracks,
                                             args=(tracks,), daemon=True).start()

            redraw = False
            if is_dirty(vol_lock, vol_state):
                v_snap = snapshot(vol_lock, vol_state)
                redraw = True
            if is_dirty(gen_lock, gen_state):
                g_snap = snapshot(gen_lock, gen_state)
                redraw = True
            if is_dirty(lyr_lock, lyr_state):
                l_snap = snapshot(lyr_lock, lyr_state)
                redraw = True
            if is_dirty(tidal_lock, tidal_state):
                t_snap = snapshot(tidal_lock, tidal_state)
                redraw = True
            if is_dirty(queue_lock, queue_state):
                q_snap = snapshot(queue_lock, queue_state)
                redraw = True

            if redraw:
                self.draw(v_snap, g_snap, l_snap, t_snap, q_snap)

            clock.tick(30)


# ===================================================================
# Main
# ===================================================================
if __name__ == "__main__":
    threading.Thread(target=socket_thread, daemon=True).start()
    try:
        Display().run()
    except KeyboardInterrupt:
        pass
    finally:
        pygame.quit()
        print("Stopped.")
