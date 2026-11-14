# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Audio-reactive input: capture the default sink's monitor, measure bass/mid/treble.

Capture: `parec --device=@DEFAULT_MONITOR@` (PulseAudio or pipewire-pulse), else
`pw-record` with `stream.capture.sink=true`; 11025 Hz mono s16le, read on a thread.
Analysis: numpy rfft when numpy imports, otherwise a pure-Python Goertzel over a handful
of probe frequencies per band. Each band gets a beat envelope (fast attack, slow release)
normalised against a slowly decaying running peak, so quiet and loud music both move.
"""
from __future__ import annotations

import math
import shutil
import struct
import subprocess
import threading
from collections.abc import Callable

RATE = 11025
BLOCK = 512  # ~46 ms; 21.5 Hz bins
BANDS = {"bass": (40, 160), "mid": (300, 2000), "treble": (2500, 5000)}
PROBES = {"bass": (50, 70, 90, 120, 150), "mid": (400, 700, 1100, 1700),
          "treble": (2800, 3600, 4500)}

try:  # optional
    import numpy as _np
except Exception:  # pragma: no cover - depends on the machine
    _np = None


def goertzel_power(samples, freq: float, rate: int = RATE) -> float:
    n = len(samples)
    k = round(n * freq / rate)
    w = 2 * math.pi * k / n
    coeff = 2 * math.cos(w)
    s1 = s2 = 0.0
    for x in samples:
        s0 = x + coeff * s1 - s2
        s2, s1 = s1, s0
    return (s1 * s1 + s2 * s2 - coeff * s1 * s2) / (n * n)


def band_energies(samples, rate: int = RATE, use_numpy: bool | None = None) -> dict[str, float]:
    """Mean power per band for one block of float samples in -1..1 (Hann-windowed)."""
    n = len(samples)
    if n == 0:
        return {b: 0.0 for b in BANDS}
    use_np = (_np is not None) if use_numpy is None else (use_numpy and _np is not None)
    if use_np:
        x = _np.asarray(samples, dtype=_np.float32) * _np.hanning(n).astype(_np.float32)
        spec = _np.abs(_np.fft.rfft(x)) ** 2 / (n * n)
        freqs = _np.fft.rfftfreq(n, 1 / rate)
        out = {}
        for band, (lo, hi) in BANDS.items():
            sel = spec[(freqs >= lo) & (freqs < hi)]
            out[band] = float(sel.mean()) if sel.size else 0.0
        return out
    win = [0.5 - 0.5 * math.cos(2 * math.pi * i / (n - 1)) for i in range(n)] if n > 1 else [1]
    x = [s * w for s, w in zip(samples, win)]
    return {band: sum(goertzel_power(x, f, rate) for f in fs) / len(fs)
            for band, fs in PROBES.items()}


class Envelope:
    """Beat envelope over a power series: attack/release in seconds, peak-normalised."""

    def __init__(self, attack: float = 0.02, release: float = 0.25,
                 peak_decay: float = 4.0, floor: float = 1e-7) -> None:
        self.attack, self.release, self.peak_decay, self.floor = attack, release, peak_decay, floor
        self.level = 0.0
        self.peak = floor

    def feed(self, power: float, dt: float) -> float:
        amp = math.sqrt(max(power, 0.0))
        self.peak = max(amp, self.peak * math.exp(-dt / self.peak_decay), math.sqrt(self.floor))
        target = amp / self.peak
        tau = self.attack if target > self.level else self.release
        self.level += (target - self.level) * (1 - math.exp(-dt / max(tau, 1e-4)))
        return self.level


class AudioAnalyzer:
    def __init__(self, rate: int = RATE, block: int = BLOCK, use_numpy: bool | None = None,
                 gate: float = 1e-7) -> None:
        self.rate, self.block, self.use_numpy, self.gate = rate, block, use_numpy, gate
        self.env = {b: Envelope() for b in BANDS}
        self._lock = threading.Lock()
        self._buf: list[float] = []
        self._tail: bytes = b""

    def feed(self, samples) -> None:
        """Float samples in -1..1, any length."""
        self._buf.extend(samples)
        dt = self.block / self.rate
        while len(self._buf) >= self.block:
            blk, self._buf = self._buf[:self.block], self._buf[self.block:]
            e = band_energies(blk, self.rate, self.use_numpy)
            silent = sum(e.values()) < self.gate
            with self._lock:
                for b, env in self.env.items():
                    env.feed(0.0 if silent else e[b], dt)

    def feed_s16le(self, data: bytes) -> None:
        if self._tail:
            data = self._tail + data
            self._tail = b""
        if len(data) & 1:
            self._tail = data[-1:]
            data = data[:-1]
        n = len(data) // 2
        if n:
            self.feed([v / 32768.0 for v in struct.unpack(f"<{n}h", data)])

    def level(self, band: str = "bass") -> float:
        with self._lock:
            if band == "full":
                return max(e.level for e in self.env.values())
            return self.env[band].level


def capture_command(which: Callable[[str], str | None] = shutil.which) -> list[str] | None:
    if which("parec"):
        return ["parec", "--device=@DEFAULT_MONITOR@", "--format=s16le", f"--rate={RATE}",
                "--channels=1", "--latency-msec=30", "--client-name=asusius"]
    if which("pw-record"):
        return ["pw-record", "-P", "stream.capture.sink=true", "--rate", str(RATE),
                "--channels", "1", "--format", "s16", "-"]
    return None


def _strip_wav_header(buf: bytes) -> tuple[bool, bytes]:
    """Return (header_done, payload_or_pending). Supports full RIFF/WAVE subchunks and
    minimal 44-byte synthetic RIFF headers."""
    if len(buf) < 4:
        return False, buf
    if buf[:4] != b"RIFF":
        return True, buf
    if len(buf) < 12:
        return False, buf
    if buf[8:12] != b"WAVE":
        return (True, buf[44:]) if len(buf) >= 44 else (False, buf)
    pos = 12
    while pos + 8 <= len(buf):
        cid = buf[pos:pos + 4]
        size = int.from_bytes(buf[pos + 4:pos + 8], "little")
        if cid == b"data":
            return True, buf[pos + 8:]
        step = 8 + size + (size & 1)
        if step <= 0 or pos + step > 65536:
            break
        pos += step
    if len(buf) >= 65536:
        return True, buf[44:]
    return False, buf


class AudioCapture:
    """Runs the capture command and feeds an analyzer. `level()` is thread-safe."""

    def __init__(self, analyzer: AudioAnalyzer | None = None, command: list[str] | None = None,
                 popen=subprocess.Popen) -> None:
        self.analyzer = analyzer or AudioAnalyzer()
        self.command = command if command is not None else capture_command()
        self.popen = popen
        self.proc = None
        self.thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self.error: str | None = None if self.command else \
            "no audio capture tool (install pulseaudio-utils for parec, or pipewire for pw-record)"

    def start(self) -> None:
        with self._lock:
            if self.proc or not self.command:
                return
            self._stopping.clear()
            try:
                proc = self.popen(self.command, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
            except OSError as exc:
                self.error = f"{self.command[0]}: {exc}"
                return
            self.proc = proc
            t = threading.Thread(target=self._pump, args=(proc,), name="asusius-audio",
                                 daemon=True)
            self.thread = t
            t.start()

    def _pump(self, proc=None) -> None:
        proc = proc or self.proc
        if proc is None or proc.stdout is None:
            return
        out = proc.stdout
        head_done = False
        pending = b""
        while True:
            try:
                data = out.read(self.analyzer.block * 2)
            except (OSError, ValueError):
                break
            if not data:
                break
            if not head_done:
                pending += data
                head_done, rem = _strip_wav_header(pending)
                if not head_done:
                    continue
                pending = b""
                data = rem
            if data:
                self.analyzer.feed_s16le(data)
        if not head_done and pending:
            if pending[:4] == b"RIFF" and len(pending) >= 44:
                self.analyzer.feed_s16le(pending[44:])
            elif pending[:4] != b"RIFF":
                self.analyzer.feed_s16le(pending)
        rc = proc.poll()
        if not self._stopping.is_set() and rc not in (None, 0, -15):
            self.error = f"{self.command[0]} exited with {rc}"

    def stop(self) -> None:
        with self._lock:
            self._stopping.set()
            proc, t = self.proc, self.thread
        if proc:
            try:
                proc.terminate()
                proc.wait(2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(2)
            except OSError:
                pass
        if t:
            t.join(1)
        if proc and proc.stdout:
            try:
                proc.stdout.close()
            except OSError:
                pass
        with self._lock:
            if self.thread is t:
                self.thread = None
            if self.proc is proc:
                self.proc = None

    def level(self, band: str = "bass") -> float:
        return self.analyzer.level(band)
