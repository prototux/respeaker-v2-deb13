#!/usr/bin/env python3
"""
ReSpeaker Core v2 - Mic View

Live web view of the microphone array: a top view of the board with each
microphone colored by its level, a VU meter and oscilloscope per channel,
direction of arrival (SRP-PHAT) of the loudest sound, and playback of any
selection of channels in the browser. Audio files dropped on the page play
on the board's speaker.

Sound card (Debian 13 image, /etc/asound.conf):
    "mics"   8 channels through dsnoop: 6 microphones + 2 loopback of the
             output (AEC reference). Other programs can record at the same time.

    "speaker" stereo output through dmix (headphone jack and speaker)

The board only runs `arecord` / `aplay` and relays raw 16-bit samples; all the
signal processing, and the decoding of dropped files (mp3, ogg, opus, flac,
wav... whatever the browser supports), runs in the browser. Dependencies:
Python 3.7+ and alsa-utils.

Run:
    python3 micview.py              # on the board
    python3 micview.py --sim        # anywhere: a synthetic talker circling the board

Then open http://<board-ip>:8081
"""

import argparse
import fcntl
import json
import math
import queue
import random
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from array import array
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


CHANNELS = 8
FRAME_BYTES = CHANNELS * 2          # S16_LE, interleaved
SPEED_OF_SOUND = 343.0

# Microphone positions in mm, from the board center: x to the right, y toward
# the edge with the Ethernet/USB connectors ("top"). MIC1..MIC6 run clockwise
# from top-left, as printed on the board (same geometry as Seeed's ODAS config).
MIC_POS = {1: (-23.2, 40.1), 2: (23.2, 40.1), 3: (46.3, 0.0),
           4: (23.2, -40.1), 5: (-23.2, -40.1), 6: (-46.3, 0.0)}


# ============================================================================
# Audio sources. Each one runs on its own thread and calls publish(bytes)
# with whole interleaved frames, or fail(message) if it dies.
# ============================================================================

class Arecord:
    def __init__(self, device, rate, publish, fail):
        cmd = ["arecord", "-q", "-D", device, "-c", str(CHANNELS), "-r", str(rate),
               "-f", "S16_LE", "-t", "raw"]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     bufsize=0)
        self.stopping = False
        threading.Thread(target=self._run, args=(publish, fail), daemon=True).start()

    def _run(self, publish, fail):
        rest = b""
        while True:
            data = self.proc.stdout.read(4096)
            if not data:
                break
            data = rest + data
            cut = len(data) - len(data) % FRAME_BYTES
            rest = data[cut:]
            if cut:
                publish(data[:cut])
        if not self.stopping:
            err = self.proc.stderr.read().decode(errors="replace").strip()
            fail("arecord stopped: %s" % (err or "exit code %s" % self.proc.wait()))

    def stop(self):
        self.stopping = True
        self.proc.terminate()


class Simulator:
    """
    A talker circling the board at 25 deg/s: band-limited noise in syllables
    and phrases, reaching each microphone with its true propagation delay.
    The loopback channels carry two quiet tones, as if music were playing.
    """
    CHUNK = 256

    def __init__(self, rate, order, publish, fail):
        self.rate = rate
        self.order = order
        self.bearing = 0.0
        self.stopping = False
        threading.Thread(target=self._run, args=(publish,), daemon=True).start()

    def _run(self, publish):
        rate, rng = self.rate, random.Random()
        lp1 = lp2 = 0.0
        hist = [0.0] * 16
        n = 0
        start = time.monotonic()
        while not self.stopping:
            t0 = n / rate
            self.bearing = (t0 * 25) % 360
            b = math.radians(self.bearing)
            ux, uy = math.sin(b), math.cos(b)
            # arrival delay of each channel, in samples, all positive
            delays = [8 - (MIC_POS[m][0] * ux + MIC_POS[m][1] * uy) / 1000 / SPEED_OF_SOUND * rate
                      for m in self.order]
            out = array("h")
            for k in range(self.CHUNK):
                t = (n + k) / rate
                talking = (t % 2.6) < 1.8
                env = (abs(math.sin(math.pi * 4 * t)) ** 0.6) if talking else 0.0
                w = rng.gauss(0, 1)
                lp1 += 0.6 * (w - lp1)
                lp2 += 0.05 * (lp1 - lp2)
                hist.append((lp1 - lp2) * env * 0.25)
                del hist[0]
                for d in delays:
                    i = int(d)
                    f = d - i
                    v = hist[-1 - i] * (1 - f) + hist[-2 - i] * f + rng.gauss(0, 0.0007)
                    out.append(max(-32767, min(32767, int(v * 32767))))
                out.append(int(1500 * math.sin(2 * math.pi * 440 * t)))
                out.append(int(1500 * math.sin(2 * math.pi * 660 * t)))
            n += self.CHUNK
            if sys.byteorder == "big":
                out.byteswap()
            publish(out.tobytes())
            time.sleep(max(0.0, start + n / rate - time.monotonic()))

    def stop(self):
        self.stopping = True


class Hub:
    """Runs the source only while someone listens, and fans chunks out to clients."""

    def __init__(self, make_source):
        self.make_source = make_source
        self.lock = threading.Lock()
        self.clients = set()
        self.source = None
        self.error = None
        self.first = None       # (time, frames) of the first chunk, for measured_rate()
        self.frames = 0

    def subscribe(self):
        q = queue.Queue(maxsize=64)
        with self.lock:
            self.clients.add(q)
            if self.source is None:
                self.error = None
                self.first, self.frames = None, 0
                self.source = self.make_source(self.publish, self.fail)
        return q

    def measured_rate(self):
        """Frames per second actually delivered, against the wall clock: shows a
        sound card whose clock is off (both directions share one I2S clock)."""
        with self.lock:
            if self.first is None or self.source is None:
                return None
            elapsed = time.monotonic() - self.first[0]
            return (self.frames - self.first[1]) / elapsed if elapsed >= 3 else None

    def unsubscribe(self, q):
        with self.lock:
            self.clients.discard(q)
            if not self.clients and self.source is not None:
                self.source.stop()
                self.source = None

    def publish(self, chunk):
        with self.lock:
            clients = list(self.clients)
            self.frames += len(chunk) // FRAME_BYTES
            if self.first is None:
                self.first = (time.monotonic(), self.frames)
        for q in clients:
            try:
                q.put_nowait(chunk)
            except queue.Full:          # client too slow: drop this chunk
                pass

    def fail(self, message):
        print(message)
        with self.lock:
            self.error = message
            self.source = None
            clients = list(self.clients)
        for q in clients:
            try:
                q.put_nowait(None)      # ends the stream
            except queue.Full:
                pass


# ============================================================================
# Speaker: files are decoded in the browser and arrive as raw S16_LE PCM.
# The upload is received into a read-ahead buffer, and a writer thread feeds
# aplay from it, so network hiccups don't starve the speaker.
# ============================================================================

class AplaySink:
    def __init__(self, device, rate, channels):
        # no -q: aplay then reports its underruns on stderr, which we count
        self.proc = subprocess.Popen(
            ["aplay", "-D", device, "-t", "raw", "-f", "S16_LE",
             "-r", str(rate), "-c", str(channels)],
            stdin=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        # A small pipe keeps little audio queued ahead of aplay: pause acts fast
        try:
            fcntl.fcntl(self.proc.stdin.fileno(), F_SETPIPE_SZ, 4096)
            self.pipe_bytes = fcntl.fcntl(self.proc.stdin.fileno(), F_GETPIPE_SZ)
        except OSError:
            self.pipe_bytes = 65536
        self.underruns = 0
        self.messages = []
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stderr(self):
        for line in self.proc.stderr:
            line = line.decode(errors="replace").strip()
            if "underrun" in line:
                self.underruns += 1
            elif line and not line.startswith("Playing raw data"):
                self.messages.append(line)

    def write(self, data):
        self.proc.stdin.write(data)

    def finish(self):
        """Waits until everything written has been played."""
        self.proc.stdin.close()
        if self.proc.wait() not in (0, -signal.SIGTERM):
            raise OSError(self.failure() or "aplay failed")

    def kill(self):
        self.proc.terminate()

    def failure(self):
        """aplay's own error message, if it died."""
        try:
            self.proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            return None
        time.sleep(0.05)                    # let the stderr thread catch up
        return self.messages[-1] if self.messages else None


class PacedSink:
    """--sim stand-in for aplay: plays nothing, at real-time speed, and counts
    an underrun whenever it runs dry like a real sound card would."""

    def __init__(self, rate, channels):
        self.byte_rate = rate * channels * 2
        self.start = None
        self.written = 0
        self.underruns = 0
        self.killed = threading.Event()

    def _wait(self):
        delay = self.start + self.written / self.byte_rate - time.monotonic()
        if self.killed.wait(max(0.0, delay)):
            raise BrokenPipeError

    def write(self, data):
        now = time.monotonic()
        if self.start is None:
            self.start = now
        elif now > self.start + self.written / self.byte_rate + 0.02:    # ran dry
            self.underruns += 1
            self.start = now - self.written / self.byte_rate
        self.written += len(data)
        self._wait()

    def finish(self):
        if self.start is not None:
            self._wait()

    def kill(self):
        self.killed.set()


class Session:
    """One file being played: a bounded read-ahead buffer between the upload
    (feed) and a writer thread (aplay)."""

    PREBUFFER = 1.0     # seconds received before playback starts
    AHEAD = 30.0        # at most this much buffered; the upload waits beyond

    def __init__(self, name, rate, channels, length, sink):
        self.name, self.length, self.sink = name, length, sink
        self.frame = channels * 2
        self.byte_rate = rate * self.frame
        self.piece = self.frame * max(1, rate // 50)        # 20 ms
        self.silence = bytes(self.piece)
        self.cond = threading.Condition()
        self.chunks = deque()                               # whole frames only
        self.buffered = self.written = 0
        self.eof = self.stopped = self.paused = False
        self.error = None
        self.done = threading.Event()
        threading.Thread(target=self._write, daemon=True).start()

    def feed(self, chunk):
        """Called by the upload; blocks while the buffer is full."""
        with self.cond:
            self.cond.wait_for(lambda: self.stopped or self.buffered < self.AHEAD * self.byte_rate)
            self.chunks.append(chunk)
            self.buffered += len(chunk)
            self.cond.notify_all()

    def end_of_input(self):
        with self.cond:
            self.eof = True
            self.cond.notify_all()

    def stop(self):
        with self.cond:
            self.stopped = True
            self.cond.notify_all()
        self.sink.kill()

    def pause(self, paused):
        with self.cond:
            self.paused = paused
            self.cond.notify_all()

    def _write(self):
        try:
            with self.cond:
                self.cond.wait_for(lambda: self.stopped or self.eof
                                   or self.buffered >= self.PREBUFFER * self.byte_rate)
            while True:
                with self.cond:
                    self.cond.wait_for(lambda: self.stopped or self.paused or self.chunks or self.eof)
                    if self.stopped:
                        return
                    if self.paused:
                        chunk = None
                    elif not self.chunks:
                        break
                    else:
                        chunk = self.chunks.popleft()
                        self.buffered -= len(chunk)
                        self.cond.notify_all()
                if chunk is None:
                    # paused: keep aplay fed with silence, so it never runs dry
                    self.sink.write(self.silence)
                    continue
                # 20 ms at a time, so pause and stop take effect quickly
                for off in range(0, len(chunk), self.piece):
                    if self.paused or self.stopped:
                        with self.cond:
                            self.chunks.appendleft(chunk[off:])
                            self.buffered += len(chunk) - off
                        break
                    self.sink.write(chunk[off:off + self.piece])
                    self.written += min(self.piece, len(chunk) - off)
            self.sink.finish()
        except OSError as e:                # includes a killed aplay
            if not self.stopped:
                failure = getattr(self.sink, "failure", lambda: None)
                self.error = failure() or str(e) or "playback failed"
        finally:
            self.done.set()

    def status(self):
        duration = self.length / self.byte_rate
        # written data minus what still sits in the pipe and aplay's buffer
        lag = getattr(self.sink, "pipe_bytes", 0) / self.byte_rate + 0.1
        played = duration if self.done.is_set() else max(0.0, self.written / self.byte_rate - lag)
        return {"name": self.name, "position": round(min(played, duration), 2),
                "duration": round(duration, 2), "buffer": round(self.buffered / self.byte_rate, 1),
                "underruns": self.sink.underruns, "paused": self.paused}


class Player:
    """One file at a time: starting a new one stops the current one."""

    def __init__(self, make_sink):
        self.make_sink = make_sink
        self.lock = threading.Lock()
        self.current = None

    def start(self, name, rate, channels, length):
        session = Session(name, rate, channels, length, self.make_sink(rate, channels))
        with self.lock:
            old, self.current = self.current, session
        if old:
            old.stop()
        return session

    def end(self, session):
        with self.lock:
            if self.current is session:
                self.current = None

    def stop(self):
        with self.lock:
            session, self.current = self.current, None
        if session:
            session.stop()

    def pause(self, paused):
        session = self.current
        if session:
            session.pause(paused)

    def status(self):
        session = self.current
        return session.status() if session else None


class Mixer:
    """An ALSA mixer control through amixer: raw steps, plus its dB scale if it has one."""

    def __init__(self, card, control):
        self.base = ["amixer", "-c", card]
        self.control = control
        self.state = None
        self.refresh()

    def refresh(self):
        try:
            out = subprocess.run(self.base + ["cget", "name=" + self.control],
                                 capture_output=True, text=True, timeout=3).stdout
        except (OSError, subprocess.TimeoutExpired):
            out = ""
        rng = re.search(r"values=(\d+),min=(-?\d+),max=(-?\d+)", out)
        cur = re.search(r": values=(-?\d+)", out)
        db = re.search(r"dBscale-min=(-?[\d.]+)dB,step=([\d.]+)dB", out)
        if not (rng and cur):
            self.state = None
            return
        self.count = int(rng.group(1))
        self.state = {"min": int(rng.group(2)), "max": int(rng.group(3)), "value": int(cur.group(1)),
                      "db_min": float(db.group(1)) if db else None,
                      "db_step": float(db.group(2)) if db else None}

    def set(self, value):
        if self.state is None:
            return
        value = max(self.state["min"], min(self.state["max"], int(value)))
        subprocess.run(self.base + ["-q", "cset", "name=" + self.control,
                                    ",".join([str(value)] * self.count)], timeout=3)
        self.refresh()


class FakeMixer:
    """--sim stand-in, shaped like the codec's Playback Volume."""

    def __init__(self):
        self.state = {"min": 0, "max": 31, "value": 21, "db_min": -17.25, "db_step": 0.75}

    def set(self, value):
        self.state["value"] = max(0, min(31, int(value)))


# ============================================================================
# Web server
# ============================================================================

F_SETPIPE_SZ, F_GETPIPE_SZ = 1031, 1032       # Linux fcntl
MAX_UPLOAD = 1 << 30            # 1 GiB of PCM, ~93 min of 48 kHz stereo


class Handler(BaseHTTPRequestHandler):
    hub = None
    player = None
    mixer = None
    info = {}

    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif path == "/api/info":
            info = dict(self.info, error=self.hub.error)
            source = self.hub.source
            if isinstance(source, Simulator):
                info["sim_bearing"] = source.bearing
            info["measured_rate"] = self.hub.measured_rate()
            info["playback"] = self.player.status()
            info["volume"] = self.mixer.state
            self._send(200, json.dumps(info), "application/json")
        elif path == "/api/audio":
            self._audio()
        else:
            self._send(404, '{"error": "not found"}', "application/json")

    def do_POST(self):
        url = urlparse(self.path)
        if url.path == "/api/play":
            self._play(parse_qs(url.query))
        elif url.path == "/api/stop":
            self.player.stop()
            self._send(200, '{"ok": true}', "application/json")
        elif url.path == "/api/pause":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                paused = bool(json.loads(self.rfile.read(length))["paused"])
            except (ValueError, KeyError, TypeError):
                return self._send(400, '{"error": "expected {\\"paused\\": true|false}"}', "application/json")
            self.player.pause(paused)
            self._send(200, json.dumps(self.player.status()), "application/json")
        elif url.path == "/api/volume":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                self.mixer.set(json.loads(self.rfile.read(length))["value"])
            except (ValueError, KeyError, TypeError, OSError, subprocess.TimeoutExpired) as e:
                return self._send(400, json.dumps({"error": str(e)}), "application/json")
            self._send(200, json.dumps(self.mixer.state), "application/json")
        else:
            self._send(404, '{"error": "not found"}', "application/json")

    def _play(self, query):
        """Body: raw S16_LE PCM. Answers once it has played, or was stopped."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
            rate = int(query.get("rate", ["48000"])[0])
            channels = int(query.get("channels", ["2"])[0])
            assert 0 < length <= MAX_UPLOAD and 8000 <= rate <= 192000 and channels in (1, 2)
        except (ValueError, AssertionError):
            return self._send(400, '{"error": "bad audio parameters"}', "application/json")
        name = query.get("name", ["audio"])[0][:200]
        try:
            session = self.player.start(name, rate, channels, length)
        except OSError as e:
            return self._send(500, json.dumps({"error": "cannot start aplay: %s" % e}),
                              "application/json")
        try:
            remaining, rest = length, b""
            while remaining > 0 and not session.stopped:
                chunk = self.rfile.read(min(1 << 16, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                data = rest + chunk                     # feed whole frames only
                cut = len(data) - len(data) % session.frame
                rest = data[cut:]
                if cut:
                    session.feed(data[:cut])
        except OSError:
            pass                                        # upload cut: play what arrived
        session.end_of_input()
        session.done.wait()
        self.player.end(session)
        result = {"result": "stopped" if session.stopped else "error" if session.error else "done",
                  "underruns": session.sink.underruns}
        if session.error:
            result["error"] = session.error
        try:
            self._send(200, json.dumps(result), "application/json")
        except OSError:
            pass

    def _audio(self):
        """Raw interleaved S16_LE frames, streamed until the client leaves."""
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        q = self.hub.subscribe()
        try:
            while True:
                chunk = q.get(timeout=5)
                if chunk is None:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, queue.Empty):
            pass
        finally:
            self.hub.unsubscribe(q)


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mic View</title>
<style>
:root {
  --bg: #0d0e11; --panel: #15171c; --line: #252930; --text: #e7e9ec;
  --muted: #8b919a; --chip: #1c1f25; --chip-hover: #252931; --accent: #38bdf8;
  --mic: #38bdf8; --loop: #c084fc;
}
* { box-sizing: border-box; }
[hidden] { display: none !important; }
body { margin: 0; background: var(--bg); color: var(--text);
       font: 15px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif; }
.wrap { max-width: 1280px; margin: 0 auto; padding: 16px; }
header { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 16px; }
h1 { font-size: 19px; margin: 0 6px 0 0; }
.badge { font-size: 12px; color: var(--muted); border: 1px solid var(--line);
         border-radius: 999px; padding: 2px 10px; }
.badge.live { color: #4ade80; border-color: #1d4d2e; }
.badge.bad { color: #f87171; border-color: #5c1d1d; }
.badge.sim { color: #fbbf24; border-color: #5c4712; }
.badge.paused { color: #fbbf24; border-color: #5c4712; }
header .pause { margin-left: auto; padding: 4px 12px; font-size: 13px; }
header .pause.on { border-color: #5c4712; color: #fbbf24; }
header .badge { max-width: 100%; overflow-wrap: anywhere; }
.layout { display: grid; grid-template-columns: minmax(300px, 440px) minmax(0, 1fr); gap: 16px; align-items: start; }
.layout > * { min-width: 0; }
@media (max-width: 900px) { .layout { grid-template-columns: minmax(0, 1fr); } }
.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 14px; padding: 16px; }
.panel h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); margin: 0 0 12px; }

#board { width: 100%; display: block; user-select: none; }
#board .pad { cursor: pointer; }
#board text { font: 4px system-ui, sans-serif; fill: var(--muted); }
#board .miclabel { font-size: 4.2px; font-weight: 600; fill: var(--text); cursor: pointer; }
.readout { display: flex; justify-content: space-between; align-items: baseline; margin: 10px 2px 14px; }
.readout b { font-size: 28px; font-variant-numeric: tabular-nums; }
.readout span { color: var(--muted); font-size: 13px; }
.hint { color: var(--muted); font-size: 12px; margin-top: 10px; }

.ctl { display: block; }
.ctl .lbl { display: flex; justify-content: space-between; color: var(--muted); font-size: 13px; margin-bottom: 6px; }
.ctl .lbl output { color: var(--text); font-variant-numeric: tabular-nums; }
input[type=range] { width: 100%; accent-color: var(--accent); }
.controls { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px 18px;
            align-items: end; margin-bottom: 14px; }
button { font: inherit; color: var(--text); background: var(--chip); border: 1px solid var(--line);
         border-radius: 9px; padding: 7px 12px; cursor: pointer; }
button:hover { background: var(--chip-hover); }

#channels { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(330px, 100%), 1fr)); gap: 10px; }
.ch { background: #111317; border: 1px solid var(--line); border-radius: 12px; padding: 10px 12px; }
.ch.on { border-color: var(--accent); }
.ch .head { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.ch .dot { width: 10px; height: 10px; border-radius: 50%; background: #2a2e36; flex: none; }
.ch .name { font-weight: 600; }
.ch .kind { color: var(--muted); font-size: 12px; }
.ch .db { margin-left: auto; color: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; min-width: 6.5em; text-align: right; }
.ch .listen { padding: 3px 10px; font-size: 12px; }
.ch.on .listen { background: #12303d; border-color: var(--accent); color: #bae6fd; }
.meter { position: relative; height: 8px; border-radius: 4px; background: #1c1f25; overflow: hidden; }
.meter .fill { position: absolute; inset: 0 auto 0 0; width: 0;
               background: linear-gradient(90deg, #16a34a 0%, #22c55e 60%, #eab308 82%, #ef4444 100%);
               background-size: var(--full, 300px) 100%; }
.meter .hold { position: absolute; top: 0; bottom: 0; width: 2px; background: #e7e9ec; left: 0; }
.meter.clip .hold { background: #ef4444; }
.column { display: grid; grid-template-columns: minmax(0, 1fr); gap: 16px; }
.drop { display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 2px;
        min-height: 92px; border: 1.5px dashed #343944; border-radius: 12px; cursor: pointer;
        text-align: center; padding: 12px; color: var(--muted); font-size: 13px; }
.drop b { color: var(--text); font-size: 15px; font-weight: 600; }
.drop:hover, .drop:focus-visible, body.dragging .drop { border-color: var(--accent); background: #12303d55; outline: none; }
.now { margin-top: 12px; }
.now .row { display: flex; align-items: center; gap: 10px; }
.now .title { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 600; }
.progress { height: 6px; border-radius: 3px; background: #1c1f25; overflow: hidden; margin: 8px 0 4px; }
.progress div { height: 100%; width: 0; background: var(--accent); }
.times { display: flex; justify-content: space-between; color: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; }
.msg { color: var(--muted); font-size: 13px; margin-top: 10px; min-height: 1.4em; overflow-wrap: anywhere; }
.msg.bad { color: #f87171; }
#volCtl { margin-top: 14px; }
canvas.scope { display: block; width: 100%; height: 56px; margin-top: 8px; border-radius: 6px; background: #0b0c0f; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Mic View</h1>
    <span class="badge" id="status">connecting…</span>
    <span class="badge" id="source"></span>
    <button id="pause" class="pause">Pause recording</button>
  </header>

  <div class="layout">
    <div class="column">
    <div class="panel">
      <h2>Board, top view</h2>
      <svg id="board" viewBox="-64 -62 128 124" role="img" aria-label="ReSpeaker Core v2 top view"></svg>
      <div class="readout">
        <div><span>Direction</span><br><b id="doaDeg">—</b></div>
        <div style="text-align:right"><span id="doaInfo">waiting for sound</span></div>
      </div>
      <label class="ctl"><div class="lbl">Direction gate <output id="o-gate"></output></div>
        <input type="range" id="gate" min="-80" max="-20" value="-55"></label>
      <div class="hint">0° is the Ethernet/USB edge, clockwise. The direction only updates when the
        microphones are louder than the gate. Click a microphone to listen to it.</div>
    </div>

    <div class="panel">
      <h2>Speaker</h2>
      <div class="drop" id="drop" tabindex="0" role="button">
        <b>Drop an audio file</b>
        <span>or click to choose · mp3, ogg, opus, flac, wav…</span>
      </div>
      <input type="file" id="file" accept="audio/*,.opus,.ogg,.flac,.mp3,.wav,.m4a" hidden>
      <div class="now" id="now" hidden>
        <div class="row"><span class="title" id="nowName"></span>
          <button id="pausePlay">Pause</button><button id="stopPlay">Stop</button></div>
        <div class="progress"><div id="nowBar"></div></div>
        <div class="times"><span id="nowPos">0:00</span><span id="nowBuf"></span><span id="nowDur">0:00</span></div>
      </div>
      <div class="msg" id="playMsg"></div>
      <label class="ctl" id="volCtl" hidden><div class="lbl">Speaker volume <output id="o-spk"></output></div>
        <input type="range" id="spk"></label>
    </div>
    </div>

    <div class="panel">
      <h2>Channels</h2>
      <div class="controls">
        <label class="ctl"><div class="lbl">Listen volume <output id="o-volume"></output></div>
          <input type="range" id="volume" min="0" max="24" value="6"></label>
        <label class="ctl"><div class="lbl">Scope zoom <output id="o-zoom"></output></div>
          <input type="range" id="zoom" min="0" max="6" value="2"></label>
        <div><button id="mute">Stop listening</button></div>
      </div>
      <div id="channels"></div>
      <div class="hint">One channel plays on both ears; two play as left / right; more are mixed.</div>
    </div>
  </div>
</div>

<script id="dsp">
// ---------------------------------------------------------------- DSP ----
// No DOM in this block: it is also run under node for testing.

function makeFFT(n) {
  const rev = new Uint32Array(n), bits = Math.log2(n);
  for (let i = 0; i < n; i++) {
    let r = 0;
    for (let b = 0; b < bits; b++) r |= ((i >> b) & 1) << (bits - 1 - b);
    rev[i] = r;
  }
  const cos = new Float64Array(n / 2), sin = new Float64Array(n / 2);
  for (let i = 0; i < n / 2; i++) { cos[i] = Math.cos(2 * Math.PI * i / n); sin[i] = Math.sin(2 * Math.PI * i / n); }
  return function fft(re, im) {          // in place, forward
    for (let i = 0; i < n; i++) {
      const j = rev[i];
      if (j > i) { let t = re[i]; re[i] = re[j]; re[j] = t; t = im[i]; im[i] = im[j]; im[j] = t; }
    }
    for (let size = 2; size <= n; size *= 2) {
      const half = size / 2, step = n / size;
      for (let i = 0; i < n; i += size) {
        for (let j = i, k = 0; j < i + half; j++, k += step) {
          const l = j + half;
          const tr = re[l] * cos[k] + im[l] * sin[k];
          const ti = im[l] * cos[k] - re[l] * sin[k];
          re[l] = re[j] - tr; im[l] = im[j] - ti;
          re[j] += tr; im[j] += ti;
        }
      }
    }
  };
}

// Direction of arrival by SRP-PHAT: for every candidate bearing, sum the
// phase-normalised cross-spectra of all microphone pairs, steered by the
// delays that bearing would produce. mics: [[x, y], ...] in meters, y toward 0°.
class Doa {
  constructor(rate, mics, { n = 512, step = 3, fmin = 200, fmax = 4000 } = {}) {
    this.n = n; this.step = step; this.angles = Math.round(360 / step);
    this.fft = makeFFT(n);
    this.win = new Float64Array(n).map((_, i) => 0.5 - 0.5 * Math.cos(2 * Math.PI * i / n));
    this.k0 = Math.max(1, Math.ceil(fmin * n / rate));
    this.k1 = Math.min(n / 2 - 1, Math.floor(fmax * n / rate));
    const nb = this.nb = this.k1 - this.k0 + 1;
    this.pairs = [];
    for (let i = 0; i < mics.length; i++) for (let j = i + 1; j < mics.length; j++) this.pairs.push([i, j]);
    const P = this.pairs.length, A = this.angles;
    this.cosT = new Float32Array(P * A * nb); this.sinT = new Float32Array(P * A * nb);
    this.pairs.forEach(([i, j], p) => {
      for (let a = 0; a < A; a++) {
        const th = a * step * Math.PI / 180, ux = Math.sin(th), uy = Math.cos(th);
        // t_i - t_j for a far-field source at bearing th (arrival t_m = -p_m.u / c)
        const delta = ((mics[j][0] - mics[i][0]) * ux + (mics[j][1] - mics[i][1]) * uy) / 343;
        const base = (p * A + a) * nb;
        for (let b = 0; b < nb; b++) {
          const ph = 2 * Math.PI * (this.k0 + b) * rate / n * delta;
          this.cosT[base + b] = Math.cos(ph); this.sinT[base + b] = Math.sin(ph);
        }
      }
    });
    this.buf = mics.map(() => new Float64Array(n));
    this.re = mics.map(() => new Float64Array(n));
    this.im = mics.map(() => new Float64Array(n));
    this.cr = new Float32Array(P * nb); this.ci = new Float32Array(P * nb);
    this.fill = 0;
  }

  // chunks: one Float32Array per microphone. Returns the last analysis, or null.
  push(chunks) {
    let out = null, off = 0;
    const len = chunks[0].length;
    while (off < len) {
      const take = Math.min(this.n - this.fill, len - off);
      for (let m = 0; m < chunks.length; m++) this.buf[m].set(chunks[m].subarray(off, off + take), this.fill);
      this.fill += take; off += take;
      if (this.fill === this.n) { out = this.analyze(); this.fill = 0; }
    }
    return out;
  }

  analyze() {
    const { n, nb, k0, angles: A, pairs } = this;
    let energy = 0;
    for (let m = 0; m < this.buf.length; m++) {
      const x = this.buf[m], re = this.re[m], im = this.im[m];
      for (let i = 0; i < n; i++) { energy += x[i] * x[i]; re[i] = x[i] * this.win[i]; im[i] = 0; }
      this.fft(re, im);
    }
    pairs.forEach(([i, j], p) => {
      const ri = this.re[i], ii = this.im[i], rj = this.re[j], ij = this.im[j];
      for (let b = 0; b < nb; b++) {
        const k = k0 + b;
        const ar = ri[k] * rj[k] + ii[k] * ij[k], ai = ii[k] * rj[k] - ri[k] * ij[k];
        const mag = Math.hypot(ar, ai) || 1e-12;
        this.cr[p * nb + b] = ar / mag; this.ci[p * nb + b] = ai / mag;
      }
    });
    const srp = new Float32Array(A);
    for (let a = 0; a < A; a++) {
      let s = 0;
      for (let p = 0; p < pairs.length; p++) {
        const base = (p * A + a) * nb, cb = p * nb;
        for (let b = 0; b < nb; b++) s += this.cr[cb + b] * this.cosT[base + b] - this.ci[cb + b] * this.sinT[base + b];
      }
      srp[a] = s / (pairs.length * nb);
    }
    let best = 0, mean = 0;
    for (let a = 0; a < A; a++) { mean += srp[a] / A; if (srp[a] > srp[best]) best = a; }
    return { srp, bearing: refine(srp, best) * this.step, confidence: srp[best] - mean,
             level: 10 * Math.log10(energy / (this.buf.length * n) + 1e-12) };
  }
}

// Sub-step peak position by parabolic interpolation, wrapping around.
function refine(v, i) {
  const A = v.length, l = v[(i + A - 1) % A], c = v[i], r = v[(i + 1) % A];
  const d = l - 2 * c + r;
  const off = d < 0 ? 0.5 * (l - r) / d : 0;
  return (i + Math.max(-0.5, Math.min(0.5, off)) + A) % A;
}

if (typeof module !== "undefined") module.exports = { makeFFT, Doa };
</script>

<script>
// ----------------------------------------------------------------- App ----
const $ = s => document.querySelector(s);
const NS = "http://www.w3.org/2000/svg";
const SCOPE_LEN = 2048;
const clamp = (x, lo = 0, hi = 1) => Math.max(lo, Math.min(hi, x));
const toDb = x => 10 * Math.log10(x + 1e-12);

let RATE = 16000, chans = [], doa = null, micIdx = [];
let doaMap = null, doaBearing = null, doaConf = 0, simBearing = null;
const ui = { gate: -55, volume: 6, zoom: 2 };

function vuColor(db, alpha = 1) {
  const t = clamp((db + 60) / 60);
  const hue = t < 0.65 ? 140 : 140 - (t - 0.65) / 0.35 * 140;
  return `hsla(${hue}, 85%, ${16 + 44 * t}%, ${alpha})`;
}

// ---------------------------------------------------------- Board view ----

function svg(tag, attrs, parent) {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  if (parent) parent.appendChild(el);
  return el;
}

function polar(r, deg) {             // bearing (0 = top, clockwise) -> svg x, y
  const a = deg * Math.PI / 180;
  return [r * Math.sin(a), -r * Math.cos(a)];
}

const board = {};
function buildBoard() {
  const root = $("#board");
  const defs = svg("defs", {}, root);
  const blur = svg("filter", { id: "glow", x: "-100%", y: "-100%", width: "300%", height: "300%" }, defs);
  svg("feGaussianBlur", { stdDeviation: "2.2" }, blur);

  // connectors on the top edge, behind the board
  svg("rect", { x: -17, y: -49.5, width: 16, height: 9, rx: 0.6, fill: "#3a3d44" }, root);
  svg("rect", { x: 3.5, y: -50.5, width: 12.5, height: 10, rx: 0.6, fill: "#9aa0a8" }, root);
  const eth = svg("text", { x: -9, y: -51.5, "text-anchor": "middle" }, root); eth.textContent = "Ethernet";
  const usb = svg("text", { x: 9.75, y: -52, "text-anchor": "middle" }, root); usb.textContent = "USB";

  // hexagonal board, corners left / right, rounded by a thick stroke
  const R = 48.5;
  const hex = [0, 60, 120, 180, 240, 300].map(a => {
    const r = a * Math.PI / 180; return `${R * Math.cos(r)},${R * Math.sin(r)}`;
  }).join(" ");
  svg("polygon", { points: hex, fill: "#16181b", stroke: "#16181b", "stroke-width": 4, "stroke-linejoin": "round" }, root);

  // white LED ring and its 12 LEDs (silkscreen 12 at the top, like a clock)
  svg("circle", { r: 41, fill: "none", stroke: "#d9dadc", "stroke-width": 5 }, root);
  board.leds = [];
  for (let k = 1; k <= 12; k++) {
    const [x, y] = polar(41, k * 30);
    const led = svg("rect", { x: x - 1.3, y: y - 1.3, width: 2.6, height: 2.6, rx: 0.4,
                              fill: "#b9bbbf", transform: `rotate(${k * 30} ${x} ${y})` }, root);
    led.appendChild(document.createElementNS(NS, "title")).textContent = "LED " + k;
    board.leds.push({ el: led, deg: k * 30 % 360 });
  }

  // polar direction plot inside the ring
  for (const r of [12, 24, 34]) svg("circle", { r, fill: "none", stroke: "#262a31", "stroke-width": 0.3 }, root);
  for (const d of [0, 90, 180, 270]) {
    const [x, y] = polar(34, d);
    svg("line", { x1: 0, y1: 0, x2: x, y2: y, stroke: "#262a31", "stroke-width": 0.3 }, root);
  }
  board.srp = svg("path", { fill: "rgba(56,189,248,.18)", stroke: "#38bdf8", "stroke-width": 0.5,
                            "stroke-linejoin": "round" }, root);
  board.sim = svg("line", { x1: 0, y1: 0, x2: 0, y2: 0, stroke: "#fbbf24", "stroke-width": 0.5,
                            "stroke-dasharray": "1.5 1", opacity: 0 }, root);
  board.arrow = svg("g", { opacity: 0 }, root);
  svg("line", { x1: 0, y1: 0, x2: 0, y2: -30, stroke: "#e7e9ec", "stroke-width": 1, "stroke-linecap": "round" }, board.arrow);
  svg("path", { d: "M0,-35 L-2.6,-29.5 L2.6,-29.5 Z", fill: "#e7e9ec" }, board.arrow);
  svg("circle", { r: 1.6, fill: "#e7e9ec" }, root);

  // microphones: glow, pad, label
  board.mics = [];
  chans.forEach((ch, c) => {
    if (!ch.pos) return;
    const x = ch.pos[0], y = -ch.pos[1];
    const deg = Math.atan2(x, -y) * 180 / Math.PI;
    const g = svg("g", { class: "pad" }, root);
    const glow = svg("circle", { cx: x, cy: y, r: 6, fill: "transparent", filter: "url(#glow)" }, g);
    const ring = svg("rect", { x: x - 3.6, y: y - 3.6, width: 7.2, height: 7.2, rx: 1.2, fill: "none",
                               stroke: "transparent", "stroke-width": 0.8, transform: `rotate(${deg} ${x} ${y})` }, g);
    const pad = svg("rect", { x: x - 2.4, y: y - 2.4, width: 4.8, height: 4.8, rx: 0.5, fill: "#c9a46a",
                              transform: `rotate(${deg} ${x} ${y})` }, g);
    const [lx, ly] = [x * 1.2, y * 1.2 + 1.4];
    const label = svg("text", { x: lx, y: ly, "text-anchor": "middle", class: "miclabel" }, g);
    label.textContent = ch.name;
    g.onclick = () => toggleListen(c);
    board.mics.push({ c, glow, ring, pad });
  });
}

function drawBoard() {
  for (const m of board.mics) {
    const ch = chans[m.c], t = clamp((ch.level + 60) / 60);
    m.pad.setAttribute("fill", t > 0.05 ? vuColor(ch.level) : "#c9a46a");
    m.glow.setAttribute("fill", vuColor(ch.level, t * t));
    m.ring.setAttribute("stroke", ch.listen ? "#38bdf8" : "transparent");
  }
  if (doaMap) {
    const A = doaMap.length, pts = [];
    for (let a = 0; a < A; a++) {
      const [x, y] = polar(4 + 30 * doaMap[a], a * 360 / A);
      pts.push(x.toFixed(2) + "," + y.toFixed(2));
    }
    board.srp.setAttribute("d", "M" + pts.join("L") + "Z");
    for (const led of board.leds) {
      const v = doaMap[Math.round(led.deg / 360 * A) % A] ** 3;
      led.el.setAttribute("fill", v > 0.08 ? `rgba(0,190,255,${0.25 + 0.75 * v})` : "#b9bbbf");
    }
  }
  board.arrow.setAttribute("opacity", doaBearing === null ? 0 : clamp(0.25 + doaConf * 6));
  if (doaBearing !== null) board.arrow.setAttribute("transform", `rotate(${doaBearing})`);
  if (simBearing !== null) {
    const [x, y] = polar(36, simBearing);
    board.sim.setAttribute("x2", x); board.sim.setAttribute("y2", y); board.sim.setAttribute("opacity", 1);
  }
}

// ------------------------------------------------------------ Channels ----

function buildChannels() {
  const list = $("#channels");
  chans.forEach((ch, c) => {
    const el = document.createElement("div");
    el.className = "ch";
    el.innerHTML = `<div class="head"><span class="dot"></span><span class="name"></span>
      <span class="kind"></span><span class="db"></span><button class="listen">Listen</button></div>
      <div class="meter"><div class="fill"></div><div class="hold"></div></div>
      <canvas class="scope"></canvas>`;
    el.querySelector(".name").textContent = ch.name;
    el.querySelector(".kind").textContent = "ch " + c;
    el.querySelector(".listen").onclick = () => toggleListen(c);
    list.appendChild(el);
    Object.assign(ch, { el, dot: el.querySelector(".dot"), db: el.querySelector(".db"),
                        meter: el.querySelector(".meter"), fill: el.querySelector(".fill"),
                        hold: el.querySelector(".hold"), canvas: el.querySelector("canvas") });
  });
}

function drawChannels() {
  const zoom = 2 ** ui.zoom;
  for (const ch of chans) {
    const pos = clamp((ch.level + 60) / 60), hold = clamp((ch.holdDb + 60) / 60);
    const full = ch.meter.clientWidth;
    ch.fill.style.width = pos * full + "px";
    ch.fill.style.setProperty("--full", full + "px");
    ch.hold.style.left = Math.min(full - 2, hold * full) + "px";
    ch.meter.classList.toggle("clip", ch.holdDb > -0.5);
    ch.dot.style.background = pos > 0.05 ? vuColor(ch.level) : "";
    ch.db.textContent = ch.level > -90 ? `${ch.level.toFixed(1)} dBFS` : "silent";

    const cv = ch.canvas, dpr = window.devicePixelRatio || 1;
    const w = Math.round(cv.clientWidth * dpr), h = Math.round(cv.clientHeight * dpr);
    if (cv.width !== w || cv.height !== h) { cv.width = w; cv.height = h; }
    const g = cv.getContext("2d");
    g.clearRect(0, 0, w, h);
    g.fillStyle = "#1c1f25"; g.fillRect(0, h / 2, w, Math.max(1, dpr / 2));
    g.fillStyle = ch.pos ? "#38bdf8" : "#c084fc";
    const n = 1024, per = n / w, mid = h / 2;
    for (let x = 0; x < w; x++) {              // min/max per pixel column
      let lo = 1, hi = -1;
      const s0 = Math.floor(x * per), s1 = Math.max(s0 + 1, Math.floor((x + 1) * per));
      for (let s = s0; s < s1; s++) {
        const v = ch.scope[(ch.w - n + s + SCOPE_LEN) % SCOPE_LEN];
        if (v < lo) lo = v; if (v > hi) hi = v;
      }
      const y0 = mid - clamp(hi * zoom, -1, 1) * (mid - 1), y1 = mid - clamp(lo * zoom, -1, 1) * (mid - 1);
      g.fillRect(x, y0, 1, Math.max(dpr, y1 - y0));
    }
  }
}

function toggleListen(c) {
  ensureAudio();
  chans[c].listen = !chans[c].listen;
  chans[c].el.classList.toggle("on", chans[c].listen);
}

// ------------------------------------------------------------ Playback ----

let actx = null, gain = null, nextT = 0;
function ensureAudio() {
  if (!actx) {
    actx = new (window.AudioContext || window.webkitAudioContext)();
    gain = actx.createGain();
    gain.connect(actx.destination);
    setVolume();
  }
  if (actx.state === "suspended") actx.resume();
}
function setVolume() { if (gain) gain.gain.value = 10 ** (ui.volume / 20); }

function play(per) {
  const sel = chans.map((ch, c) => ch.listen ? c : -1).filter(c => c >= 0);
  if (!sel.length || !actx) return;
  const frames = per[0].length;
  const buf = actx.createBuffer(2, frames, RATE);
  const L = buf.getChannelData(0), R = buf.getChannelData(1);
  if (sel.length === 2) {
    L.set(per[sel[0]]); R.set(per[sel[1]]);
  } else {
    const k = 1 / Math.sqrt(sel.length);
    for (let f = 0; f < frames; f++) {
      let s = 0;
      for (const c of sel) s += per[c][f];
      L[f] = R[f] = s * k;
    }
  }
  const src = actx.createBufferSource();
  src.buffer = buf;
  src.connect(gain);
  const now = actx.currentTime;
  if (nextT < now + 0.02 || nextT > now + 0.5) nextT = now + 0.12;   // underrun or drift
  src.start(nextT);
  nextT += frames / RATE;
}

// ----------------------------------------------------------- Streaming ----

function process(pcm) {
  const C = chans.length, frames = pcm.length / C, dt = frames / RATE, now = performance.now();
  const per = chans.map(() => new Float32Array(frames));
  for (let c = 0; c < C; c++) {
    const ch = chans[c], out = per[c];
    let x1 = ch.x1, y1 = ch.y1, sum = 0, peak = 0;
    for (let f = 0; f < frames; f++) {
      const x = pcm[f * C + c] / 32768;
      const y = x - x1 + 0.995 * y1;          // DC blocker
      x1 = x; y1 = y; out[f] = y;
      sum += y * y; const a = Math.abs(y); if (a > peak) peak = a;
      ch.scope[ch.w] = y; ch.w = (ch.w + 1) % SCOPE_LEN;
    }
    ch.x1 = x1; ch.y1 = y1;
    ch.level = Math.max(toDb(sum / frames), ch.level - 30 * dt);     // fast attack, 30 dB/s release
    const pk = toDb(peak * peak);                                    // peak, dBFS
    if (pk >= ch.holdDb || now - ch.holdT > 1500) { ch.holdDb = pk; ch.holdT = now; }
  }
  const res = doa.push(micIdx.map(c => per[c]));
  if (res) updateDoa(res);
  play(per);
}

function updateDoa(res) {
  const A = res.srp.length;
  if (!doaMap) doaMap = new Float32Array(A);
  if (res.level < ui.gate) {
    for (let a = 0; a < A; a++) doaMap[a] *= 0.92;
    doaConf *= 0.92;
    $("#doaInfo").textContent = `quiet (${res.level.toFixed(0)} dBFS)`;
    return;
  }
  let lo = Infinity, hi = -Infinity;
  for (const v of res.srp) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
  for (let a = 0; a < A; a++) doaMap[a] = 0.65 * doaMap[a] + 0.35 * ((res.srp[a] - lo) / (hi - lo || 1)) ** 2;
  doaBearing = smoothAngle(doaBearing, res.bearing, 0.35);
  doaConf = 0.7 * doaConf + 0.3 * res.confidence;
  $("#doaDeg").textContent = Math.round(doaBearing) % 360 + "°";
  $("#doaInfo").textContent = `confidence ${(doaConf * 100).toFixed(0)} · ${res.level.toFixed(0)} dBFS`;
}

function smoothAngle(prev, next, k) {
  if (prev === null) return next;
  const d = ((next - prev + 540) % 360) - 180;
  return (prev + k * d + 360) % 360;
}

function setStatus(text, cls) {
  const el = $("#status");
  el.textContent = text; el.className = "badge " + (cls || "");
}

let paused = false, abortCtl = null, resume = null;
function togglePause() {
  paused = !paused;
  const btn = $("#pause");
  btn.textContent = paused ? "Resume recording" : "Pause recording";
  btn.classList.toggle("on", paused);
  if (paused) {
    if (abortCtl) abortCtl.abort();
    for (const ch of chans) { ch.level = ch.holdDb = -120; ch.scope.fill(0); }
    setStatus("recording paused", "paused");
  } else if (resume) {
    resume();
  }
}

async function stream() {
  const frameBytes = chans.length * 2;
  for (;;) {
    if (paused) await new Promise(r => { resume = r; });
    abortCtl = new AbortController();
    try {
      const res = await fetch("/api/audio", { signal: abortCtl.signal });
      if (!res.ok) throw new Error("HTTP " + res.status);
      setStatus("live", "live");
      const reader = res.body.getReader();
      let rest = new Uint8Array(0);
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        let data = value;
        if (rest.length) { data = new Uint8Array(rest.length + value.length); data.set(rest); data.set(value, rest.length); }
        const whole = data.length - data.length % frameBytes;
        if (whole) process(new Int16Array(data.buffer.slice(data.byteOffset, data.byteOffset + whole)));
        rest = data.slice(whole);
      }
    } catch (e) { /* reconnect below */ }
    if (paused) continue;
    const info = await fetch("/api/info").then(r => r.json()).catch(() => null);
    setStatus(info && info.error ? info.error : "reconnecting…", "bad");
    await new Promise(r => setTimeout(r, 1500));
  }
}

// ------------------------------------------------- Speaker (dropped files) ----

const PLAY_RATE = 48000;            // the rate of the board's dmix: no resampling there
let decoding = false, playPaused = false;

function playMsg(text, bad) {
  const el = $("#playMsg");
  el.textContent = text || ""; el.className = "msg" + (bad ? " bad" : "");
}

const mmss = s => Math.floor(s / 60) + ":" + String(Math.floor(s % 60)).padStart(2, "0");

async function playFile(file) {
  if (!file || decoding) return;
  decoding = true;
  playMsg(`Decoding ${file.name}…`);
  let audio;
  try {
    const Offline = window.OfflineAudioContext || window.webkitOfflineAudioContext;
    // decodeAudioData resamples to the context's rate
    audio = await new Offline(2, 1, PLAY_RATE).decodeAudioData(await file.arrayBuffer());
  } catch (e) {
    decoding = false;
    return playMsg(`${file.name}: this browser cannot decode that format.`, true);
  }
  const L = audio.getChannelData(0), R = audio.numberOfChannels > 1 ? audio.getChannelData(1) : L;
  const pcm = new Int16Array(audio.length * 2);
  for (let i = 0; i < audio.length; i++) {
    pcm[2 * i] = Math.max(-1, Math.min(1, L[i])) * 32767;
    pcm[2 * i + 1] = Math.max(-1, Math.min(1, R[i])) * 32767;
  }
  decoding = false;
  playMsg("");
  showNow({ name: file.name, position: 0, duration: audio.duration });
  const q = new URLSearchParams({ name: file.name, rate: PLAY_RATE, channels: 2 });
  try {
    const res = await fetch("/api/play?" + q, {
      method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: pcm });
    const out = await res.json();
    if (out.error) playMsg(out.error, true);
  } catch (e) { /* stopped or replaced: the server closed the upload */ }
}

function showNow(pb) {
  $("#now").hidden = !pb;
  if (!pb) return;
  $("#nowName").textContent = $("#nowName").title = pb.name;
  $("#nowBar").style.width = (pb.duration ? 100 * pb.position / pb.duration : 0) + "%";
  $("#nowPos").textContent = mmss(pb.position);
  $("#nowDur").textContent = mmss(pb.duration);
  $("#nowBuf").textContent = pb.paused ? "paused" : pb.buffer === undefined ? "" : `buffer ${pb.buffer.toFixed(1)} s`;
  playPaused = !!pb.paused;
  $("#pausePlay").textContent = playPaused ? "Resume" : "Pause";
  if (pb.underruns) {
    playMsg(`${pb.underruns} underrun${pb.underruns > 1 ? "s" : ""}: the board receives the audio slower than it plays it, `
      + "so playback stalls. On Wi-Fi, pause the recording, or try: sudo iw dev wlan0 set power_save off", true);
  } else if ($("#playMsg").classList.contains("bad") && $("#playMsg").textContent.includes("underrun")) {
    playMsg("");
  }
}

let volume = null, volumeTimer = null;
function volumeLabel(v) {
  return volume.db_min === null ? String(v) : (volume.db_min + (v - volume.min) * volume.db_step).toFixed(1) + " dB";
}
function showVolume(state) {
  volume = state;
  $("#volCtl").hidden = !state;
  if (!state) return;
  const el = $("#spk");
  el.min = state.min; el.max = state.max;
  if (document.activeElement !== el) el.value = state.value;
  $("#o-spk").textContent = volumeLabel(+el.value);
}

function bindSpeaker() {
  const input = $("#file"), drop = $("#drop");
  drop.onclick = () => input.click();
  drop.onkeydown = e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); } };
  input.onchange = () => { playFile(input.files[0]); input.value = ""; };
  let depth = 0;      // dragenter/leave fire for every child element
  const files = e => e.dataTransfer && [...e.dataTransfer.types].includes("Files");
  window.addEventListener("dragenter", e => { if (files(e)) { depth++; document.body.classList.add("dragging"); } });
  window.addEventListener("dragleave", () => { if (--depth <= 0) { depth = 0; document.body.classList.remove("dragging"); } });
  window.addEventListener("dragover", e => { if (files(e)) e.preventDefault(); });
  window.addEventListener("drop", e => {
    e.preventDefault(); depth = 0; document.body.classList.remove("dragging");
    if (e.dataTransfer.files.length) playFile(e.dataTransfer.files[0]);
  });
  $("#stopPlay").onclick = () => fetch("/api/stop", { method: "POST" }).then(() => showNow(null));
  $("#pausePlay").onclick = () => fetch("/api/pause", { method: "POST", body: JSON.stringify({ paused: !playPaused }) })
    .then(r => r.json()).then(pb => { if (pb) showNow(pb); });
  $("#spk").oninput = e => {
    const v = +e.target.value;
    $("#o-spk").textContent = volumeLabel(v);
    clearTimeout(volumeTimer);
    volumeTimer = setTimeout(() => fetch("/api/volume", { method: "POST", body: JSON.stringify({ value: v }) })
      .then(r => r.json()).then(showVolume), 60);
  };
}

let sourceText = "";
function showRate(rate) {
  const el = $("#source");
  if (!rate) { el.textContent = sourceText; return; }
  const off = rate / RATE - 1;
  el.textContent = `${sourceText} · measured ${Math.round(rate)} Hz`;
  el.classList.toggle("bad", Math.abs(off) > 0.01);
  el.title = Math.abs(off) > 0.01
    ? `The sound card delivers ${(off * 100).toFixed(1)}% ${off < 0 ? "fewer" : "more"} samples than it should: `
      + "its clock is off, and playback on the speaker will be off by the same amount."
    : "Samples per second actually received, measured against the board's clock.";
}

async function pollInfo() {
  for (;;) {
    const info = await fetch("/api/info").then(r => r.json()).catch(() => null);
    if (info) {
      if (info.sim_bearing !== undefined) simBearing = info.sim_bearing;
      showNow(info.playback);
      showRate(info.measured_rate);
    }
    await new Promise(r => setTimeout(r, 250));
  }
}

function frame() {
  drawBoard();
  drawChannels();
  requestAnimationFrame(frame);
}

function bindControls() {
  const fmt = { gate: v => v + " dBFS", volume: v => (v > 0 ? "+" : "") + v + " dB", zoom: v => "×" + 2 ** v };
  for (const key of Object.keys(fmt)) {
    const el = $("#" + key);
    const update = () => { ui[key] = +el.value; $("#o-" + key).textContent = fmt[key](ui[key]); if (key === "volume") setVolume(); };
    el.oninput = update; update();
  }
  $("#mute").onclick = () => chans.forEach((ch, c) => { if (ch.listen) toggleListen(c); });
}

function setup(info) {
  RATE = info.rate;
  chans = info.channels.map(ch => ({ ...ch, level: -120, holdDb: -120, holdT: 0, listen: false,
                                     x1: 0, y1: 0, scope: new Float32Array(SCOPE_LEN), w: 0 }));
  micIdx = chans.map((ch, c) => ch.pos ? c : -1).filter(c => c >= 0);
  doa = new Doa(RATE, micIdx.map(c => [chans[c].pos[0] / 1000, chans[c].pos[1] / 1000]));
  const src = $("#source");
  src.textContent = sourceText = info.source;
  if (info.simulated) src.classList.add("sim");
  buildBoard();
  buildChannels();
  bindControls();
  bindSpeaker();
  $("#pause").onclick = togglePause;
  showVolume(info.volume);
  showNow(info.playback);
}

async function init() {
  setup(await fetch("/api/info").then(r => r.json()));
  requestAnimationFrame(frame);
  stream();
  pollInfo();
}
init();
</script>
</body>
</html>
"""


# ============================================================================
# Main
# ============================================================================

def main():
    ap = argparse.ArgumentParser(description="ReSpeaker Core v2 microphone array web view")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--device", default="mics", help="ALSA capture device (default: mics)")
    ap.add_argument("--rate", type=int, default=16000)
    ap.add_argument("--order", default="1,2,3,4,5,6",
                    help="microphone number (as printed on the board) on ALSA channels 0..5")
    ap.add_argument("--play-device", default="speaker",
                    help="ALSA playback device for dropped files (default: speaker)")
    ap.add_argument("--card", default="seeed8micvoicec", help="sound card for the volume control")
    ap.add_argument("--volume-control", default="Playback Volume",
                    help="mixer control behind the speaker volume slider")
    ap.add_argument("--sim", action="store_true", help="synthetic signals, no sound card")
    args = ap.parse_args()

    try:
        order = [int(m) for m in args.order.split(",")]
        assert sorted(order) == [1, 2, 3, 4, 5, 6]
    except (ValueError, AssertionError):
        sys.exit("--order must list the microphones 1..6 once each, e.g. 1,2,3,4,5,6")

    if args.sim:
        source = "simulation @ %d Hz" % args.rate
        make = lambda publish, fail: Simulator(args.rate, order, publish, fail)
    else:
        if not shutil.which("arecord"):
            sys.exit("arecord not found: sudo apt install alsa-utils (or use --sim)")
        source = "arecord -D %s @ %d Hz" % (args.device, args.rate)
        make = lambda publish, fail: Arecord(args.device, args.rate, publish, fail)

    channels = [{"name": "MIC%d" % m, "mic": m, "pos": MIC_POS[m]} for m in order]
    channels += [{"name": "Loopback L", "mic": None, "pos": None},
                 {"name": "Loopback R", "mic": None, "pos": None}]
    Handler.hub = Hub(make)
    if args.sim:
        Handler.player = Player(lambda rate, ch: PacedSink(rate, ch))
        Handler.mixer = FakeMixer()
    else:
        if not shutil.which("aplay"):
            sys.exit("aplay not found: sudo apt install alsa-utils (or use --sim)")
        Handler.player = Player(lambda rate, ch: AplaySink(args.play_device, rate, ch))
        Handler.mixer = Mixer(args.card, args.volume_control)
    Handler.info = {"rate": args.rate, "channels": channels, "source": source,
                    "simulated": args.sim}

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    print("Mic View: %s" % source)
    print("  Web: http://%s:%d" % ("<board-ip>" if args.host == "0.0.0.0" else args.host, args.port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        source_obj = Handler.hub.source
        if source_obj is not None:
            source_obj.stop()
        Handler.player.stop()
        server.server_close()


if __name__ == "__main__":
    main()
