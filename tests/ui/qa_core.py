# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Display-free logic of the UI QA lint (importable without GTK; unit-tested).

Everything here works on numbers, strings and plain colour tuples: WCAG contrast,
GLib log parsing and its allowlist, geometry helpers, finding de-duplication and the
Markdown report. The widget walking that feeds it lives in qa_audit.py and the
headless harness in qa_lint.py.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

# -- configuration shared by the harness and the tests --------------------------------------
PAGES = ["dashboard", "processes", "services", "cpu", "memory", "dgpu", "igpu", "fanmon",
         "storage", "network", "performance", "fans", "gpu", "display", "gestures",
         "battery", "keyboard", "automation", "agent", "updates", "about"]

SIZES = {  # name -> (width, height, maximised)
    "1920x1080": (1920, 1080, True),
    "1280x720": (1280, 720, False),
    "900x700": (900, 700, False),
    "640x700": (640, 700, False),
    "360x640": (360, 640, False),
}
VARIANTS = ("dark", "light", "usertheme")  # usertheme = dark + the owner's ~/.config/gtk-4.0

MIN_TARGET = 24  # px, WCAG 2.2 AA minimum target size: below this is a failure
TOUCH_TARGET = 32  # px: below this is a warning (touch)
BODY_CONTRAST = 4.5
LARGE_CONTRAST = 3.0
SEVERITIES = ("fail", "warn", "info")


# -- findings -------------------------------------------------------------------------------
@dataclass(frozen=True)
class Finding:
    kind: str  # e.g. "ellipsized", "contrast", "gtk-warning"
    severity: str  # fail | warn | info
    scope: str  # page key / "shell" / "dialog:gpu-confirm" / ...
    where: str  # widget description (stable between runs)
    msg: str

    @property
    def key(self) -> tuple:
        return (self.kind, self.scope, self.where)


@dataclass
class Collected:
    """Findings keyed for de-duplication, remembering which combos produced each."""
    items: dict = field(default_factory=dict)  # key -> (Finding, set of combos)

    def add(self, f: Finding, combo: str) -> None:
        cur = self.items.get(f.key)
        if cur is None:
            self.items[f.key] = (f, {combo})
        else:
            cur[1].add(combo)
            if SEVERITIES.index(f.severity) < SEVERITIES.index(cur[0].severity):
                self.items[f.key] = (f, cur[1])

    def by_severity(self, sev: str) -> list:
        return sorted(((f, sorted(c)) for f, c in self.items.values() if f.severity == sev),
                      key=lambda x: (x[0].kind, x[0].scope, x[0].where))

    def count(self, sev: str) -> int:
        return sum(1 for f, _c in self.items.values() if f.severity == sev)


# -- colour ---------------------------------------------------------------------------------
def _lin(c: float) -> float:
    c = c / 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def rel_luminance(rgb) -> float:
    r, g, b = (_lin(v) for v in rgb[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a, b) -> float:
    la, lb = rel_luminance(a), rel_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def required_ratio(size_px: float, bold: bool) -> float:
    """WCAG 1.4.3: large text (>= 18pt, or >= 14pt bold; 1pt = 4/3 px) needs 3:1."""
    pt = size_px * 0.75
    return LARGE_CONTRAST if pt >= 18 or (bold and pt >= 14) else BODY_CONTRAST


def text_colours(colours: list) -> tuple | None:
    """From PIL-style getcolors() [(count, rgb), ...] of a label's rectangle: the
    background (most frequent colour) and the text colour (the one farthest from it in
    luminance, ignoring anti-aliasing fringes that are less than 15 % of the way).
    None when nothing was drawn (opacity 0, or the text is the background colour)."""
    if not colours:
        return None
    bg = max(colours, key=lambda c: c[0])[1][:3]
    lbg = rel_luminance(bg)
    best, far = None, 0.0
    for _n, rgb in colours:
        d = abs(rel_luminance(rgb[:3]) - lbg)
        if d > far:
            best, far = rgb[:3], d
    if best is None or far < 0.01:
        return None
    return bg, best


# -- geometry -------------------------------------------------------------------------------
Rect = tuple  # (x, y, w, h)


def overlap_area(a: Rect, b: Rect) -> float:
    w = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
    h = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0.0


def overflow_x(child: Rect, parent: Rect, tol: float = 1.5) -> float:
    """How many px the child sticks out of the parent horizontally (0 when inside)."""
    left = parent[0] - child[0]
    right = (child[0] + child[2]) - (parent[0] + parent[2])
    return max(0.0, left if left > tol else 0.0, right if right > tol else 0.0)


def overflow_y(child: Rect, parent: Rect, tol: float = 1.5) -> float:
    top = parent[1] - child[1]
    bottom = (child[1] + child[3]) - (parent[1] + parent[3])
    return max(0.0, top if top > tol else 0.0, bottom if bottom > tol else 0.0)


def classify_target(w: float, h: float) -> str | None:
    """'fail' under 24 px, 'warn' under 32 px, else None (sizes in px)."""
    if min(w, h) < MIN_TARGET:
        return "fail"
    if min(w, h) < TOUCH_TARGET:
        return "warn"
    return None


def centred(inner: Rect, outer: Rect, tol: float = 24.0) -> bool:
    cx_i, cy_i = inner[0] + inner[2] / 2, inner[1] + inner[3] / 2
    cx_o, cy_o = outer[0] + outer[2] / 2, outer[1] + outer[3] / 2
    return abs(cx_i - cx_o) <= tol and abs(cy_i - cy_o) <= max(tol, outer[3] * 0.15)


def stale_colours(fresh: list, switched: list, min_share: float = 0.0015,
                  tol: int = 30) -> list:
    """Colours that cover a noticeable share of the switched page but are nowhere in the
    fresh one. Both are PIL getcolors() lists [(count, (r, g, b)), ...] (quantised).
    Live data moves graphs around between two runs; a stale surface (a dark card left in
    a light page) shows up as a whole colour that a fresh start never paints."""
    total = sum(n for n, _c in switched) or 1
    out = []
    for n, rgb in switched:
        if n / total < min_share:
            continue
        if not any(sum(abs(a - b) for a, b in zip(rgb[:3], f[:3])) <= tol for _m, f in fresh):
            out.append((n / total, tuple(rgb[:3])))
    return sorted(out, reverse=True)


def mean_abs_diff(a: bytes, b: bytes) -> float:
    """Mean absolute per-byte difference of two equal-length pixel buffers (0..255)."""
    if len(a) != len(b) or not a:
        return 255.0
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


# -- GLib / Gtk log parsing -----------------------------------------------------------------
LOG_RE = re.compile(
    r"^\((?P<proc>[^)]*)\): (?P<domain>[\w.\-]+?)-(?P<level>WARNING|CRITICAL|ERROR|DEBUG|"
    r"INFO|MESSAGE)(?: \*\*)?: (?:\d\d:\d\d:\d\d\.\d+: )?(?P<msg>.*)$")
BAD_LEVELS = {"WARNING", "CRITICAL", "ERROR"}

# Harness noise that is not the app's fault: no portals / settings daemon / compositor
# niceties in a private headless session.
ALLOWLIST = [re.compile(p, re.I) for p in (
    r"portal", r"dconf", r"gsettings", r"GSettings", r"settings daemon",
    r"Unable to acquire session bus", r"libEGL", r"MESA", r"No GSettings schemas",
    r"cannot register existing type", r"AT-SPI", r"atspi", r"a11y bus",
    r"Failed to open dri", r"Could not (?:open|load) (?:libva|dri)",
    r"Failed to initialize libdecor", r"cursor theme", r"gnome-shell.*not running",
    r"org\.freedesktop\.(?:impl\.portal|portal)", r"No such file or directory.*(?:\.so|dri)",
    r"Theme parsing error: gtk\.css.*(?:gtk-contained|@define-color)",
    # headless Mutter: the app's own window is ours, the shell's extras are not running
    r"org\.gnome\.(?:Shell|SessionManager|Mutter\.DisplayConfig)",
    r"Failed to (?:register|connect to) (?:the )?(?:session|accessibility)",
    r"prefer-dark-theme",  # the owner's settings.ini, not the app
    r"/tests/ui/qa_", r"/tests/ui/screenshots\.py",  # the harness's own deprecated calls
)]


@dataclass(frozen=True)
class LogLine:
    domain: str
    level: str
    msg: str

    @property
    def text(self) -> str:
        return f"{self.domain}-{self.level}: {self.msg}"


def parse_log(text: str) -> list:
    """Every GLib-formatted line of a stderr capture. Plain tracebacks and Python
    warnings are reported as domain 'Python'."""
    out = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = LOG_RE.match(line.strip())
        if m:
            out.append(LogLine(m["domain"], m["level"], m["msg"].strip()))
        elif line.startswith("Traceback (most recent call last)"):
            tail = next((x for x in lines[i + 1:] if x and not x.startswith((" ", "\t"))), "")
            out.append(LogLine("Python", "ERROR", tail.strip() or "traceback"))
        elif re.search(r"\b(?:DeprecationWarning|RuntimeWarning|PyGIWarning|UserWarning)\b",
                       line):
            out.append(LogLine("Python", "WARNING", line.strip()))
    return out


def is_allowed(line: LogLine) -> bool:
    return any(p.search(line.msg) or p.search(line.text) for p in ALLOWLIST)


def log_failures(text: str) -> list:
    """Warnings / criticals / errors that are not harness noise (de-duplicated)."""
    seen, out = set(), []
    for ln in parse_log(text):
        if ln.level in BAD_LEVELS and not is_allowed(ln) and ln.text not in seen:
            seen.add(ln.text)
            out.append(ln)
    return out


# -- report ---------------------------------------------------------------------------------
def render_report(col: Collected, meta: dict | None = None) -> str:
    meta = meta or {}
    lines = ["# UI QA report", ""]
    for k, v in meta.items():
        lines.append(f"- {k}: {v}")
    lines.append("")
    lines.append(f"**{col.count('fail')} failures, {col.count('warn')} warnings, "
                 f"{col.count('info')} notes** (grouped; each lists the combinations "
                 "`variant@size[+mode]` where it occurred)")
    lines.append("")
    for sev, title in (("fail", "Failures"), ("warn", "Warnings"), ("info", "Notes")):
        rows = col.by_severity(sev)
        if not rows:
            continue
        lines.append(f"## {title} ({len(rows)})")
        lines.append("")
        by_kind = defaultdict(list)
        for f, combos in rows:
            by_kind[f.kind].append((f, combos))
        for kind, items in sorted(by_kind.items()):
            lines.append(f"### {kind} ({len(items)})")
            for f, combos in items:
                shown = ", ".join(combos[:6]) + (f" (+{len(combos) - 6})" if len(combos) > 6
                                                  else "")
                lines.append(f"- `{f.scope}` {f.where}: {f.msg}  [{shown}]")
            lines.append("")
    if not col.items:
        lines.append("No findings.")
    return "\n".join(lines) + "\n"


def summarise(col: Collected) -> str:
    kinds = Counter(f.kind for f, _c in col.items.values() if f.severity == "fail")
    if not kinds:
        return "no failures"
    return ", ".join(f"{k}: {n}" for k, n in kinds.most_common())
