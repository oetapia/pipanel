"""
Display output sinks for pipanel.

Apps render offscreen with pygame and hand the finished surface to a sink, so
the profile decides where those pixels land:

    "fbdev"             Linux framebuffer  (/dev/fb1 SPI TFT, /dev/fb0 HDMI)
    "waveshare_st7735"  Waveshare 1.44" LCD HAT (128x128, ST7735S) over SPI

The Waveshare HAT has no framebuffer device — its controller is driven directly
over SPI + GPIO — which is why the output had to become a sink instead of a
path. Profiles pick one with sdl.driver; that key defaults to "fbdev", so the
existing 35panel / 1080TV profiles behave exactly as before.
"""

import numpy as np
import pygame


def rgb565_bytes(surface, big_endian=False):
    """Pack an RGB888 pygame surface into RGB565, row-major."""
    raw = pygame.surfarray.array3d(surface).transpose(1, 0, 2)  # (H, W, 3)
    r = (raw[:, :, 0].astype(np.uint16) >> 3) << 11
    g = (raw[:, :, 1].astype(np.uint16) >> 2) << 5
    b =  raw[:, :, 2].astype(np.uint16) >> 3
    px = (r | g | b).astype('>u2' if big_endian else np.uint16)
    return px.tobytes()


class FramebufferSink:
    """Writes RGB565 straight to a Linux framebuffer device."""

    def __init__(self, path):
        self.path = path

    def write(self, surface):
        with open(self.path, "wb") as f:
            f.write(rgb565_bytes(surface))

    def close(self):
        pass

    def __str__(self):
        return f"fbdev {self.path}"


# --- Waveshare 1.44" LCD HAT (ST7735S) ------------------------------------
# The controller has 132x162 of GRAM but the panel only shows a 128x128 window
# of it, so writes are offset by 2 columns / 1 row. Ported from Waveshare's own
# driver, with the offsets keyed off the MV bit (see _set_scan_direction).
LCD_X, LCD_Y = 2, 1

# Scan direction -> memory-access control (MADCTL) bits. 0x20 (MV) exchanges
# rows and columns; 0x40 (MX) and 0x80 (MY) mirror them.
SCAN_DIRS = {
    "L2R_U2D": 0x00 | 0x00,
    "L2R_D2U": 0x00 | 0x80,
    "R2L_U2D": 0x40 | 0x00,
    "R2L_D2U": 0x40 | 0x80,
    "U2D_L2R": 0x00 | 0x00 | 0x20,
    "U2D_R2L": 0x00 | 0x40 | 0x20,
    "D2U_L2R": 0x80 | 0x00 | 0x20,
    "D2U_R2L": 0x40 | 0x80 | 0x20,
}


class WaveshareST7735Sink:
    """Pushes frames to a Waveshare ST7735S LCD HAT over SPI."""

    def __init__(self, w=128, h=128, spi=None, gpio=None,
                 scan_dir="U2D_R2L", backlight=100, offset=None):
        import RPi.GPIO as GPIO   # imported here so non-Pi hosts can still
        import spidev             # import this module for the fbdev sinks
        self.GPIO = GPIO

        spi_cfg  = spi  or {}
        gpio_cfg = gpio or {}
        self.rst = gpio_cfg.get("rst", 27)
        self.dc  = gpio_cfg.get("dc",  25)
        self.bl  = gpio_cfg.get("bl",  24)
        self.cs  = gpio_cfg.get("cs",   8)

        self.width, self.height = w, h
        # Scan direction picks these; sdl.offset in the profile can override if
        # a panel variant sits somewhere else in GRAM (image shifted by a pixel).
        self.x_adjust, self.y_adjust = LCD_X, LCD_Y
        self._offset_override = offset

        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        for pin in (self.rst, self.dc, self.bl, self.cs):
            GPIO.setup(pin, GPIO.OUT)

        self.spi = spidev.SpiDev()
        self.spi.open(spi_cfg.get("bus", 0), spi_cfg.get("device", 0))
        self.spi.max_speed_hz = spi_cfg.get("speed_hz", 9000000)
        self.spi.mode = 0b00

        self.pwm = GPIO.PWM(self.bl, 1000)
        self.pwm.start(backlight)

        self._init_panel(scan_dir)

    # -- low level ---------------------------------------------------------
    def _cmd(self, reg):
        self.GPIO.output(self.dc, self.GPIO.LOW)
        self.spi.writebytes([reg])

    def _data(self, *values):
        self.GPIO.output(self.dc, self.GPIO.HIGH)
        self.spi.writebytes(list(values))

    def _reset(self):
        import time
        for level in (self.GPIO.HIGH, self.GPIO.LOW, self.GPIO.HIGH):
            self.GPIO.output(self.rst, level)
            time.sleep(0.01)

    def _init_panel(self, scan_dir):
        import time
        self._reset()

        # Frame rate (normal / idle / partial modes)
        for reg in (0xB1, 0xB2):
            self._cmd(reg)
            self._data(0x01, 0x2C, 0x2D)
        self._cmd(0xB3)
        self._data(0x01, 0x2C, 0x2D, 0x01, 0x2C, 0x2D)

        self._cmd(0xB4)          # column inversion
        self._data(0x07)

        # Power sequence
        self._cmd(0xC0); self._data(0xA2, 0x02, 0x84)
        self._cmd(0xC1); self._data(0xC5)
        self._cmd(0xC2); self._data(0x0A, 0x00)
        self._cmd(0xC3); self._data(0x8A, 0x2A)
        self._cmd(0xC4); self._data(0x8A, 0xEE)
        self._cmd(0xC5); self._data(0x0E)   # VCOM

        # Gamma
        self._cmd(0xE0)
        self._data(0x0f, 0x1a, 0x0f, 0x18, 0x2f, 0x28, 0x20, 0x22,
                   0x1f, 0x1b, 0x23, 0x37, 0x00, 0x07, 0x02, 0x10)
        self._cmd(0xE1)
        self._data(0x0f, 0x1b, 0x0f, 0x17, 0x33, 0x2c, 0x29, 0x2e,
                   0x30, 0x30, 0x39, 0x3f, 0x00, 0x07, 0x03, 0x10)

        self._cmd(0xF0); self._data(0x01)   # enable test command
        self._cmd(0xF6); self._data(0x00)   # disable RAM power save
        self._cmd(0x3A); self._data(0x05)   # 65k colour mode

        self._set_scan_direction(scan_dir)
        time.sleep(0.2)

        self._cmd(0x11)          # sleep out
        time.sleep(0.12)
        self._cmd(0x29)          # display on

    def _set_scan_direction(self, scan_dir):
        mem_access = SCAN_DIRS.get(scan_dir, SCAN_DIRS["U2D_R2L"])
        if not mem_access & 0x20:
            # No row/column exchange: the frame is addressed portrait, so the
            # panel's own width/height swap round.
            self.width, self.height = self.height, self.width
        else:
            # MV on: set_window's X addresses GRAM rows and Y its columns, so
            # the visible-window offsets swap with them.
            self.x_adjust, self.y_adjust = LCD_Y, LCD_X
        if self._offset_override:
            self.x_adjust = self._offset_override.get("x", self.x_adjust)
            self.y_adjust = self._offset_override.get("y", self.y_adjust)
        self._cmd(0x36)
        self._data(mem_access | 0x08)   # RGB (not BGR) order

    def _set_window(self, x0, y0, x1, y1):
        self._cmd(0x2A)
        self._data(0x00, (x0 & 0xff) + self.x_adjust,
                   0x00, ((x1 - 1) & 0xff) + self.x_adjust)
        self._cmd(0x2B)
        self._data(0x00, (y0 & 0xff) + self.y_adjust,
                   0x00, ((y1 - 1) & 0xff) + self.y_adjust)
        self._cmd(0x2C)          # memory write

    # -- sink interface ----------------------------------------------------
    def write(self, surface):
        if surface.get_size() != (self.width, self.height):
            surface = pygame.transform.scale(surface, (self.width, self.height))
        # ST7735 takes RGB565 high byte first.
        data = rgb565_bytes(surface, big_endian=True)

        self._set_window(0, 0, self.width, self.height)
        self.GPIO.output(self.dc, self.GPIO.HIGH)
        if hasattr(self.spi, "writebytes2"):
            self.spi.writebytes2(data)        # chunks internally, no per-byte list
        else:
            for i in range(0, len(data), 4096):
                self.spi.writebytes(list(data[i:i + 4096]))

    def clear(self, colour=(0, 0, 0)):
        surface = pygame.Surface((self.width, self.height))
        surface.fill(colour)
        self.write(surface)

    def close(self):
        try:
            self.pwm.stop()
            self.spi.close()
        finally:
            # Only release the pins we claimed — a bare cleanup() would yank
            # GPIO out from under anything else on the board.
            self.GPIO.cleanup([self.rst, self.dc, self.bl, self.cs])

    def __str__(self):
        return f"waveshare_st7735 {self.width}x{self.height} spi"


def make_sink(P):
    """Build the output sink for a profile (see profiles.json -> sdl.driver)."""
    sdl    = P.get("sdl", {})
    driver = sdl.get("driver", "fbdev")

    if driver == "fbdev":
        return FramebufferSink(sdl["fbdev"])
    if driver == "waveshare_st7735":
        return WaveshareST7735Sink(
            w=P["screen"]["w"], h=P["screen"]["h"],
            spi=sdl.get("spi"), gpio=sdl.get("gpio"),
            scan_dir=sdl.get("scan_dir", "U2D_R2L"),
            backlight=sdl.get("backlight", 100),
            offset=sdl.get("offset"),
        )
    raise ValueError(f"Unknown display driver: {driver!r}")


def fb_write(surface, target):
    """Write a frame to a sink, or to a raw framebuffer path (older callers)."""
    if hasattr(target, "write"):
        target.write(surface)
    else:
        FramebufferSink(target).write(surface)
