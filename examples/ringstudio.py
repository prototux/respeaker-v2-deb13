#!/usr/bin/env python3
"""
ReSpeaker Core v2 - LED Ring Studio

Web UI driving the 12 APA102 LEDs of a ReSpeaker Core v2, with a live preview
and 90+ patterns: Alexa, Google Home, other assistants, classic effects,
status indicators and hardware tests.

Hardware (mainline device tree, Debian 13 image):
    LEDs        /dev/spidev0.1 (APA102, BGR, 5-bit global brightness)
    LED power   GPIO line "LED_PWR_N" (gpiochip2 line 2, active low)

Dependencies: none beyond Python 3.7+. Optional:
    python3-spidev    sets SPI mode/speed (otherwise raw writes to /dev/spidev*)
    python3-libgpiod  drives LED_PWR_N (otherwise the `gpioset` tool, from `gpiod`)

Run:
    python3 ringstudio.py            # on the board (user in groups spi + gpio)
    python3 ringstudio.py --sim      # anywhere, preview only

Then open http://<board-ip>:8080
"""

import argparse
import colorsys
import glob
import json
import math
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace


N = 12                      # LEDs on the ring
FPS = 30
GAMMA = 2.2
POWER_LINE = "LED_PWR_N"    # gpio-line-names in the board DTS
DEFAULT_SPI = "/dev/spidev0.1"
TAU = 2 * math.pi


# ============================================================================
# Color / math helpers. Colors are (r, g, b) floats in 0..1, perceptual
# (gamma is applied once, at output).
# ============================================================================

BLACK = (0.0, 0.0, 0.0)
WHITE = (1.0, 1.0, 1.0)
RED = (1.0, 0.0, 0.0)
GREEN = (0.0, 1.0, 0.0)
BLUE = (0.0, 0.0, 1.0)
ORANGE = (1.0, 0.38, 0.0)
AMBER = (1.0, 0.55, 0.0)
YELLOW = (1.0, 0.78, 0.0)
PURPLE = (0.55, 0.0, 1.0)
OK_GREEN = (0.0, 1.0, 0.3)

ALEXA_BLUE = (0.0, 0.12, 1.0)
ALEXA_CYAN = (0.0, 0.85, 1.0)
GOOGLE = [(0.26, 0.52, 0.96), (0.92, 0.26, 0.21),
          (0.98, 0.74, 0.02), (0.20, 0.66, 0.33)]


def clamp(x, lo=0.0, hi=1.0):
    return lo if x < lo else hi if x > hi else x


def smooth(x):
    x = clamp(x)
    return x * x * (3 - 2 * x)


def osc(t, period=1.0, phase=0.0):
    """0..1 cosine wave, 0 at t=0."""
    return 0.5 - 0.5 * math.cos(TAU * (t / period + phase))


def tri(x):
    """Triangle wave 0 -> 1 -> 0 over one unit."""
    return 1 - abs(2 * (x % 1.0) - 1)


def env(x, attack, hold, release):
    """Attack / hold / release envelope, 0 outside."""
    if x < 0:
        return 0.0
    if x < attack:
        return x / attack
    x -= attack
    if x < hold:
        return 1.0
    x -= hold
    return clamp(1 - x / release) if release else 0.0


def mix(a, b, k):
    return (a[0] + (b[0] - a[0]) * k,
            a[1] + (b[1] - a[1]) * k,
            a[2] + (b[2] - a[2]) * k)


def scale(c, k):
    return (c[0] * k, c[1] * k, c[2] * k)


def hsv(h, s=1.0, v=1.0):
    return colorsys.hsv_to_rgb(h % 1.0, s, v)


def hex_to_rgb(value, fallback=(0.0, 0.67, 1.0)):
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", str(value).strip())
    if not m:
        return fallback
    v = m.group(1)
    return tuple(int(v[i:i + 2], 16) / 255 for i in (0, 2, 4))


def palette_at(pal, x):
    seg = clamp(x) * (len(pal) - 1)
    i = min(int(seg), len(pal) - 2)
    return mix(pal[i], pal[i + 1], seg - i)


def _hash(n):
    return (math.sin(n * 127.1 + 311.7) * 43758.5453) % 1.0


def noise(x):
    """Smooth 1D value noise, 0..1."""
    i = math.floor(x)
    a, b = _hash(i), _hash(i + 1)
    return a + (b - a) * smooth(x - i)


def ringnoise(i, t, seed=0.0):
    """Organic 0..1 field that wraps seamlessly around the ring."""
    a = TAU * i / N
    return clamp(0.5
                 + 0.25 * math.sin(a + t * 0.9 + seed)
                 + 0.15 * math.sin(2 * a - t * 1.3 + seed * 2.1 + 1)
                 + 0.10 * math.sin(3 * a + t * 1.7 + seed * 3.7 + 2))


def speech(t, seed=0.0):
    """Fake voice envelope, 0..1: syllables with gaps between words."""
    v = (0.5 + 0.3 * math.sin(t * 9.3 + seed)
         + 0.2 * math.sin(t * 14.1 + seed * 1.7)
         + 0.15 * math.sin(t * 4.7 + seed * 0.6))
    words = 0.25 + 0.75 * smooth(noise(t * 2.2 + seed * 10))
    return clamp(v * words)


# ============================================================================
# Drawing primitives on a frame (list of N colors). Positions are floats in
# LED units and wrap around the ring.
# ============================================================================

def blank():
    return [BLACK] * N


def ring(c):
    return [c] * N


def cdist(a, b):
    d = abs(a - b) % N
    return min(d, N - d)


def add(px, i, c, k=1.0):
    i = int(i) % N
    r, g, b = px[i]
    px[i] = (r + c[0] * k, g + c[1] * k, b + c[2] * k)


def paint(px, i, c, k=1.0):
    i = int(i) % N
    px[i] = mix(px[i], c, clamp(k))


def dot(px, pos, c, k=1.0):
    """Anti-aliased dot, split between the two nearest LEDs."""
    i = math.floor(pos)
    f = pos - i
    add(px, i, c, k * (1 - f))
    add(px, i + 1, c, k * f)


def arc(px, center, half, c, k=1.0):
    """Soft-edged arc painted over the frame."""
    for i in range(N):
        w = clamp(half + 0.5 - cdist(i, center))
        if w > 0:
            paint(px, i, c, w * k)


def comet(px, pos, c, length=3, k=1.0, reverse=False):
    """Head at pos with a fading tail behind it (tail cw if reverse)."""
    for i in range(N):
        d = ((i - pos) if reverse else (pos - i)) % N   # distance behind head
        if d > N - 1:
            w = 1 - (N - d)                             # just ahead: anti-alias
        elif d < length + 1:
            w = 1 - d / (length + 1)
        else:
            continue
        add(px, i, c, k * w)


def fill(px, amount, c, start=0.0, k=1.0):
    """Light `amount` LEDs clockwise from start, last one partially."""
    for j in range(N):
        w = clamp(amount - j)
        if w <= 0:
            break
        add(px, start + j, c, k * w)


def spin(t, color, bg=None, length=3, rps=1.0, reverse=False):
    px = ring(bg) if bg else blank()
    pos = (-1 if reverse else 1) * t * rps * N
    comet(px, pos, color, length, reverse=reverse)
    return px


def clamp_frame(px):
    return [(clamp(r), clamp(g), clamp(b)) for r, g, b in px]


def fade_buffer(c, decay, key="buf"):
    """Persistent frame that fades each tick (for trails that outlive motion)."""
    buf = c.state.setdefault(key, blank())
    k = decay ** c.f
    for i in range(N):
        buf[i] = scale(buf[i], k)
    return buf


def trail_decay(trail):
    return trail / (trail + 1.5)


def lobes(t, specs, gain=1.0):
    """Gaussian blobs moving around the ring: (color, start, rps, width, amp)."""
    px = blank()
    for color, start, rps, width, amp in specs:
        center = start + t * rps * N
        for i in range(N):
            w = math.exp(-(cdist(i, center) / width) ** 2)
            add(px, i, color, w * amp * gain)
    return px


def flow(t, pal, speed=1.0):
    return [scale(palette_at(pal, ringnoise(i, t * speed)),
                  0.25 + 0.75 * smooth(ringnoise(i, -t * speed * 1.3, 4.0)))
            for i in range(N)]


# ============================================================================
# Pattern registry
#
# A pattern is fn(t, c) -> list of N colors. t is seconds since the pattern
# started (scaled by the speed setting); c holds settings and state:
#   c.c1, c.c2   colors          c.trail, c.count   ints
#   c.level      0..1            c.dir              direction, in LED units
#   c.dt, c.f    tick (s, and in 30 fps frames)
#   c.state      dict kept until the pattern restarts;  c.rng   random.Random
# `uses` lists the settings the UI should show for the pattern.
# ============================================================================

PATTERNS = {}


def pattern(category, label, uses="", desc=""):
    def register(fn):
        PATTERNS[fn.__name__] = {"fn": fn, "cat": category, "label": label,
                                 "uses": uses.split(), "desc": desc}
        return fn
    return register


# ---------------------------------------------------------------- Alexa ----

@pattern("Alexa", "Wake word", "direction",
         "Blue spreads around the ring, then cyan points at the speaker.")
def alexa_wake(t, c):
    px = blank()
    spread = smooth(t / 0.45) * (N / 2 + 1)
    for i in range(N):
        w = clamp(spread - cdist(i, c.dir))
        if w > 0:
            add(px, i, ALEXA_BLUE, w)
    arc(px, c.dir, 1, ALEXA_CYAN, smooth((t - 0.3) / 0.3))
    return px


@pattern("Alexa", "Listening", "direction",
         "Solid blue with a cyan segment toward the speaker.")
def alexa_listening(t, c):
    px = ring(ALEXA_BLUE)
    arc(px, c.dir, 1, ALEXA_CYAN, 0.8 + 0.2 * osc(t, 2))
    return px


@pattern("Alexa", "Thinking", desc="Cyan and blue alternating: processing.")
def alexa_thinking(t, c):
    return [mix(ALEXA_BLUE, ALEXA_CYAN, smooth(osc(t, 0.6, i / 2)))
            for i in range(N)]


@pattern("Alexa", "Speaking", desc="Blue and cyan pulsing with the voice.")
def alexa_speaking(t, c):
    e = speech(t)
    return ring(scale(mix(ALEXA_BLUE, ALEXA_CYAN, e), 0.55 + 0.45 * e))


@pattern("Alexa", "Mic off", desc="Solid red: microphones disabled.")
def alexa_mic_off(t, c):
    return ring(scale(RED, smooth(t / 0.3)))


@pattern("Alexa", "Notification", desc="Slow pulsing yellow: message or reminder.")
def alexa_notification(t, c):
    return ring(scale(YELLOW, 0.06 + 0.94 * osc(t, 2.2)))


@pattern("Alexa", "Do not disturb", desc="Brief purple glow, repeated.")
def alexa_dnd(t, c):
    return ring(scale(PURPLE, env(t % 4, 0.25, 0.6, 0.9)))


@pattern("Alexa", "Incoming call", desc="Pulsing green.")
def alexa_incoming_call(t, c):
    return ring(scale(GREEN, 0.1 + 0.9 * osc(t, 1.0)))


@pattern("Alexa", "Active call", desc="Green spinning: call or Drop In.")
def alexa_active_call(t, c):
    return spin(t, GREEN, scale(GREEN, 0.08), length=4, rps=0.7)


@pattern("Alexa", "Setup mode", desc="Orange spinning: setup / connecting.")
def alexa_setup(t, c):
    return spin(t, ORANGE, length=3, rps=0.8)


@pattern("Alexa", "Booting", desc="Blue ring with a cyan spinner.")
def alexa_boot(t, c):
    return spin(t, ALEXA_CYAN, scale(ALEXA_BLUE, 0.7), length=4, rps=0.9)


@pattern("Alexa", "Guard / Away", desc="Slow white spin.")
def alexa_guard(t, c):
    return spin(t, WHITE, length=5, rps=0.35)


@pattern("Alexa", "Volume", "level", desc="White arc showing the volume level.")
def alexa_volume(t, c):
    px = ring(scale(WHITE, 0.05))
    fill(px, c.level * N, WHITE)
    return px


@pattern("Alexa", "Alarm / timer", desc="Cyan swirl: alarm or timer going off.")
def alexa_alarm(t, c):
    return [scale(ALEXA_CYAN, 0.25 + 0.75 * osc(i / N * 2 - t * 1.5))
            for i in range(N)]


# --------------------------------------------------------------- Google ----

def google_dots(c, offset=0.0, levels=(1, 1, 1, 1), colors=GOOGLE, trail=0):
    px = blank()
    for k in range(4):
        pos = c.dir + offset + k * N / 4
        if trail:
            comet(px, pos, colors[k], trail, levels[k])
        else:
            dot(px, pos, colors[k], levels[k])
    return px


@pattern("Google", "Hotword", "direction",
         "Four colored dots swirl in and settle.")
def google_wake(t, c):
    angle = (1 - (1 - clamp(t / 0.9)) ** 3) * N
    lv = smooth(t / 0.25) * (1 if t < 0.9 else 0.8 + 0.2 * osc(t - 0.9, 1.6, 0.5))
    return google_dots(c, angle, [lv] * 4)


@pattern("Google", "Listening", "direction", "Four dots breathing.")
def google_listening(t, c):
    return google_dots(c, levels=[0.35 + 0.65 * osc(t, 1.4, -k * 0.15)
                                  for k in range(4)])


@pattern("Google", "Thinking", "direction", "Four dots chasing around.")
def google_thinking(t, c):
    return google_dots(c, t * N * 0.7, trail=1.5)


@pattern("Google", "Speaking", "direction", "Dots flicker with the voice.")
def google_speaking(t, c):
    return google_dots(c, levels=[0.15 + 0.85 * speech(t, k * 2.1)
                                  for k in range(4)])


@pattern("Google", "Mic muted", "direction", "Four amber dots.")
def google_mic_mute(t, c):
    return google_dots(c, colors=[AMBER] * 4)


@pattern("Google", "Volume", "level direction", "White LEDs fill to the level.")
def google_volume(t, c):
    px = blank()
    fill(px, c.level * N, WHITE, c.dir)
    return px


@pattern("Google", "Booting", desc="Four-color gradient spinning.")
def google_boot(t, c):
    px = []
    for i in range(N):
        x = ((i - t * 6) / N * 4) % 4
        k = int(x)
        px.append(mix(GOOGLE[k], GOOGLE[(k + 1) % 4], smooth(x - k)))
    return px


@pattern("Google", "Ready for setup", "direction", "White dots breathing in turn.")
def google_setup(t, c):
    return google_dots(c, colors=[WHITE] * 4,
                       levels=[0.1 + 0.9 * osc(t, 2.4, -k * 0.25) for k in range(4)])


@pattern("Google", "Updating", desc="White spinner.")
def google_update(t, c):
    return spin(t, WHITE, length=4, rps=0.6)


@pattern("Google", "Factory reset", desc="Orange spinner.")
def google_reset(t, c):
    return spin(t, ORANGE, scale(ORANGE, 0.06), length=3, rps=1.0)


@pattern("Google", "Alarm / timer", desc="Whole ring pulsing white.")
def google_alarm(t, c):
    return ring(scale(WHITE, 0.08 + 0.92 * osc(t, 1.2)))


# ------------------------------------------------------ Other assistants ----

@pattern("Other assistants", "Siri", desc="Pink, cyan and violet waves following a voice.")
def siri(t, c):
    e = 0.25 + 0.75 * speech(t)
    return lobes(t, [((1.0, 0.18, 0.55), 0, 0.22, 1.6, 0.6 + 0.4 * math.sin(t * 2)),
                     ((0.1, 0.75, 1.0), 4, -0.33, 1.3, 0.6 + 0.4 * math.sin(t * 3 + 1)),
                     ((0.6, 0.3, 1.0), 8, 0.15, 2.0, 0.6 + 0.4 * math.sin(t * 4 + 2))], e)


@pattern("Other assistants", "HomePod", desc="Soft swirling white, teal and pink.")
def homepod(t, c):
    gain = 0.45 + 0.55 * osc(t, 3)
    return lobes(t, [((0.9, 0.9, 1.0), 0, 0.12, 2.2, 0.8),
                     ((0.2, 0.9, 0.8), 4, -0.17, 1.6, 0.7),
                     ((1.0, 0.35, 0.6), 8, 0.09, 1.6, 0.7)], gain)


@pattern("Other assistants", "Cortana listening", desc="Blue ring breathing.")
def cortana_listening(t, c):
    return ring(scale((0.0, 0.47, 0.84), 0.3 + 0.7 * osc(t, 1.6)))


def material(t, color, period=1.5, max_len=8.0):
    """Arc that grows and shrinks while rotating (material design spinner)."""
    n, ph = divmod(t / period, 1.0)
    base = t * N * 0.25 + n * max_len
    head = base + max_len * smooth(ph * 2)
    tail = base + max_len * smooth(ph * 2 - 1)
    px = blank()
    for i in range(N):
        d = (i - tail) % N          # distance cw from the tail
        w = clamp(min(d + 0.5, head - tail + 1 - d - 0.5))
        if w > 0:
            add(px, i, color, w)
    return px


@pattern("Other assistants", "Cortana thinking", desc="Blue arc growing and shrinking.")
def cortana_thinking(t, c):
    return material(t, (0.0, 0.47, 0.84))


@pattern("Other assistants", "Bixby", desc="Violet-to-blue gradient rotating.")
def bixby(t, c):
    return [scale(mix((0.45, 0.15, 1.0), (0.05, 0.5, 1.0), osc(i / N - t * 0.3)),
                  0.5 + 0.5 * osc(t, 2)) for i in range(N)]


@pattern("Other assistants", "HAL 9000", desc="I'm sorry Dave.")
def hal9000(t, c):
    k = 0.5 + 0.3 * osc(t, 4) + 0.08 * noise(t * 8)
    return [scale(RED, k * (0.9 + 0.1 * ringnoise(i, t))) for i in range(N)]


@pattern("Other assistants", "Arc reactor", desc="Shimmering cyan-white.")
def arc_reactor(t, c):
    return [mix((0.3, 0.75, 1.0), (0.85, 0.95, 1.0), ringnoise(i, t * 4))
            for i in range(N)]


@pattern("Other assistants", "KITT", "trail direction", "Knight Rider scanner.")
def kitt(t, c):
    buf = fade_buffer(c, trail_decay(max(2, c.trail)))
    dot(buf, c.dir - N / 4 + smooth(tri(t * 0.6)) * N / 2, RED)
    buf[:] = clamp_frame(buf)
    return buf


# ------------------------------------------------------------- Standard ----

@pattern("Standard", "Off")
def off(t, c):
    return blank()


@pattern("Standard", "Solid", "color")
def solid(t, c):
    return ring(c.c1)


@pattern("Standard", "Breathe", "color", "Slow sinusoidal breathing.")
def breathe(t, c):
    return ring(scale(c.c1, 0.04 + 0.96 * osc(t, 4)))


@pattern("Standard", "Blink", "color")
def blink(t, c):
    return ring(c.c1 if t % 1 < 0.5 else BLACK)


@pattern("Standard", "Strobe", "color")
def strobe(t, c):
    return ring(c.c1 if t % 0.2 < 0.05 else BLACK)


@pattern("Standard", "Heartbeat", "color")
def heartbeat(t, c):
    x = t % 1.2
    k = max(env(x, 0.04, 0.02, 0.18), 0.7 * env(x - 0.28, 0.04, 0.02, 0.3))
    return ring(scale(c.c1, 0.03 + 0.97 * k))


@pattern("Standard", "Fade", "color color2", "Cross-fade between two colors.")
def fade(t, c):
    return ring(mix(c.c1, c.c2, osc(t, 5)))


@pattern("Standard", "Color wipe", "color color2")
def color_wipe(t, c):
    cycle, pos = divmod(t * 8, N)
    a, b = (c.c1, c.c2) if cycle % 2 == 0 else (c.c2, c.c1)
    return [mix(b, a, clamp(pos - i)) for i in range(N)]


@pattern("Standard", "Theater chase", "color")
def theater_chase(t, c):
    step = int(t * 8)
    return [c.c1 if (i - step) % 3 == 0 else scale(c.c1, 0.04) for i in range(N)]


@pattern("Standard", "Rainbow", desc="Rotating rainbow.")
def rainbow(t, c):
    return [hsv(i / N + t * 0.2) for i in range(N)]


@pattern("Standard", "Rainbow cycle", desc="Whole ring through the hues.")
def rainbow_cycle(t, c):
    return ring(hsv(t * 0.1))


@pattern("Standard", "Gradient", "color color2", "Two-color gradient rotating.")
def gradient(t, c):
    return [mix(c.c1, c.c2, osc(i / N - t * 0.25)) for i in range(N)]


@pattern("Standard", "Spinner", "color trail")
def spinner(t, c):
    return spin(t, c.c1, length=c.trail, rps=0.75)


@pattern("Standard", "Comet", "color trail", "Fast head with a long tail.")
def comet_pattern(t, c):
    return spin(t, c.c1, length=max(2, c.trail * 1.5), rps=1.2)


@pattern("Standard", "Dual spinner", "color color2 trail")
def dual_spinner(t, c):
    px = blank()
    comet(px, t * N * 0.75, c.c1, c.trail)
    comet(px, t * N * 0.75 + N / 2, c.c2, c.trail)
    return px


@pattern("Standard", "Orbits", "color color2 trail", "Two dots circling in opposite directions.")
def orbits(t, c):
    px = blank()
    comet(px, t * N * 0.5, c.c1, c.trail)
    comet(px, -t * N * 0.5, c.c2, c.trail, reverse=True)
    return px


@pattern("Standard", "Scanner", "color trail direction",
         "Two dots sweeping out from the direction and back.")
def scanner(t, c):
    buf = fade_buffer(c, trail_decay(c.trail))
    p = tri(t * 0.5) * N / 2
    dot(buf, c.dir + p, c.c1)
    dot(buf, c.dir - p, c.c1)
    buf[:] = clamp_frame(buf)
    return buf


@pattern("Standard", "Ping-pong", "color trail", "Bounces back and forth around the ring.")
def pingpong(t, c):
    buf = fade_buffer(c, trail_decay(c.trail))
    dot(buf, tri(t * 0.25) * (N - 1), c.c1)
    buf[:] = clamp_frame(buf)
    return buf


@pattern("Standard", "Loading dots", "color count", "Several dots orbiting, fading.")
def loading(t, c):
    px = blank()
    pos = t * N / 2
    for k in range(c.count):
        dot(px, pos - k * N / c.count, c.c1, 1 - k / c.count)
    return px


@pattern("Standard", "iOS spinner", "color", "12 spokes, brightest stepping round.")
def ios_spinner(t, c):
    head = int(t * 12) % N
    return [scale(c.c1, 0.06 + 0.94 * (1 - ((head - i) % N) / N) ** 2)
            for i in range(N)]


@pattern("Standard", "Material spinner", "color", "Arc growing and shrinking while it turns.")
def material_spinner(t, c):
    return material(t, c.c1)


@pattern("Standard", "Windows loading", "color direction", "Five dots racing round, then pausing.")
def windows_loading(t, c):
    px = blank()
    ph = t % 3.0
    for k in range(5):
        x = (ph - k * 0.15) / 2.2
        if 0 <= x <= 1:
            dot(px, c.dir + smooth(x) * N, c.c1, min(1, x * 10, (1 - x) * 10))
    return px


@pattern("Standard", "Twinkle", "color", "LEDs fade in and out at random.")
def twinkle(t, c):
    ph = c.state.setdefault("ph", [-1.0] * N)
    px = blank()
    for i in range(N):
        if ph[i] < 0:
            if c.rng.random() < 0.015 * c.f:
                ph[i] = 0.0
        else:
            ph[i] += c.dt * 0.8
            if ph[i] >= 1:
                ph[i] = -1.0
            else:
                add(px, i, c.c1, math.sin(math.pi * ph[i]) ** 2)
    return px


@pattern("Standard", "Sparkle", "color", "Dim base with white glints.")
def sparkle(t, c):
    fl = c.state.setdefault("fl", [0.0] * N)
    for i in range(N):
        fl[i] *= 0.7 ** c.f
    if c.rng.random() < 0.25 * c.f:
        fl[c.rng.randrange(N)] = 1.0
    return [mix(scale(c.c1, 0.12), WHITE, fl[i]) for i in range(N)]


@pattern("Standard", "Confetti", desc="Random colors popping and fading.")
def confetti(t, c):
    px = c.state.setdefault("px", blank())
    k = 0.93 ** c.f
    for i in range(N):
        px[i] = scale(px[i], k)
    if c.rng.random() < 0.3 * c.f:
        px[c.rng.randrange(N)] = hsv(c.rng.random())
    return px


@pattern("Standard", "Fire", desc="Flickering embers.")
def fire(t, c):
    heat = c.state.setdefault("heat", [0.3] * N)
    for i in range(N):
        heat[i] = max(0.0, heat[i] - c.rng.random() * 0.06 * c.f)
    if c.rng.random() < 0.5 * c.f:
        j = c.rng.randrange(N)
        heat[j] = min(1.0, heat[j] + c.rng.uniform(0.4, 0.9))
    spread = [(heat[i - 1] + 2 * heat[i] + heat[(i + 1) % N]) / 4 for i in range(N)]
    k = min(1.0, 0.5 * c.f)
    heat[:] = [h + (s - h) * k for h, s in zip(heat, spread)]
    return [(clamp(h * 3), clamp(h * 3 - 1) * 0.8, clamp(h * 3 - 2) * 0.5) for h in heat]


@pattern("Standard", "Candle", desc="Warm, gently flickering.")
def candle(t, c):
    flick = 0.6 + 0.25 * noise(t * 5) + 0.15 * noise(t * 17 + 3)
    return [scale((1.0, 0.42, 0.06), flick * (0.85 + 0.15 * ringnoise(i, t * 3)))
            for i in range(N)]


@pattern("Standard", "Meteor", "color trail", "Comet with a crumbling tail.")
def meteor(t, c):
    buf = c.state.setdefault("buf", blank())
    base = trail_decay(max(1, c.trail))
    for i in range(N):
        k = base * (0.65 if c.rng.random() < 0.5 else 1.0)
        buf[i] = scale(buf[i], k ** c.f)
    dot(buf, t * N * 0.9, c.c1)
    buf[:] = clamp_frame(buf)
    return buf


@pattern("Standard", "Police", "direction", "Red and blue halves, double flashes.")
def police(t, c):
    x = t % 1.0
    y = x % 0.5
    on = y < 0.08 or 0.14 <= y < 0.22
    red_side = x < 0.5
    px = []
    for i in range(N):
        first_half = (i - c.dir) % N < N / 2
        if on and first_half == red_side:
            px.append(RED if red_side else BLUE)
        else:
            px.append(BLACK)
    return px


@pattern("Standard", "Lighthouse", "color", "Soft beam sweeping round.")
def lighthouse(t, c):
    center = t * N / 3
    return [mix(scale(c.c1, 0.03), c.c1, math.exp(-(cdist(i, center) / 1.3) ** 2))
            for i in range(N)]


@pattern("Standard", "Ripple", "color direction", "Waves running from the direction to the far side.")
def ripple(t, c):
    px = []
    for i in range(N):
        d = cdist(i, c.dir)
        k = max(0.0, math.cos(TAU * (d / 4 - t * 0.7))) ** 4 * (1 - 0.5 * d / (N / 2))
        px.append(scale(c.c1, k))
    return px


@pattern("Standard", "Wave", "color color2", "Sine wave between two colors.")
def wave(t, c):
    return [mix(c.c2, c.c1, 0.5 + 0.5 * math.sin(TAU * (2 * i / N - t * 0.5)))
            for i in range(N)]


@pattern("Standard", "Plasma", desc="Psychedelic shifting hues.")
def plasma(t, c):
    return [hsv(t * 0.05 + 0.5 * ringnoise(i, t, 1.0),
                1.0, 0.4 + 0.6 * ringnoise(i, -t * 1.4, 7.0)) for i in range(N)]


@pattern("Standard", "Aurora", desc="Green, teal and violet curtains.")
def aurora(t, c):
    return flow(t * 0.5, [(0.0, 1.0, 0.4), (0.0, 0.6, 0.8), (0.5, 0.1, 0.9)])


@pattern("Standard", "Ocean", desc="Deep blue and teal swell.")
def ocean(t, c):
    return flow(t * 0.4, [(0.0, 0.05, 0.4), (0.0, 0.35, 0.9), (0.1, 0.9, 0.8)])


@pattern("Standard", "Lava", desc="Slow red and orange blobs.")
def lava(t, c):
    return flow(t * 0.3, [(0.4, 0.0, 0.0), (1.0, 0.1, 0.0), (1.0, 0.5, 0.0)])


@pattern("Standard", "Matter / antimatter", "color color2 trail direction",
         "Two particles leave together and annihilate on the far side.")
def matter(t, c):
    px = blank()
    ph = (t / 2.0) % 1.0
    if ph < 0.8:
        p = ph / 0.8 * N / 2
        comet(px, c.dir + p, c.c1, c.trail)
        comet(px, c.dir - p, c.c2, c.trail, reverse=True)
    else:
        arc(px, c.dir + N / 2, 1.5, WHITE, 1 - (ph - 0.8) / 0.2)
    return px


@pattern("Standard", "Matter / antimatter loop", "color color2 trail direction",
         "The particles pass through each other and keep circling.")
def matter_loop(t, c):
    px = blank()
    p = (t / 4.0) % 1.0 * N
    comet(px, c.dir + p, c.c1, c.trail)
    comet(px, c.dir - p, c.c2, c.trail, reverse=True)
    return px


@pattern("Standard", "Collision", "color color2 trail direction",
         "Particles from opposite sides collide in a flash.")
def collision(t, c):
    px = blank()
    ph = (t / 2.0) % 1.0
    meet = c.dir + N / 4
    if ph < 0.7:
        q = ph / 0.7 * N / 4
        comet(px, c.dir + q, c.c1, c.trail)
        comet(px, c.dir + N / 2 - q, c.c2, c.trail, reverse=True)
    else:
        x = (ph - 0.7) / 0.3
        arc(px, meet, x * N / 2, mix(mix(c.c1, c.c2, 0.5), WHITE, 0.5), 1 - x)
    return px


@pattern("Standard", "Yin-yang", "color color2", "Two halves rotating, each with a dot of the other.")
def yin_yang(t, c):
    rot = t * 0.12 * N
    px = [mix(c.c2, c.c1, smooth((osc(i / N - t * 0.12 + 0.5) - 0.4) / 0.2))
          for i in range(N)]
    paint(px, round(rot), c.c2, 0.8)
    paint(px, round(rot + N / 2), c.c1, 0.8)
    return px


@pattern("Standard", "Clock", "color color2",
         "Live clock: hour (color), minute (color 2), seconds (white). LED 0 is 12 o'clock.")
def clock(t, c):
    lt = time.localtime()
    sec = lt.tm_sec + time.time() % 1
    mins = lt.tm_min + sec / 60
    hours = lt.tm_hour % 12 + mins / 60
    px = blank()
    for i in range(0, N, 3):
        add(px, i, WHITE, 0.04)
    dot(px, hours / 12 * N, c.c1)
    dot(px, mins / 60 * N, c.c2, 0.8)
    dot(px, sec / 60 * N, WHITE, 0.35)
    return px


@pattern("Standard", "Countdown", "color", "Ring empties over 10 s, then flashes.")
def countdown(t, c):
    x = t % 12
    px = blank()
    if x < 10:
        fill(px, (1 - x / 10) * N, c.c1)
    elif x % 0.5 < 0.25:
        px = ring(c.c1)
    return px


@pattern("Standard", "Progress", "color level", "Static gauge at the level setting.")
def progress(t, c):
    px = ring(scale(c.c1, 0.04))
    fill(px, c.level * N, c.c1)
    return px


@pattern("Standard", "VU meter", desc="Fake audio level, green to red, with peak hold.")
def vu(t, c):
    level = clamp(speech(t * 1.3) * 1.05) * N
    peak = c.state.get("peak", 0.0)
    peak = level if level > peak else max(0.0, peak - 6 * c.dt)
    c.state["peak"] = peak
    px = blank()
    for i in range(N):
        color = GREEN if i < N * 0.6 else YELLOW if i < N * 0.8 else RED
        add(px, i, color, clamp(level - i))
    add(px, min(N - 1, int(peak)), WHITE, 0.6)
    return px


@pattern("Standard", "Sunrise", desc="60 s from darkness to warm white, then holds.")
def sunrise(t, c):
    x = clamp(t / 60)
    pal = [BLACK, (0.5, 0.0, 0.0), (1.0, 0.35, 0.0), (1.0, 0.75, 0.4), (1.0, 0.9, 0.75)]
    return ring(palette_at(pal, x))


# --------------------------------------------------------------- Status ----

@pattern("Status", "Wake", "color", "Bright flash, then a soft glow.")
def wake(t, c):
    k = max(env(t, 0.12, 0.05, 0.45), smooth((t - 0.3) / 0.3) * (0.3 + 0.1 * osc(t, 2)))
    return ring(scale(c.c1, k))


@pattern("Status", "Success", "direction", "Green sweep, hold, fade.")
def success(t, c):
    x = t % 3
    px = blank()
    k = 1.0 if x < 1.4 else clamp(1 - (x - 1.4) / 0.5)
    fill(px, smooth(x / 0.6) * N, OK_GREEN, c.dir, k)
    return px


@pattern("Status", "Error", desc="Red double flash.")
def error(t, c):
    x = t % 1.5
    return ring(RED if x < 0.12 or 0.25 <= x < 0.37 else BLACK)


@pattern("Status", "Warning", desc="Amber pulsing.")
def warning(t, c):
    return ring(scale(AMBER, osc(t, 0.9)))


@pattern("Status", "Notification", "color", "Three pulses, then a pause.")
def notification(t, c):
    x = t % 3
    k = max(env(x - j * 0.45, 0.12, 0.0, 0.3) for j in range(3))
    return ring(scale(c.c1, k))


@pattern("Status", "Charging", "level direction", "Green gauge with a light running up it.")
def charging(t, c):
    amount = max(1.0, c.level * N)
    px = blank()
    fill(px, amount, OK_GREEN, c.dir, 0.35)
    pos = (t % 1.8) / 1.5 * amount
    if pos <= amount:
        dot(px, c.dir + pos, OK_GREEN, 0.7)
    return px


@pattern("Status", "Low battery", "direction", "Single red LED blinking.")
def low_battery(t, c):
    px = blank()
    dot(px, c.dir, RED, env(t % 2, 0.1, 0.2, 0.4))
    return px


@pattern("Status", "Searching", "direction", "Two blue dots sweeping: looking for Wi-Fi.")
def searching(t, c):
    px = blank()
    p = smooth(tri(t * 0.4)) * N / 2
    dot(px, c.dir + p, BLUE)
    dot(px, c.dir - p, BLUE)
    return px


@pattern("Status", "Downloading", desc="Cyan ring filling over 8 s, with a bright head.")
def downloading(t, c):
    amount = (t % 9) / 8 * N
    px = ring(scale(ALEXA_CYAN, 0.04))
    fill(px, min(amount, N), scale(ALEXA_CYAN, 0.6))
    if amount < N:
        dot(px, amount, WHITE, 0.8)
    return px


@pattern("Status", "Recording", "direction", "Red dot pulsing.")
def recording(t, c):
    px = ring(scale(RED, 0.05))
    dot(px, c.dir, RED, 0.3 + 0.7 * osc(t, 1.0))
    return px


@pattern("Status", "Alarm", "color", "Bursts of fast flashes.")
def alarm(t, c):
    x = t % 2
    return ring(c.c1 if x < 1 and x % 0.25 < 0.12 else scale(c.c1, 0.03))


# ----------------------------------------------------------------- Test ----

@pattern("Test", "Pixel walk", desc="One white LED at a time; LED 0 stays dim red.")
def pixel_walk(t, c):
    px = blank()
    px[0] = scale(RED, 0.15)
    px[int(t * 2) % N] = WHITE
    return px


@pattern("Test", "RGB test", desc="Red, green, blue, white, 1 s each.")
def rgb_test(t, c):
    return ring([RED, GREEN, BLUE, WHITE][int(t) % 4])


@pattern("Test", "Orientation", "direction",
         "LED 0 red, LED 1 green, last LED blue, direction yellow.")
def orientation(t, c):
    px = blank()
    px[0], px[1], px[N - 1] = RED, GREEN, BLUE
    paint(px, round(c.dir), YELLOW)
    return px


@pattern("Test", "Brightness ramp", desc="LED 0 off up to the last LED at full white.")
def gamma_ramp(t, c):
    return [scale(WHITE, i / (N - 1)) for i in range(N)]


# ============================================================================
# Output: APA102 over spidev, LED power through the GPIO character device
# ============================================================================

def encode_apa102(frame, brightness):
    """
    APA102 frame. Uses the 5-bit per-LED current control as extra dynamic
    range: each LED gets the smallest current level that fits its brightest
    channel, so dim colors keep their full 8-bit resolution.
    """
    data = bytearray(4)                                 # start frame
    for r, g, b in frame:
        lin = [clamp(v) ** GAMMA * brightness for v in (r, g, b)]
        peak = max(lin)
        if peak <= 0:
            data += b"\xe0\x00\x00\x00"
            continue
        level = min(31, max(1, math.ceil(peak * 31)))
        k = 255 * 31 / level
        data += bytes((0xE0 | level,
                       min(255, round(lin[2] * k)),
                       min(255, round(lin[1] * k)),
                       min(255, round(lin[0] * k))))
    data += b"\xff" * 4                                 # end frame
    return bytes(data)


class LedPower:
    """
    LED_PWR_N (active low). Requested by name through python3-libgpiod v2 if
    installed, otherwise held by a `gpioset` process (libgpiod v2 tools).
    """

    def __init__(self, name=POWER_LINE):
        self.name = name
        self.request = None
        self.proc = None
        self.how = "not controlled"

    def on(self):
        try:
            import gpiod
        except ImportError:
            gpiod = None
        if gpiod is not None and hasattr(gpiod, "request_lines"):
            self._on_gpiod(gpiod)
        elif shutil.which("gpioset"):
            self._on_gpioset()
        else:
            raise RuntimeError("install python3-libgpiod or gpiod to switch the LED power")

    def _on_gpiod(self, gpiod):
        from gpiod.line import Direction, Value
        for path in sorted(glob.glob("/dev/gpiochip*")):
            try:
                with gpiod.Chip(path) as chip:
                    offset = chip.line_offset_from_id(self.name)
            except (OSError, ValueError, LookupError):
                continue
            settings = gpiod.LineSettings(direction=Direction.OUTPUT, active_low=True,
                                          output_value=Value.ACTIVE)
            self.request = gpiod.request_lines(path, consumer="ringstudio",
                                               config={offset: settings})
            self.offset = offset
            self.how = "%s line %d (python gpiod)" % (path, offset)
            return
        raise RuntimeError("GPIO line %s not found" % self.name)

    def _on_gpioset(self):
        self.proc = subprocess.Popen(
            ["gpioset", "--consumer", "ringstudio", "--active-low", self.name + "=1"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        time.sleep(0.3)
        if self.proc.poll() is not None:            # gpioset holds the line until killed
            err = self.proc.stderr.read().decode(errors="replace").strip()
            self.proc = None
            raise RuntimeError("gpioset failed: %s" % err)
        self.how = "%s (gpioset)" % self.name

    def off(self):
        if self.request is not None:
            from gpiod.line import Value
            self.request.set_value(self.offset, Value.INACTIVE)
            self.request.release()
            self.request = None
        if self.proc is not None:
            self.proc.terminate()
            self.proc.wait(timeout=2)
            self.proc = None
            subprocess.run(["gpioset", "-t", "0", self.name + "=1"],   # high = off
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)


class Apa102:
    def __init__(self, path, hz, offset=0, reverse=False):
        self.map = [(offset + (-i if reverse else i)) % N for i in range(N)]
        try:
            import spidev
            m = re.search(r"spidev(\d+)\.(\d+)$", path)
            self.spi = spidev.SpiDev()
            self.spi.open(int(m.group(1)), int(m.group(2)))
            self.spi.max_speed_hz = hz
            self.spi.mode = 0
            self.write = lambda data: self.spi.xfer2(list(data))
            self.how = "%s @ %.1f MHz" % (path, hz / 1e6)
        except ImportError:
            self.fd = os.open(path, os.O_WRONLY)
            self.write = lambda data: os.write(self.fd, data)
            self.how = "%s (raw writes, no python3-spidev)" % path

    def show(self, frame, brightness):
        physical = [BLACK] * N
        for i, px in enumerate(frame):
            physical[self.map[i]] = px
        self.write(encode_apa102(physical, brightness))

    def close(self):
        try:
            self.show(blank(), 0)
        except OSError:
            pass


class Simulator:
    how = "simulation (preview only)"

    def show(self, frame, brightness):
        pass

    def close(self):
        pass


# ============================================================================
# Engine: renders the current pattern at FPS on its own thread
# ============================================================================

SETTING_RANGES = {"brightness": (0, 100), "speed": (10, 400), "trail": (0, 8),
                  "count": (1, 8), "level": (0, 100), "direction": (0, 359)}
DEFAULTS = {"brightness": 40, "speed": 100, "trail": 3, "count": 3, "level": 60,
            "direction": 0, "color": "#00aaff", "color2": "#ff0055"}


class Engine:
    def __init__(self, strip):
        self.strip = strip
        self.lock = threading.Lock()
        self.settings = dict(DEFAULTS)
        self.pattern = "off"
        self.frame = blank()
        self.stopped = threading.Event()
        self._restart()

    def _restart(self):
        self.t = 0.0
        self.state = {}
        self.rng = random.Random()

    def set_pattern(self, name):
        with self.lock:
            self.pattern = name
            self._restart()

    def update(self, data):
        with self.lock:
            for key, (lo, hi) in SETTING_RANGES.items():
                if key in data:
                    try:
                        self.settings[key] = int(clamp(int(data[key]), lo, hi))
                    except (TypeError, ValueError):
                        pass
            for key in ("color", "color2"):
                if key in data and re.fullmatch(r"#[0-9a-fA-F]{6}", str(data[key])):
                    self.settings[key] = data[key]

    def _context(self, dt):
        s = self.settings
        return SimpleNamespace(
            c1=hex_to_rgb(s["color"]), c2=hex_to_rgb(s["color2"]),
            trail=s["trail"], count=s["count"], level=s["level"] / 100,
            dir=s["direction"] / 360 * N, dt=dt, f=dt * FPS,
            state=self.state, rng=self.rng)

    def run(self):
        last = time.monotonic()
        while not self.stopped.is_set():
            now = time.monotonic()
            with self.lock:
                dt = min(now - last, 0.25) * self.settings["speed"] / 100
                self.t += dt
                try:
                    frame = PATTERNS[self.pattern]["fn"](self.t, self._context(dt))
                except Exception:
                    traceback.print_exc()
                    print("Pattern %r failed, switching off." % self.pattern)
                    self.pattern = "off"
                    frame = blank()
                self.frame = clamp_frame(frame)
                brightness = (self.settings["brightness"] / 100) ** GAMMA
            last = now
            try:
                self.strip.show(self.frame, brightness)
            except OSError as e:
                print("LED write failed:", e)
                time.sleep(1)
            self.stopped.wait(max(0.0, 1 / FPS - (time.monotonic() - now)))

    def stop(self):
        self.stopped.set()

    def snapshot(self):
        with self.lock:
            return {"pattern": self.pattern, "settings": dict(self.settings),
                    "frame": ["#%02x%02x%02x" % tuple(round(v * 255) for v in px)
                              for px in self.frame]}


def catalog():
    return [{"id": k, "cat": p["cat"], "label": p["label"], "desc": p["desc"],
             "uses": p["uses"]} for k, p in PATTERNS.items()]


# ============================================================================
# Web server (stdlib only)
# ============================================================================

class Handler(BaseHTTPRequestHandler):
    engine = None
    output = ""

    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj))

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif path == "/api/patterns":
            self._json(catalog())
        elif path == "/api/state":
            self._json(dict(self.engine.snapshot(), output=self.output))
        elif path == "/api/stream":
            self._stream()
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            length = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(data, dict):
                raise ValueError
        except ValueError:
            return self._json({"error": "invalid JSON object"}, 400)

        if path == "/api/pattern":
            name = data.get("pattern")
            if name not in PATTERNS:
                return self._json({"error": "unknown pattern"}, 400)
            self.engine.set_pattern(name)
        elif path == "/api/settings":
            self.engine.update(data)
        else:
            return self._json({"error": "not found"}, 404)
        self._json(self.engine.snapshot())

    def _stream(self):
        """Server-sent events: the live frame, 20 times a second."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            while not self.engine.stopped.is_set():
                msg = "data: %s\n\n" % json.dumps(self.engine.snapshot())
                self.wfile.write(msg.encode())
                self.wfile.flush()
                time.sleep(0.05)
        except (BrokenPipeError, ConnectionResetError):
            pass


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ring Studio</title>
<style>
:root {
  --bg: #0d0e11; --panel: #15171c; --line: #252930; --text: #e7e9ec;
  --muted: #8b919a; --chip: #1c1f25; --chip-hover: #252931; --accent: #38bdf8;
  --off: #1c1f25;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
       font: 15px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif; }
.wrap { max-width: 1120px; margin: 0 auto; padding: 16px; }
header { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 16px; }
h1 { font-size: 19px; margin: 0 6px 0 0; }
.badge { font-size: 12px; color: var(--muted); border: 1px solid var(--line);
         border-radius: 999px; padding: 2px 10px; }
.badge.sim { color: #fbbf24; border-color: #5c4712; }
.badge.down { color: #f87171; border-color: #5c1d1d; }
.top { display: grid; grid-template-columns: minmax(260px, 330px) 1fr; gap: 16px; margin-bottom: 16px; }
@media (max-width: 760px) { .top { grid-template-columns: 1fr; } }
.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 14px; padding: 16px; }

.ring { position: relative; width: 100%; max-width: 290px; aspect-ratio: 1; margin: 0 auto; }
.led { position: absolute; width: 14%; height: 14%; margin: -7% 0 0 -7%; border-radius: 50%;
       background: var(--off); border: 1px solid #ffffff10; }
.idx { position: absolute; font-size: 10px; color: var(--muted); transform: translate(-50%, -50%); }
.center { position: absolute; inset: 24%; display: flex; flex-direction: column;
          align-items: center; justify-content: center; text-align: center; gap: 2px; }
.center b { font-size: 16px; }
.center span { font-size: 12px; color: var(--muted); }
.center button { margin-top: 10px; }

.controls { display: grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: 14px 18px; }
.ctl { display: block; }
[hidden] { display: none !important; }
.ctl .lbl { display: flex; justify-content: space-between; color: var(--muted); font-size: 13px; margin-bottom: 6px; }
.ctl .lbl output { color: var(--text); font-variant-numeric: tabular-nums; }
input[type=range] { width: 100%; accent-color: var(--accent); }
input[type=color] { width: 100%; height: 36px; padding: 0; border: 1px solid var(--line);
                    border-radius: 8px; background: none; cursor: pointer; }
.desc { margin-top: 14px; color: var(--muted); font-size: 13px; min-height: 1.4em; }

button { font: inherit; color: var(--text); background: var(--chip); border: 1px solid var(--line);
         border-radius: 9px; padding: 8px 12px; cursor: pointer; }
button:hover { background: var(--chip-hover); }
button.stop { border-color: #5c1d1d; color: #fca5a5; padding: 5px 14px; font-size: 13px; }

.toolbar { display: flex; gap: 10px; align-items: center; margin-bottom: 4px; }
.toolbar input { flex: 1; font: inherit; color: var(--text); background: var(--chip);
                 border: 1px solid var(--line); border-radius: 9px; padding: 8px 12px; }
.toolbar span { color: var(--muted); font-size: 13px; white-space: nowrap; }
section h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .06em;
             color: var(--muted); margin: 18px 0 8px; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 8px; }
.grid button { text-align: left; }
.grid button.active { border-color: var(--accent); background: #12303d; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Ring Studio</h1>
    <span class="badge" id="output">connecting…</span>
    <span class="badge" id="conn" hidden>disconnected</span>
  </header>

  <div class="top">
    <div class="panel">
      <div class="ring" id="ring">
        <div class="center">
          <b id="name">Off</b>
          <span id="cat"></span>
          <button class="stop" onclick="choose('off')">Stop</button>
        </div>
      </div>
    </div>

    <div class="panel">
      <div class="controls">
        <label class="ctl"><div class="lbl">Brightness <output id="o-brightness"></output></div>
          <input type="range" id="brightness" min="0" max="100"></label>
        <label class="ctl"><div class="lbl">Speed <output id="o-speed"></output></div>
          <input type="range" id="speed" min="10" max="400" step="5"></label>
        <label class="ctl" data-use="color"><div class="lbl">Color</div>
          <input type="color" id="color"></label>
        <label class="ctl" data-use="color2"><div class="lbl">Color 2</div>
          <input type="color" id="color2"></label>
        <label class="ctl" data-use="trail"><div class="lbl">Trail <output id="o-trail"></output></div>
          <input type="range" id="trail" min="0" max="8"></label>
        <label class="ctl" data-use="count"><div class="lbl">Dots <output id="o-count"></output></div>
          <input type="range" id="count" min="1" max="8"></label>
        <label class="ctl" data-use="level"><div class="lbl">Level <output id="o-level"></output></div>
          <input type="range" id="level" min="0" max="100"></label>
        <label class="ctl" data-use="direction"><div class="lbl">Direction <output id="o-direction"></output></div>
          <input type="range" id="direction" min="0" max="359"></label>
      </div>
      <div class="desc" id="desc"></div>
    </div>
  </div>

  <div class="panel">
    <div class="toolbar">
      <input id="search" type="search" placeholder="Filter patterns…" autocomplete="off">
      <span id="total"></span>
    </div>
    <div id="list"></div>
  </div>
</div>

<script>
const $ = s => document.querySelector(s);
const FORMAT = {
  brightness: v => v + "%", speed: v => (v / 100).toFixed(2) + "×", trail: v => v,
  count: v => v, level: v => v + "%", direction: v => v + "°",
};
let byId = {}, current = null, leds = [], buttons = {};

function buildRing(n) {
  const ring = $("#ring");
  for (let i = 0; i < n; i++) {
    const a = i / n * 2 * Math.PI - Math.PI / 2;
    const led = document.createElement("div");
    led.className = "led";
    led.style.left = 50 + 42 * Math.cos(a) + "%";
    led.style.top = 50 + 42 * Math.sin(a) + "%";
    led.title = "LED " + i;
    ring.appendChild(led);
    leds.push(led);
  }
  const idx = document.createElement("div");
  idx.className = "idx"; idx.textContent = "0";
  idx.style.left = "50%"; idx.style.top = "-1%";
  ring.appendChild(idx);
}

function buildList(catalog) {
  const list = $("#list"), groups = {};
  for (const p of catalog) {
    byId[p.id] = p;
    if (!groups[p.cat]) {
      const sec = document.createElement("section");
      sec.innerHTML = "<h2></h2><div class='grid'></div>";
      sec.querySelector("h2").textContent = p.cat;
      list.appendChild(sec);
      groups[p.cat] = sec;
    }
    const b = document.createElement("button");
    b.dataset.id = p.id;
    b.title = p.desc || p.label;
    b.textContent = p.label;
    b.onclick = () => choose(p.id);
    buttons[p.id] = b;
    groups[p.cat].querySelector(".grid").appendChild(b);
  }
  $("#total").textContent = catalog.length + " patterns";
}

function filter() {
  const q = $("#search").value.trim().toLowerCase();
  for (const sec of document.querySelectorAll("#list section")) {
    let shown = 0;
    for (const b of sec.querySelectorAll("button")) {
      const p = byId[b.dataset.id];
      const hit = !q || (p.label + " " + p.cat + " " + p.desc).toLowerCase().includes(q);
      b.hidden = !hit; shown += hit;
    }
    sec.hidden = !shown;
  }
}

function post(path, body) {
  return fetch(path, { method: "POST", headers: { "Content-Type": "application/json" },
                       body: JSON.stringify(body) }).then(r => r.json());
}

function choose(id) { post("/api/pattern", { pattern: id }); }

let pending = {}, timer = null;
function setting(key, value) {
  pending[key] = value;
  if (FORMAT[key]) $("#o-" + key).textContent = FORMAT[key](value);
  clearTimeout(timer);
  timer = setTimeout(() => { post("/api/settings", pending); pending = {}; }, 40);
}

function loadSettings(s) {
  for (const [k, v] of Object.entries(s)) {
    const el = document.getElementById(k);
    if (!el) continue;
    el.value = v;
    if (FORMAT[k]) $("#o-" + k).textContent = FORMAT[k](v);
    el.oninput = () => setting(k, el.type === "color" ? el.value : +el.value);
  }
}

const OFF = [28, 31, 37];
function render(st) {
  st.frame.forEach((hex, i) => {
    const v = parseInt(hex.slice(1), 16), led = leds[i];
    const rgb = [v >> 16, (v >> 8) & 255, v & 255];
    const k = Math.max(...rgb) / 255;
    if (k < 0.02) { led.style.background = ""; led.style.boxShadow = ""; return; }
    // screen-blend over the unlit color, so dim LEDs never look darker than off ones
    led.style.background = `rgb(${rgb.map((c, j) => OFF[j] + c * (1 - OFF[j] / 255))})`;
    led.style.boxShadow = `0 0 ${4 + 18 * k}px ${2 + 6 * k}px rgba(${rgb},${k})`;
  });
  if (st.pattern === current) return;
  if (current) buttons[current].classList.remove("active");
  current = st.pattern;
  const p = byId[current];
  buttons[current].classList.add("active");
  $("#name").textContent = p.label;
  $("#cat").textContent = p.cat;
  $("#desc").textContent = p.desc;
  for (const el of document.querySelectorAll("[data-use]"))
    el.hidden = !p.uses.includes(el.dataset.use);
}

function connect() {
  const es = new EventSource("/api/stream");
  es.onopen = () => { $("#conn").hidden = true; };
  es.onmessage = e => render(JSON.parse(e.data));
  es.onerror = () => { $("#conn").hidden = false; $("#conn").className = "badge down"; };
}

async function init() {
  buildList(await (await fetch("/api/patterns")).json());
  const st = await (await fetch("/api/state")).json();
  buildRing(st.frame.length);
  loadSettings(st.settings);
  const out = $("#output");
  out.textContent = st.output;
  if (st.output.startsWith("simulation")) out.classList.add("sim");
  render(st);
  $("#search").oninput = filter;
  connect();
}
init();
</script>
</body>
</html>
"""


# ============================================================================
# Main
# ============================================================================

def find_spidev():
    if os.path.exists(DEFAULT_SPI):
        return DEFAULT_SPI
    found = sorted(glob.glob("/dev/spidev*"))
    return found[0] if found else None


def main():
    ap = argparse.ArgumentParser(description="ReSpeaker Core v2 LED ring web UI")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--sim", action="store_true", help="no hardware, browser preview only")
    ap.add_argument("--spi", help="spidev device (default: %s, else first found)" % DEFAULT_SPI)
    ap.add_argument("--spi-hz", type=int, default=2_000_000)
    ap.add_argument("--power-line", default=POWER_LINE,
                    help="GPIO line name of the LED power switch (active low)")
    ap.add_argument("--no-power", action="store_true", help="do not touch the LED power line")
    ap.add_argument("--offset", type=int, default=0, help="physical LED shown as LED 0")
    ap.add_argument("--reverse", action="store_true", help="ring is wired counter-clockwise")
    ap.add_argument("--pattern", default="off", choices=sorted(PATTERNS), metavar="NAME",
                    help="pattern to start with")
    args = ap.parse_args()

    strip, power = Simulator(), None
    if not args.sim:
        path = args.spi or find_spidev()
        if path is None:
            print("No /dev/spidev* found: running in simulation (preview only).")
        else:
            if not args.no_power:
                power = LedPower(args.power_line)
                try:
                    power.on()
                except Exception as e:
                    print("Warning: LED power not switched on: %s" % e)
            try:
                strip = Apa102(path, args.spi_hz, args.offset, args.reverse)
            except OSError as e:
                sys.exit("Cannot open %s: %s (is the user in the 'spi' group?)" % (path, e))

    engine = Engine(strip)
    engine.set_pattern(args.pattern)
    threading.Thread(target=engine.run, daemon=True).start()

    Handler.engine = engine
    Handler.output = strip.how
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    print("Ring Studio: %d patterns" % len(PATTERNS))
    print("  LEDs:  %s" % strip.how)
    print("  Power: %s" % (power.how if power else "not controlled"))
    print("  Web:   http://%s:%d" % ("<board-ip>" if args.host == "0.0.0.0" else args.host, args.port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        engine.stop()
        time.sleep(2 / FPS)
        strip.close()
        if power:
            power.off()
        server.server_close()


if __name__ == "__main__":
    main()
