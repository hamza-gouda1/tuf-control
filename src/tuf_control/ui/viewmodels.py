# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Display-free UI logic: formatting, history buffers, editors, validators.

Nothing here imports GTK, so it is unit-tested without a display.
"""
from __future__ import annotations

import colorsys
import math
import re
from collections import deque
from dataclasses import dataclass, field

from ..core import (
    BackendError,
    BackendUnavailable,
    FanCurve,
    FanPoint,
    GpuAction,
    GpuMode,
    KbdEffect,
    PowerLimit,
    PrivilegeRequired,
    Profile,
    Unsupported,
    ValidationError,
)

DASH = "–"

# -- formatting ---------------------------------------------------------------


def fmt_num(value: float | None, digits: int = 0) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return DASH
    return f"{value:.{digits}f}"


_NB_RE = re.compile(r"(?<=[\d%]) (?=[\d°%A-Za-zµ/])")


def nb(text: str) -> str:
    """No-break spaces inside number groups and between a number and its unit
    ("5 300 rpm", "12.4 °C", "42 %"), so a wrapping label never splits a reading across
    two lines. Spaces between words are left alone."""
    return _NB_RE.sub("\u00a0", text)


def norm_app_key(text: str) -> str:
    """One comparable form for an application id, desktop file id, StartupWMClass or
    executable name: lower case, no .desktop, only the last part of a reverse-DNS id,
    no separators ("app-gnome-tuf\\x2dcontrol\\x2dgui" scope -> "tufcontrolgui")."""
    t = text.strip().lower()
    if t.endswith(".desktop"):
        t = t[:-8]
    if t.count(".") >= 2:
        t = t.rsplit(".", 1)[-1]
    return re.sub(r"[^a-z0-9]", "", t)


OWN_APP_ID = "io.github.hamzagouda.TufControl"
OWN_APP_KEYS = {norm_app_key(k) for k in (OWN_APP_ID, "tuf-control", "tuf-control-gui")}


def desktop_index(entries) -> dict[str, object]:
    """key -> app info for (desktop id, StartupWMClass, executable basename) triples,
    first entry wins. `entries` yields (info, desktop_id, wm_class, exec_line)."""
    out: dict[str, object] = {}
    for info, did, wm, exec_line in entries:
        keys = [did, wm or ""]
        first = (exec_line or "").split(" ")[0]
        keys.append(first.rsplit("/", 1)[-1])
        for k in keys:
            nk = norm_app_key(k)
            if nk and nk not in out:
                out[nk] = info
    return out


def signal_refusal(pid: int | None, own_pids, kind: str = "proc") -> str | None:
    """Why Stop / Force Stop must not signal this target (None when it may)."""
    if pid == 1:
        return "PID 1 is the init system: stopping it would take the session down"
    if pid is not None and pid in own_pids:
        return "This is TUF Control itself"
    return None


def column_totals(cpu, mem_pct, swap_pct, drive_pct, gpu_pct, vram_pct) -> dict[str, str]:
    """Header second lines like Mission Center's ("4%"); '' while unknown."""
    def f(v):
        return "" if v is None else f"{v:.0f}%"
    return {"cpu": f(cpu), "mem": f(mem_pct), "swap": f(swap_pct), "disk": f(drive_pct),
            "gpu": f(gpu_pct), "vram": f(vram_pct)}


def fmt_rpm(value: int | None) -> str:
    return DASH if value is None else f"{int(value):,}".replace(",", " ")


def humanize_error(exc: BaseException) -> str:
    """One short, honest sentence for a toast."""
    msg = str(exc).strip() or type(exc).__name__
    if isinstance(exc, PrivilegeRequired):
        return f"Needs administrator rights ({msg})"
    if isinstance(exc, ValidationError):
        return f"Rejected: {msg}"
    if isinstance(exc, (BackendUnavailable, Unsupported)):
        return f"Not available: {msg}"
    if isinstance(exc, BackendError):
        return f"Failed: {msg}"
    return f"Error: {msg}"


# -- profiles ---------------------------------------------------------------------

PROFILE_INFO: dict[Profile, tuple[str, str, str]] = {
    Profile.QUIET: ("Quiet", "Cool and silent, lowest power",
                    "power-profile-power-saver-symbolic"),
    Profile.BALANCED: ("Balanced", "Everyday mix of speed and noise",
                       "power-profile-balanced-symbolic"),
    Profile.PERFORMANCE: ("Performance", "Full power, fans work harder",
                          "power-profile-performance-symbolic"),
}
PROFILE_ORDER = [Profile.QUIET, Profile.BALANCED, Profile.PERFORMANCE]


# -- telemetry history ---------------------------------------------------------------


class History:
    """Time-windowed samples for a sparkline."""

    def __init__(self, window_s: float = 120.0, maxlen: int = 1024) -> None:
        self.window_s = window_s
        self._data: deque[tuple[float, float]] = deque(maxlen=maxlen)

    def push(self, t: float, value: float | None) -> None:
        if value is None:
            return
        if self._data and t < self._data[-1][0]:
            return  # clocks never run backwards here
        self._data.append((t, float(value)))
        self._trim(t)

    def _trim(self, now: float) -> None:
        while self._data and now - self._data[0][0] > self.window_s:
            self._data.popleft()

    def __len__(self) -> int:
        return len(self._data)

    @property
    def last(self) -> float | None:
        return self._data[-1][1] if self._data else None

    def samples(self) -> list[tuple[float, float]]:
        return list(self._data)

    def normalized(self, now: float) -> list[tuple[float, float]]:
        """(x, v) with x in [0, 1] across the window ending at `now`."""
        start = now - self.window_s
        return [((t - start) / self.window_s, v) for t, v in self._data if t >= start]

    def bounds(self, floor: float | None = 0.0, min_span: float = 10.0) -> tuple[float, float]:
        """Y range with headroom; at least `min_span` tall so flat lines stay calm."""
        if not self._data:
            return (floor or 0.0, (floor or 0.0) + min_span)
        vals = [v for _, v in self._data]
        lo, hi = min(vals), max(vals)
        if hi - lo < min_span:
            mid = (hi + lo) / 2
            lo, hi = mid - min_span / 2, mid + min_span / 2
        pad = (hi - lo) * 0.12
        lo, hi = lo - pad, hi + pad
        if floor is not None and lo < floor:
            hi += floor - lo
            lo = floor
        return lo, hi


def smooth_values(points: list[tuple[float, float]], radius: int = 1) -> list[tuple[float, float]]:
    """Centred moving average over values (keeps x); calms integer sensor steps."""
    if radius <= 0 or len(points) < 3:
        return list(points)
    out = []
    n = len(points)
    for i, (x, _v) in enumerate(points):
        lo, hi = max(0, i - radius), min(n, i + radius + 1)
        out.append((x, sum(v for _, v in points[lo:hi]) / (hi - lo)))
    return out


def smooth_path(points: list[tuple[float, float]]) -> list[tuple[float, ...]]:
    """Catmull-Rom through points -> cubic Bezier segments (x1,y1,x2,y2,x,y)."""
    segs = []
    n = len(points)
    for i in range(n - 1):
        p0 = points[i - 1] if i > 0 else points[i]
        p1, p2 = points[i], points[i + 1]
        p3 = points[i + 2] if i + 2 < n else p2
        c1 = (p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6)
        c2 = (p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6)
        segs.append((*c1, *c2, *p2))
    return segs


def battery_icon(percent: int | None, status: str | None) -> str:
    if percent is None:
        return "ac-adapter-symbolic"
    level = max(0, min(100, int(round(percent / 10.0)) * 10))
    st = (status or "").lower()
    if st == "full" or (level == 100 and st in ("charging", "not charging")):
        return "battery-level-100-charged-symbolic"
    suffix = "-charging" if st in ("charging", "not charging") else ""
    return f"battery-level-{level}{suffix}-symbolic"


def power_state_text(status: str | None, percent: int | None) -> str:
    if percent is None:
        return "AC"
    st = (status or "").lower()
    if st in ("charging",):
        return f"{percent}% · charging"
    if st in ("not charging", "full"):
        return f"{percent}% · plugged in"
    return f"{percent}%"


# -- power limits -----------------------------------------------------------------------

LIMIT_INFO: dict[str, tuple[str, str]] = {
    "ppt_pl1_spl": ("CPU sustained power", "Long-term package limit (PL1)"),
    "ppt_pl2_sppt": ("CPU boost power", "Short-term package limit (PL2)"),
    "ppt_pl3_fppt": ("CPU peak power", "Very short boost limit"),
    "ppt_apu_sppt": ("APU boost power", "Short-term APU limit"),
    "ppt_platform_sppt": ("Platform boost power", "Short-term platform limit"),
    "nv_dynamic_boost": ("GPU dynamic boost", "Extra power shifted from CPU to GPU"),
    "nv_temp_target": ("GPU temperature target", "dGPU throttles above this"),
    "nv_tgp": ("GPU extra TGP", "Added on top of the base TGP"),
}


@dataclass
class LimitRow:
    name: str
    title: str
    subtitle: str
    value: int
    min: int
    max: int
    step: int
    default: int | None
    unit: str


@dataclass
class PowerLimitsView:
    available: bool
    reason: str = ""
    rows: list[LimitRow] = field(default_factory=list)


def explain_unavailable(cap_info: dict | None) -> str:
    """Pick the most useful reason from a capabilities() entry."""
    if not cap_info:
        return "No backend reported this feature."
    reasons = [r for r in (cap_info.get("unavailable") or {}).values() if r]
    for r in reasons:  # D-Bus/asusd reasons describe the firmware state best
        if "PPT" in r or "EnablePptGroup" in r or "firmware" in r:
            return r[0].upper() + r[1:]
    return (reasons[0][0].upper() + reasons[0][1:]) if reasons else "Unavailable."


def power_limits_view(cap_info: dict | None, limits: list[PowerLimit] | None) -> PowerLimitsView:
    if not cap_info or not cap_info.get("available") or not limits:
        return PowerLimitsView(False, explain_unavailable(cap_info))
    rows = []
    for lim in limits:
        if not lim.bounded or lim.current is None:
            continue
        title, sub = LIMIT_INFO.get(lim.name, (lim.display_name or lim.name, lim.name))
        unit = "°C" if "temp" in lim.name else "W"
        rows.append(LimitRow(lim.name, title, sub, lim.current, lim.min, lim.max,  # type: ignore
                             max(1, lim.increment), lim.default, unit))
    if not rows:
        return PowerLimitsView(False, "Firmware exposes the limits but no readable values.")
    order = list(LIMIT_INFO)
    rows.sort(key=lambda r: order.index(r.name) if r.name in order else len(order))
    return PowerLimitsView(True, "", rows)


def snap_to_step(value: float, lo: int, hi: int, step: int) -> int:
    step = max(1, step)
    v = lo + round((value - lo) / step) * step
    return int(max(lo, min(hi, v)))


# -- fan curve editor -----------------------------------------------------------------------


def pwm_to_pct(pwm: float) -> float:
    return pwm * 100.0 / FanCurve.PWM_RANGE[1]


def pct_to_pwm(pct: float) -> int:
    return int(round(max(0.0, min(100.0, pct)) * FanCurve.PWM_RANGE[1] / 100.0))


class FanCurveModel:
    """Editable 8-point curve. Every drag is clamped so the curve stays valid."""

    def __init__(self, curve: FanCurve) -> None:
        self.fan = curve.fan
        self.original = [(p.temp, p.pwm) for p in curve.points]
        self.points: list[tuple[int, int]] = list(self.original)
        self.enabled = curve.enabled
        self.original_enabled = curve.enabled

    @property
    def dirty(self) -> bool:
        return self.points != self.original or self.enabled != self.original_enabled

    def revert(self) -> None:
        self.points = list(self.original)
        self.enabled = self.original_enabled

    def commit(self) -> None:
        self.original = list(self.points)
        self.original_enabled = self.enabled

    def limits_for(self, i: int) -> tuple[int, int, int, int]:
        """(temp_lo, temp_hi, pwm_lo, pwm_hi) allowed for point i."""
        lo_t, hi_t = FanCurve.TEMP_RANGE
        lo_p, hi_p = FanCurve.PWM_RANGE
        n = len(self.points)
        # leave room for the remaining points on each side (temps strictly increase)
        t_lo = self.points[i - 1][0] + 1 if i > 0 else lo_t
        t_hi = self.points[i + 1][0] - 1 if i < n - 1 else hi_t
        t_lo = max(t_lo, lo_t + i)
        t_hi = min(t_hi, hi_t - (n - 1 - i))
        p_lo = self.points[i - 1][1] if i > 0 else lo_p
        p_hi = self.points[i + 1][1] if i < n - 1 else hi_p
        return t_lo, t_hi, p_lo, p_hi

    def drag(self, i: int, temp: float, pwm: float) -> tuple[int, int]:
        t_lo, t_hi, p_lo, p_hi = self.limits_for(i)
        t = int(round(max(t_lo, min(t_hi, temp))))
        p = int(round(max(p_lo, min(p_hi, pwm))))
        self.points[i] = (t, p)
        return self.points[i]

    def nearest(self, temp: float, pwm: float, max_dist: float,
                scale: tuple[float, float] = (1.0, 1.0)) -> int | None:
        """Index of the point closest to (temp, pwm) in scaled units, within max_dist."""
        best, best_d = None, max_dist
        for i, (t, p) in enumerate(self.points):
            d = math.hypot((t - temp) * scale[0], (p - pwm) * scale[1])
            if d <= best_d:
                best, best_d = i, d
        return best

    def to_curve(self) -> FanCurve:
        return FanCurve(self.fan, [FanPoint(t, p) for t, p in self.points],
                        self.enabled).validate()


@dataclass(frozen=True)
class CurveGeometry:
    """Maps curve data (temp, pwm) to canvas pixels and back."""

    width: float
    height: float
    left: float = 44.0
    right: float = 16.0
    top: float = 16.0
    bottom: float = 30.0

    @property
    def plot_w(self) -> float:
        return max(1.0, self.width - self.left - self.right)

    @property
    def plot_h(self) -> float:
        return max(1.0, self.height - self.top - self.bottom)

    def to_px(self, temp: float, pwm: float) -> tuple[float, float]:
        lo_t, hi_t = FanCurve.TEMP_RANGE
        x = self.left + (temp - lo_t) / (hi_t - lo_t) * self.plot_w
        y = self.top + (1 - pwm / FanCurve.PWM_RANGE[1]) * self.plot_h
        return x, y

    def to_data(self, x: float, y: float) -> tuple[float, float]:
        lo_t, hi_t = FanCurve.TEMP_RANGE
        temp = lo_t + (x - self.left) / self.plot_w * (hi_t - lo_t)
        pwm = (1 - (y - self.top) / self.plot_h) * FanCurve.PWM_RANGE[1]
        return temp, pwm

    @property
    def scale(self) -> tuple[float, float]:
        """Pixels per unit of (temp, pwm), for hit testing."""
        lo_t, hi_t = FanCurve.TEMP_RANGE
        return self.plot_w / (hi_t - lo_t), self.plot_h / FanCurve.PWM_RANGE[1]


def bubble_rect(x: float, y: float, tw: float, th: float, w: float, h: float,
                left: float, right: float) -> tuple[float, float, float, float]:
    """Where the fan-curve readout bubble ("62° · 62%") goes for a point at (x, y) on a
    w×h canvas: centred above the point (below it when there is no room), kept over the
    plot horizontally, and never outside the canvas, so it is not clipped at an edge."""
    bw, bh = tw + 16.0, th + 8.0
    bx = min(max(x - bw / 2, left), w - right - bw)
    bx = max(2.0, min(bx, w - bw - 2.0))
    by = y - th - 22.0
    if by <= 2.0:
        by = y + 14.0
    by = max(2.0, min(by, h - bh - 2.0))
    return bx, by, bw, bh


# -- battery -----------------------------------------------------------------------------

CHARGE_PRESETS = (60, 80, 100)
CHARGE_RANGE = (20, 100)


def validate_charge_limit(value: float) -> int:
    v = int(round(value))
    lo, hi = CHARGE_RANGE
    if not lo <= v <= hi:
        raise ValidationError(f"charge limit must be {lo}-{hi}%")
    return v


def charge_limit_hint(value: int) -> str:
    if value <= 60:
        return "Best for battery lifespan when mostly plugged in"
    if value <= 85:
        return "Good balance of lifespan and runtime"
    return "Full capacity, faster wear when kept at 100%"


def health_label(health_pct: float) -> str:
    if health_pct >= 90:
        return "Excellent"
    if health_pct >= 80:
        return "Good"
    if health_pct >= 65:
        return "Fair"
    return "Worn"


# -- gpu ------------------------------------------------------------------------------------

GPU_MODE_INFO: dict[GpuMode, tuple[str, str]] = {
    GpuMode.INTEGRATED: ("Integrated", "dGPU powered off. Longest battery life"),
    GpuMode.HYBRID: ("Hybrid", "dGPU wakes on demand. Recommended"),
    GpuMode.ASUS_MUX_DGPU: ("Ultimate", "dGPU drives the panel through the MUX. Lowest latency"),
    GpuMode.NVIDIA_NO_MODESET: ("NVIDIA no modeset", "Legacy mode"),
    GpuMode.VFIO: ("VFIO", "dGPU passed through to a VM"),
    GpuMode.ASUS_EGPU: ("eGPU", "External GPU"),
    GpuMode.NONE: ("Unknown", ""),
}
GPU_UI_ORDER = [GpuMode.INTEGRATED, GpuMode.HYBRID, GpuMode.ASUS_MUX_DGPU]


def gpu_switch_requirement(current: GpuMode, target: GpuMode) -> str | None:
    """What the user should expect before confirming: 'reboot', 'logout' or None."""
    if current is target:
        return None
    if GpuMode.ASUS_MUX_DGPU in (current, target):
        return "reboot"
    return "logout"


def gpu_action_text(action: GpuAction | None, pending: GpuMode | None = None) -> str | None:
    target = f" to switch to {GPU_MODE_INFO[pending][0]}" if pending is not None else ""
    return {
        GpuAction.REBOOT: f"Restart{target}",
        GpuAction.LOGOUT: f"Log out{target}",
        GpuAction.SWITCH_TO_INTEGRATED: "Switch to Integrated first",
        GpuAction.ASUS_EGPU_DISABLE: "Disconnect the eGPU first",
    }.get(action) if action is not None else None


DGPU_LOST_TEXT = ("GPU unavailable: the NVIDIA driver lost the GPU (runtime power error). "
                  "A restart usually recovers it.")
DGPU_POWER_TEXT = {
    "error": "Unavailable (lost by the driver)",
    "active": "Active", "suspended": "Sleeping", "off": "Powered off",
    "disabled": "Disabled in firmware", "mux-discreet": "Driving the display",
    "suspending": "Going to sleep", "resuming": "Waking up",
}


# -- keyboard -----------------------------------------------------------------------------------

AURA_MODES: dict[int, str] = {
    0: "Static", 1: "Breathe", 2: "Colour cycle", 3: "Rainbow wave", 4: "Star", 5: "Rain",
    6: "Highlight", 7: "Laser", 8: "Ripple", 10: "Pulse", 11: "Comet", 12: "Flash",
}
# which inputs each mode takes, as asusctl 6.3 builds them (asusctl/src/aura_cli.rs:
# SingleColour, TwoColourSpeed, SingleSpeed, SingleSpeedDirection, StarsTwoColour, ...)
MODE_USES_COLOUR2 = {1, 4}
MODE_USES_COLOUR1 = {0, 1, 4, 6, 7, 8, 10, 11, 12}
MODE_USES_SPEED = {1, 2, 3, 4, 5, 6, 7, 8}
MODE_USES_DIRECTION = {3}
# rog-aura AuraDeviceType::LaptopKeyboardTuf (asusd `DeviceType` 2). asusd drives it through
# the kernel's kbd_rgb_mode ("cmd mode red green blue speed") and writes
# [1, mode, colour1.r, colour1.g, colour1.b, speed]: colour2 is stored and read back over
# D-Bus but never reaches the keys, so its firmware Breathe is Colour 1 <-> off.
TUF_DEVICE_TYPE = 2
SPEEDS = [("Slow", 0), ("Medium", 1), ("Fast", 2)]
DIRECTIONS = [("Right", 0), ("Left", 1), ("Up", 2), ("Down", 3)]
BRIGHTNESS_NAMES = ["Off", "Low", "Med", "High"]
_RAW_SPEEDS = {0xE1: 0, 0xEB: 1, 0xF5: 2}


def speed_index(raw: int) -> int:
    """asusd reports speed as 0-2 or as the raw HID byte; map both to 0-2."""
    return _RAW_SPEEDS.get(raw, raw if raw in (0, 1, 2) else 1)


def rgb_to_hex(rgb) -> str:
    return "#{:02X}{:02X}{:02X}".format(*(int(c) for c in rgb))


def hex_to_rgb(text: str) -> tuple[int, int, int]:
    t = text.strip().lstrip("#")
    if len(t) == 3:
        t = "".join(c * 2 for c in t)
    if len(t) != 6:
        raise ValidationError(f"bad colour {text!r}")
    try:
        return tuple(int(t[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        raise ValidationError(f"bad colour {text!r}") from None


def effect_from_rgb_data(data: dict | None) -> KbdEffect:
    if not data:
        return KbdEffect(0)
    return KbdEffect(int(data.get("mode", 0)), int(data.get("zone", 0)),
                     tuple(data.get("colour1", (142, 42, 68))),
                     tuple(data.get("colour2", (0, 0, 0))),
                     int(data.get("speed", 1)), int(data.get("direction", 0)))


def hardware_two_colour(device_type) -> bool:
    """Whether the keyboard's own firmware shows colour2 (False on TUF; unknown: True)."""
    return device_type is None or int(device_type) != TUF_DEVICE_TYPE


# The software Gradient breathing cycle (A -> B -> A) for each hardware speed.
BREATHE_PERIODS = (6.0, 4.0, 2.5)
GRADIENT_DIP = 0.3   # default darkening at the crossing (matches the effect's default)
DEFAULT_COLOUR2 = (201, 174, 134)  # gold: Breathe never starts as "Colour 1 <-> black"

# quick two-colour palettes: (name, colour 1, colour 2)
PALETTES: list[tuple[str, tuple[int, int, int], tuple[int, int, int]]] = [
    ("Wine & Gold", (142, 42, 68), (201, 174, 134)),
    ("Wine & Ivory", (142, 42, 68), (242, 232, 218)),
    ("Ice", (47, 111, 223), (191, 230, 255)),
    ("Sunset", (255, 94, 58), (122, 28, 52)),
    ("Ocean", (11, 79, 138), (34, 193, 195)),
    ("Neon", (255, 43, 214), (0, 229, 255)),
]


@dataclass(frozen=True)
class KbdControls:
    """Which rows the hardware-effect form shows for a mode, and what the mode does."""

    colour1: bool
    colour2: bool
    speed: bool
    direction: bool
    blend: bool          # offer the "Two colours" switch (single-colour firmware Breathe)
    palettes: bool
    colour1_title: str
    summary: str
    breathe: str         # preview style: alternate | single | blend


_SUMMARIES = {
    0: "Static: one steady colour.",
    2: "Colour cycle: walks round the colour wheel. Speed only.",
    3: "Rainbow wave: a rainbow moving across the keys.",
    4: "Stars: Colour 1 and Colour 2 twinkle.",
    5: "Rain: colours fall down the keys.",
    6: "Highlight: the colour follows the keys.",
    7: "Laser: the colour sweeps across.",
    8: "Ripple: the colour ripples out.",
    10: "Pulse: the colour pulses.",
    11: "Comet: the colour streaks across.",
    12: "Flash: the colour blinks.",
}


def kbd_controls(mode: int, device_type=None, blend: bool = False) -> KbdControls:
    hw2 = hardware_two_colour(device_type)
    offer_blend = mode == 1 and not hw2
    two = mode in MODE_USES_COLOUR2 and (hw2 or (offer_blend and blend))
    if mode == 1 and hw2:
        summary, style = "Breathe: Colour 1 \u2194 Colour 2.", "alternate"
    elif mode == 1 and two:
        summary, style = "Breathe: Colour 1 \u2194 Colour 2, a smooth fade.", "blend"
    elif mode == 1:
        summary, style = ("Breathe: colour \u2194 off. This keyboard's firmware has one "
                          "colour slot."), "single"
    else:
        summary, style = _SUMMARIES.get(mode, AURA_MODES.get(mode, "Effect")), "alternate"
        if mode == 4 and not hw2:
            summary = ("Stars: the colour twinkles (this keyboard's firmware has one "
                       "colour slot).")
    return KbdControls(
        colour1=mode in MODE_USES_COLOUR1, colour2=two, speed=mode in MODE_USES_SPEED,
        direction=mode in MODE_USES_DIRECTION, blend=offer_blend, palettes=two,
        colour1_title="Colour 1" if two else "Colour", summary=summary, breathe=style)


def gradient_params(effect: KbdEffect, dip: float = GRADIENT_DIP) -> dict:
    """Gradient breathing parameters for a two-colour Breathe."""
    return {"colour1": rgb_to_hex(effect.colour1).lower(),
            "colour2": rgb_to_hex(effect.colour2).lower(),
            "period": BREATHE_PERIODS[speed_index(effect.speed)], "dip": dip}


def _period_speed(period) -> int:
    try:
        p = float(period)
    except (TypeError, ValueError):
        return 1
    return min(range(3), key=lambda i: abs(BREATHE_PERIODS[i] - p))


@dataclass(frozen=True)
class KbdPlan:
    hardware: KbdEffect
    software: dict | None   # {"active": "gradient", "params": {...}} or None (effects off)


class KbdForm:
    """The hardware-effect form without GTK: per-mode drafts so switching modes keeps the
    colours you picked, mode-dependent controls, the dirty flag and what Apply writes."""

    def __init__(self) -> None:
        self.saved: KbdEffect | None = None
        self.current = KbdEffect(0)
        self.builtins: dict[int, KbdEffect] = {}
        self.drafts: dict[int, KbdEffect] = {}
        self.modes: list[int] = [0]
        self.device_type = None
        self.blend = self.saved_blend = False
        self.raw_speed = False

    def load(self, info: dict, soft: dict | None = None) -> None:
        """info: Controller.get_kbd_rgb(); soft: the config's effects section."""
        saved = effect_from_rgb_data(info.get("mode_data"))
        self.device_type = info.get("device_type")
        # asusd keeps colours/speed per mode: switching the picker shows that mode's own
        self.builtins = {int(m): effect_from_rgb_data(d)
                         for m, d in (info.get("builtins") or {}).items()}
        self.builtins[saved.mode] = saved
        modes = [int(m) for m in (info.get("supported_modes") or [saved.mode])]
        self.modes = [m for m in modes if m in AURA_MODES] or [saved.mode]
        self.blend = False
        fx = soft or {}
        g = (fx.get("params") or {}).get("gradient") or {}
        if fx.get("active") == "gradient" and 1 in self.modes \
                and not hardware_two_colour(self.device_type):
            # the keyboard shows Breathe's two-colour fade (frames are Static writes)
            base = self.builtins.get(1, KbdEffect(1))
            try:
                c1 = hex_to_rgb(g.get("colour1", "#8e2a44"))
                c2 = hex_to_rgb(g.get("colour2", "#c9ae86"))
            except ValidationError:
                c1, c2 = base.colour1, base.colour2
            speed = _period_speed(g.get("period", 4.0))
            if base.speed not in (0, 1, 2):
                speed = (0xE1, 0xEB, 0xF5)[speed]
            saved = KbdEffect(1, base.zone, c1, c2, speed, base.direction)
            self.builtins[1] = saved
            self.blend = True
        self.saved_blend = self.blend
        self.saved = saved
        self.current = saved
        self.drafts = {}
        self.raw_speed = saved.speed not in (0, 1, 2)

    @property
    def controls(self) -> KbdControls:
        return kbd_controls(self.current.mode, self.device_type, self.blend)

    @property
    def dirty(self) -> bool:
        return self.saved is not None and (self.current != self.saved
                                           or self.blend != self.saved_blend)

    def _fill_colour2(self, eff: KbdEffect) -> KbdEffect:
        if eff.mode in MODE_USES_COLOUR2 and tuple(eff.colour2) == (0, 0, 0):
            c2 = DEFAULT_COLOUR2 if tuple(eff.colour1) != DEFAULT_COLOUR2 else (242, 232, 218)
            return KbdEffect(eff.mode, eff.zone, eff.colour1, c2, eff.speed, eff.direction)
        return eff

    def select(self, mode: int) -> KbdEffect:
        if mode == self.current.mode:
            return self.current
        self.drafts[self.current.mode] = self.current
        new = self.drafts.get(mode) or self.builtins.get(mode)
        if new is None:  # never set: carry the colours over
            c = self.current
            new = KbdEffect(mode, c.zone, c.colour1, c.colour2, c.speed, c.direction)
        self.current = self._fill_colour2(new) if mode != (self.saved and self.saved.mode) \
            else new
        return self.current

    def edit(self, colour1=None, colour2=None, speed_idx: int | None = None,
             direction: int | None = None) -> KbdEffect:
        c = self.current
        speed = c.speed
        if speed_idx is not None:
            speed = (0xE1, 0xEB, 0xF5)[speed_idx] if self.raw_speed else speed_idx
        self.current = KbdEffect(c.mode, c.zone,
                                 tuple(colour1) if colour1 is not None else c.colour1,
                                 tuple(colour2) if colour2 is not None else c.colour2,
                                 speed, c.direction if direction is None else direction)
        return self.current

    def palette(self, index: int) -> KbdEffect:
        _name, a, b = PALETTES[index]
        return self.edit(colour1=a, colour2=b)

    def set_blend(self, on: bool) -> KbdEffect:
        self.blend = bool(on)
        if on:
            self.current = self._fill_colour2(self.current)
        return self.current

    def plan(self, dip: float = GRADIENT_DIP) -> KbdPlan:
        eff = self.current
        if self.controls.blend and self.blend:
            return KbdPlan(eff, {"active": "gradient", "params": gradient_params(eff, dip)})
        return KbdPlan(eff, None)

    def applied(self, plan: KbdPlan) -> None:
        self.saved = self.current = plan.hardware
        self.saved_blend = self.blend
        self.builtins[plan.hardware.mode] = plan.hardware
        self.drafts.pop(plan.hardware.mode, None)


def preview_colour(effect: KbdEffect, t: float, x: float = 0.0,
                   brightness: int = 3, breathe: str = "alternate") -> tuple[float, float, float]:
    """Approximate colour (0..1 floats) of the keyboard at time t and position x in [0,1].

    breathe: "alternate" fades Colour 1 -> off -> Colour 2 -> off (two-colour firmware),
    "single" Colour 1 <-> off (TUF firmware), "blend" the software Gradient breathing."""
    period = (4.0, 2.5, 1.4)[speed_index(effect.speed)]
    c1 = tuple(c / 255 for c in effect.colour1)
    c2 = tuple(c / 255 for c in effect.colour2)
    m = effect.mode
    if m in (2, 3):  # colour cycle / rainbow wave
        sign = -1 if effect.direction == 1 else 1
        hue = (t / period * 0.25 + (sign * x * 0.6 if m == 3 else 0.0)) % 1.0
        col = colorsys.hsv_to_rgb(hue, 0.85, 1.0)
    elif m == 1 and breathe == "blend":
        from ..effects.plugins import two_colour_breathe
        rgb = two_colour_breathe(effect.colour1, effect.colour2, t,
                                 BREATHE_PERIODS[speed_index(effect.speed)], GRADIENT_DIP)
        col = tuple(c / 255 for c in rgb)
    elif m == 1:  # breathe: fade c1 -> off -> c2 -> off (or c1 <-> off)
        phase = (t / period) % 2.0
        k = 0.5 - 0.5 * math.cos(math.pi * 2 * (phase % 1.0))
        base = c1 if phase < 1.0 or breathe == "single" else (c2 if any(c2) else c1)
        col = tuple(c * k for c in base)
    elif m == 10:  # pulse
        k = 0.35 + 0.65 * (0.5 - 0.5 * math.cos(math.pi * 2 * t / period))
        col = tuple(c * k for c in c1)
    elif m == 12:  # flash
        col = c1 if (t / period * 2) % 1.0 < 0.5 else (0.0, 0.0, 0.0)
    else:
        col = c1
    level = (0.0, 0.45, 0.72, 1.0)[max(0, min(3, brightness))]
    return tuple(c * level for c in col)  # type: ignore[return-value]


ANIMATED_MODES = frozenset({1, 2, 3, 10, 12})  # breathe, cycle, rainbow, pulse, flash


def is_animated(effect: KbdEffect) -> bool:
    """False when preview_colour does not depend on time (it only needs drawing once)."""
    return effect.mode in ANIMATED_MODES


class FrameGate:
    """Frame limiter and dirty flag for a live preview (no GTK, so it can be tested).

    Runs at `fast_fps` (15), drops to `slow_fps` (10) when a frame costs more than
    `slow_cost_s` on average, and backs off to `idle_hz` once `settle_frames` frames in a row
    produced nothing new. `observe(key, cost)` is the dirty flag: it returns True only when
    `key` differs from the last one, and any change wakes the gate back to full speed.
    """

    def __init__(self, fast_fps: float = 15.0, slow_fps: float = 10.0, idle_hz: float = 4.0,
                 settle_frames: int = 30, slow_cost_s: float = 0.020) -> None:
        self.fast, self.slow, self.idle = 1 / fast_fps, 1 / slow_fps, 1 / idle_hz
        self.settle_frames, self.slow_cost = settle_frames, slow_cost_s
        self.key: object = None
        self.unchanged = 0
        self.cost = 0.0
        self.last = -1e9

    @property
    def interval(self) -> float:
        if self.unchanged >= self.settle_frames:
            return self.idle
        return self.slow if self.cost > self.slow_cost else self.fast

    def due(self, now: float) -> bool:
        """True at most once per `interval`; the caller then does the frame."""
        if now - self.last >= self.interval - 0.002:  # 2 ms of timer jitter
            self.last = now
            return True
        return False

    def add_cost(self, cost: float) -> None:
        self.cost = cost if self.cost == 0.0 else self.cost * 0.8 + cost * 0.2

    def observe(self, key, cost: float = 0.0) -> bool:
        if cost:
            self.add_cost(cost)
        changed = key != self.key
        self.key = key
        self.unchanged = 0 if changed else self.unchanged + 1
        return changed

    def wake(self) -> None:
        self.unchanged = 0
        self.key = object()  # nothing equals it: the next observe() is a change


# -- display -----------------------------------------------------------------------------------


@dataclass
class DisplayMode:
    id: str
    width: int
    height: int
    refresh: float
    current: bool = False
    preferred: bool = False

    @property
    def label(self) -> str:
        return f"{self.refresh:.0f} Hz" if abs(self.refresh - round(self.refresh)) < 0.05 \
            else f"{self.refresh:.2f} Hz"


@dataclass
class Monitor:
    connector: str
    name: str
    builtin: bool
    modes: list[DisplayMode]
    vrr_supported: bool = False

    @property
    def current(self) -> DisplayMode | None:
        return next((m for m in self.modes if m.current), None)

    def refresh_options(self) -> list[DisplayMode]:
        """Modes at the current resolution, one per distinct refresh rate, fastest first."""
        cur = self.current
        if cur is None:
            return []
        seen: dict[int, DisplayMode] = {}
        for m in self.modes:
            if (m.width, m.height) != (cur.width, cur.height):
                continue
            key = round(m.refresh * 10)
            if key not in seen or m.current:
                seen[key] = m
        return sorted(seen.values(), key=lambda m: -m.refresh)


def parse_display_state(state) -> list[Monitor]:
    """Parse org.gnome.Mutter.DisplayConfig.GetCurrentState() output."""
    _serial, monitors, _logical, _props = state
    out = []
    for spec, modes, props in monitors:
        connector, vendor, product, _serialno = spec
        ms, vrr = [], False
        for mid, w, h, r, _ps, _scales, mp in modes:
            if mp.get("refresh-rate-mode") == "variable":
                vrr = True  # GNOME 46+ lists VRR as separate modes
                continue
            ms.append(DisplayMode(mid, int(w), int(h), float(r), bool(mp.get("is-current")),
                                  bool(mp.get("is-preferred"))))
        name = props.get("display-name") or f"{vendor} {product}".strip()
        out.append(Monitor(connector, name, bool(props.get("is-builtin")), ms,
                           vrr or bool(props.get("is-vrr-allowed", False))))
    return out


def build_apply_config(state, connector: str, mode_id: str):
    """Logical-monitor list for ApplyMonitorsConfig keeping everything but one mode."""
    _serial, monitors, logical, _props = state
    current_mode = {}
    for spec, modes, _p in monitors:
        cur = next((m[0] for m in modes if m[6].get("is-current")), None)
        current_mode[spec[0]] = cur
    if connector not in current_mode:
        raise ValidationError(f"unknown monitor {connector}")
    out = []
    for x, y, scale, transform, primary, mons, _lp in logical:
        entries = []
        for spec in mons:
            conn = spec[0]
            mid = mode_id if conn == connector else current_mode.get(conn)
            if mid is None:
                raise ValidationError(f"monitor {conn} has no current mode")
            entries.append((conn, mid, {}))
        out.append((int(x), int(y), float(scale), int(transform), bool(primary), entries))
    return out
