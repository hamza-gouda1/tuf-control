#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Headless UI QA lint: every page x size x theme, plus dialogs, toasts and stress modes.

    python3 tests/ui/qa_lint.py                      # full matrix (about 15 minutes)
    python3 tests/ui/qa_lint.py --quick              # 1280x720 + 360x640, dark + light
    python3 tests/ui/qa_lint.py --sizes 640x700 --variants dark --pages dashboard,fans
    python3 tests/ui/qa_lint.py --list               # show the combinations and exit

What runs. A private `dbus-run-session` hosts `mutter --headless`; for each size one
compositor with a virtual monitor of that size, and for each variant one app process
against a fake hardware tree (TUF_CONTROL_FAKE_ROOT, nothing real is read or written;
automation, agent and daemon state live in a temp dir). Sizes: 1920x1080 maximised,
1280x720, 900x700, 640x700, 360x640. Variants: dark, light, and `usertheme` = dark with
the owner's real ~/.config/gtk-4.0/gtk.css and settings.ini loaded (read-only).
Extra modes (`--modes`, default on at 1280x720 and 640x700, dark): `rtl` (right-to-left
layout), `scale` (text scaling 1.5), `long` (80-character process names, very long GPU,
disk, mount and rule names), `empty` (no sensors, no battery, no dGPU: empty states) and
`ppt` (firmware that exposes power limits).

Memory safety. Everything is re-executed under
`systemd-run --user --scope -p MemoryMax=1500M -p MemorySwapMax=0`, one GTK process at a
time, each child with a hard timeout. Use --no-scope only if you know what you are doing.

For every page it checks (qa_audit.py): GTK/Adwaita/GLib warnings on stderr
(G_MESSAGES_DEBUG=all; harness noise is allow-listed in qa_core.ALLOWLIST), labels that
are ellipsized without a tooltip or clipped, widgets outside the window or sticking out of
their parent or scrolled viewport, unexpected horizontal scrollbars, overlapping siblings,
zero-size or too small (<24 px fail, <32 px warn) interactive widgets, icon-only buttons
without tooltip or accessible label, text contrast below WCAG 4.5:1 (3:1 for large text)
measured on the rendered pixels at several scroll positions, minimum-width violations,
Tab order (traps, unreachable widgets) and the visible focus ring. Dialogs, toasts,
banners and popovers get the same treatment plus centring/size checks. The live
dark -> light switch is compared pixel-wise with a fresh light run (stale colours).

Output: a Markdown report (default tests/ui/qa_report.md; findings grouped by kind with
the combinations where they occur) and exit status 1 when anything failed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import site
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
SRC = REPO / "src"
sys.path.insert(0, str(HERE))

from qa_core import (PAGES, SIZES, VARIANTS, Collected, Finding, log_failures,  # noqa: E402
                     render_report, stale_colours, summarise)

MEM_MAX = "1500M"
CHILD_TIMEOUT = 420
MODES = ("rtl", "scale", "long", "empty", "ppt", "gpu-lost")
MODE_SIZES = ("1280x720", "640x700")
THEME_PAIR_PAGES = ("dashboard", "performance", "fans", "battery", "processes")  # not the keyboard: live colour


def snapshot_widget(widget):
    """Capture a matching window child, including its dialog host and overlays.

    GtkWindow's paintable can briefly expose only its cached background after a
    surface invalidation. The direct child has the complete render node; unlike
    AdwWindow.get_content(), it also contains presented Adw.Dialog widgets. Keep
    native coordinates by selecting it only when both dimensions match.
    """
    getter = getattr(widget, "get_child", None)
    child = getter() if callable(getter) else None
    if child is not None and (child.get_width(), child.get_height()) == (
            widget.get_width(), widget.get_height()):
        return child
    return widget


def combo_name(variant: str, size: str, mode: str = "") -> str:
    return f"{variant}@{size}" + (f"+{mode}" if mode else "")


def jobs(args) -> list:
    out = []
    sizes = args.sizes.split(",")
    variants = [v for v in args.variants.split(",") if v]  # "" = only the extras
    for size in sizes:
        for variant in variants:
            out.append((variant, size, ""))
    if getattr(args, "real", False):
        out.append(("dark", "1280x720", "real"))
    if args.modes:
        for mode in args.modes.split(","):
            for size in [s for s in MODE_SIZES if s in sizes]:
                out.append(("dark", size, mode))
    return out


# ============================================================================================
# outer: scope, bus, compositor, one child per combination
# ============================================================================================
def reexec(args) -> int:
    """Under systemd-run's memory cap, then under a private session bus."""
    if not os.environ.get("TUF_QA_SCOPE") and not args.no_scope:
        if not shutil.which("systemd-run"):
            print("systemd-run not found: refusing to run GTK without a memory cap "
                  "(use --no-scope to override)", file=sys.stderr)
            return 2
        env = {**os.environ, "TUF_QA_SCOPE": "1"}
        return subprocess.call(
            ["systemd-run", "--user", "--scope", "-q", "-p", f"MemoryMax={MEM_MAX}",
             "-p", "MemorySwapMax=0", "--", sys.executable, __file__, *sys.argv[1:]], env=env)
    if not os.environ.get("TUF_QA_BUS"):
        env = {**os.environ, "TUF_QA_BUS": "1"}
        return subprocess.call(["dbus-run-session", "--", sys.executable, __file__,
                                *sys.argv[1:]], env=env)
    return outer(args)


def start_mutter(size: str):
    w, h, maximised = SIZES[size]
    mw, mh = (w, h) if maximised else (w + 240, h + 240)
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", tempfile.gettempdir()))
    sock = f"tuf-qa-{os.getpid()}-{size}"
    mutter = subprocess.Popen(
        ["mutter", "--headless", "--no-x11", f"--wayland-display={sock}",
         "--virtual-monitor", f"{mw}x{mh}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={**os.environ, "GTK_A11Y": "none", "GDK_DEBUG": "no-portals"})
    for _ in range(100):
        if (runtime / sock).exists():
            return mutter, sock
        time.sleep(0.1)
    mutter.kill()
    raise RuntimeError("mutter did not start")


def run_child(job: dict, sock: str, workdir: Path) -> tuple:
    env = {**os.environ, "GDK_BACKEND": "wayland", "WAYLAND_DISPLAY": sock,
           "TUF_QA_JOB": json.dumps(job),
           "PYTHONPATH": f"{SRC}:{HERE}:{site.getusersitepackages()}:"
                         + os.environ.get("PYTHONPATH", ""),
           "TUF_CONTROL_COLOR_SCHEME": "light" if job["variant"] == "light" else "dark",
           "GSK_RENDERER": "cairo", "ADW_DISABLE_PORTAL": "1", "GDK_DEBUG": "no-portals",
           "GTK_A11Y": "none", "G_MESSAGES_DEBUG": "all", "GSETTINGS_BACKEND": "keyfile",
           "NO_AT_BRIDGE": "1", "PYTHONWARNINGS": "default", "PYTHONUNBUFFERED": "1"}
    env.pop("DISPLAY", None)
    import screenshots as sh
    isolated_home = Path(tempfile.mkdtemp(prefix="tuf-qa-home-", dir=workdir))
    env.update(sh.isolated_env(isolated_home, job["variant"] == "usertheme"))
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", job["name"])   # job names are ours; be sure
    log = workdir / f"{safe_name}.log"
    cmd = ["timeout", "-k", "5", str(CHILD_TIMEOUT), sys.executable, __file__, "--child"]
    with open(log, "w") as fh:
        rc = subprocess.call(cmd, env=env, stdout=fh, stderr=subprocess.STDOUT)
    return rc, log


def outer(args) -> int:
    started = time.time()
    col = Collected()
    work = Path(tempfile.mkdtemp(prefix="tuf-qa-"))
    shots = Path(args.shots) if args.shots else work / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    plan = jobs(args)
    results = []
    by_size: dict = {}
    for variant, size, mode in plan:
        by_size.setdefault(size, []).append((variant, mode))
    for size, items in by_size.items():
        try:
            mutter, sock = start_mutter(size)
        except RuntimeError as exc:
            col.add(Finding("harness", "fail", "outer", size, str(exc)), size)
            continue
        try:
            for variant, mode in items:
                name = combo_name(variant, size, mode)
                job = {"variant": variant, "size": size, "mode": mode, "name": name,
                       "pages": args.pages, "quick": args.quick, "shots": str(shots),
                       "result": str(work / f"{name}.json"), "deep": args.deep,
                       "keep_shots": args.keep_shots}
                t0 = time.time()
                rc, log = run_child(job, sock, work)
                dt = time.time() - t0
                text = log.read_text(errors="replace")
                if os.environ.get("TUF_QA_DEBUG_FOCUS"):
                    print("\n".join(x for x in text.splitlines() if x.startswith(("FOCUS","STUCK"))))
                res = {}
                rp = Path(job["result"])
                if rp.exists():
                    res = json.loads(rp.read_text())
                for fd in res.get("findings", []):
                    col.add(Finding(**fd), name)
                for ln in log_failures(text):
                    col.add(Finding("gtk-" + ln.level.lower(), "fail", "log",
                                    f"{ln.domain}: {ln.msg[:160]}",
                                    "GLib log line in the app's stderr"), name)
                if rc != 0 and not res:
                    col.add(Finding("harness", "fail", "outer", name,
                                    f"child exited {rc} without a result; see {log}"), name)
                    print(text[-1500:], file=sys.stderr)
                results.append((name, res, dt))
                print(f"[{name}] {dt:4.0f}s  rc={rc}  findings="
                      f"{len(res.get('findings', []))}", flush=True)
        finally:
            mutter.terminate()
            try:
                mutter.wait(5)
            except subprocess.TimeoutExpired:
                mutter.kill()
    compare_theme_pairs(col, shots, plan)
    meta = {"date": time.strftime("%Y-%m-%d %H:%M"), "combinations": len(plan),
            "duration": f"{time.time() - started:.0f}s",
            "sizes": args.sizes, "variants": args.variants}
    notes = [f"{n}: {json.dumps(r.get('info', {}))}" for n, r, _t in results if r.get("info")]
    report = render_report(col, meta)
    if notes:
        report += "\n## Measurements\n" + "\n".join(f"- {n}" for n in notes) + "\n"
    Path(args.report).write_text(report)
    if args.keep_shots:
        print(f"screenshots kept in {shots}")
    else:
        shutil.rmtree(work, ignore_errors=True)
    print(f"report: {args.report}")
    print(f"{col.count('fail')} failures, {col.count('warn')} warnings, "
          f"{col.count('info')} notes; {summarise(col)}")
    return 1 if col.count("fail") else 0


def compare_theme_pairs(col: Collected, shots: Path, plan) -> None:
    """Dark app switched to light vs an app that started light: same pixels, or stale."""
    try:
        from PIL import Image
    except ImportError:
        return
    for _v, size, mode in plan:
        if mode:
            continue
        for page in THEME_PAIR_PAGES:
            a = shots / f"fresh-light-{size}-{page}.png"
            b = shots / f"switched-{size}-{page}.png"
            if not (a.exists() and b.exists()):
                continue
            ia, ib = Image.open(a).convert("RGB"), Image.open(b).convert("RGB")
            if ia.size != ib.size:
                col.add(Finding("theme-switch", "warn", page, "page",
                                f"sizes differ {ia.size} vs {ib.size}"), f"{size}")
                continue
            qa_, qb_ = (im.point(lambda v: v & 0xF8).getcolors(maxcolors=1 << 20) or []
                        for im in (ia, ib))
            stale = stale_colours(qa_, qb_)
            if stale:
                share, rgb = stale[0]
                col.add(Finding("theme-switch", "fail", page, "page",
                                f"after the live dark->light switch {len(stale)} colours that "
                                f"a fresh light start never paints cover the page (largest "
                                f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}, {share * 100:.1f}%): "
                                "stale colours"), size)


# ============================================================================================
# inner: the app, driven page by page, audited
# ============================================================================================
def child(job: dict) -> int:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    gi.require_version("Gsk", "4.0")
    gi.require_version("Graphene", "1.0")
    from gi.repository import Adw, Gdk, GLib, Gsk, Gtk, Graphene

    import screenshots as sh
    from PIL import Image, ImageChops
    import qa_audit as qa
    from tuf_control.backends.fake import build_fake_tree
    from tuf_control.controller import FAKE_ROOT_ENV

    variant, size, mode = job["variant"], job["size"], job["mode"]
    name = job["name"]
    w, h, maximised = SIZES[size]
    findings: list = []
    info: dict = {}
    seen = set()

    tag = {"v": ""}

    def emit(f: Finding) -> None:
        if tag["v"]:
            f = Finding(f.kind, f.severity, f.scope, f.where + tag["v"], f.msg)
        if f.key not in seen:
            seen.add(f.key)
            findings.append(dict(f.__dict__))

    tmp = Path(tempfile.mkdtemp(prefix="tuf-qa-run-"))
    real = mode == "real"  # the real machine, read-only: nothing is written or clicked
    root = build_fake_tree(tmp / "root", ppt_readable=(mode == "ppt"))
    if real:
        os.environ.pop(FAKE_ROOT_ENV, None)
    else:
        os.environ[FAKE_ROOT_ENV] = str(root)
    from tuf_control.automation.config import CONFIG_DIR_ENV, RUNTIME_DIR_ENV
    os.environ[CONFIG_DIR_ENV] = str(tmp / "config")
    os.environ[RUNTIME_DIR_ENV] = str(tmp / "run")
    from tuf_control.agent.store import STATE_DIR_ENV
    os.environ[STATE_DIR_ENV] = str(tmp / "state")
    os.environ["TUF_CONTROL_AGENT_NOTIFY"] = "0"
    long_mode = mode == "long"
    if long_mode:
        patch_long_text(root)
    if mode == "empty":
        from tuf_control.telemetry import fake
        orig_ensure = fake.ensure

        def ensure_stripped(r):  # a laptop that exposes none of the ASUS or sensor files
            orig_ensure(r)
            strip_hardware(Path(r))
        fake.ensure = ensure_stripped
        fake.FakeWorld.advance = lambda self, now: None
        strip_hardware(root)
    if mode == "gpu-lost":
        from tuf_control.telemetry.gpu_nvidia import NvidiaGpu
        original_sample = NvidiaGpu.sample

        def lost_sample(gpu, now):
            gpu.backend = None  # also exercise failed NVML initialization.
            (root / "sys/bus/pci/devices/0000:01:00.0/power/runtime_status").write_text(
                "error\n")
            return original_sample(gpu, now)

        NvidiaGpu.sample = lost_sample
    if not real:
        sh.seed_agent_demo()
    if long_mode:
        from tuf_control.agent.tools import AgentService
        svc = AgentService()
        svc.set_client("an-agent-client-with-a-really-long-name-that-just-keeps-going-on", "1")
        svc.call("get_status", {})
    if not real:
        seed_audit_rows(50)
    if long_mode:
        sh.DEMO_RULES[0]["name"] = ("Games get Performance, a keyboard effect and overdrive "
                                    "and a long rule name")  # 80 characters, the limit
    daemon = None if real else sh.start_demo_daemon()

    if mode == "rtl":
        Gtk.init()
        Gtk.Widget.set_default_direction(Gtk.TextDirection.RTL)
    from tuf_control.ui.app import TufControlApp
    app = TufControlApp()
    if mode == "scale":
        def _scale(_a):
            Gtk.Settings.get_default().set_property("gtk-xft-dpi", int(96 * 1.5 * 1024))
        app.connect("startup", _scale)
    qa.install_a11y_spy()
    shots = Path(job["shots"])
    state = {"failed": False}

    # -- rendering ------------------------------------------------------------------------
    def render_png(widget, path: Path) -> "Image.Image":
        ww, hh = widget.get_width(), widget.get_height()
        snap = Gtk.Snapshot()
        bg = widget.get_style_context().lookup_color("window_bg_color")[1]
        snap.append_color(bg, Graphene.Rect().init(0, 0, ww, hh))
        Gtk.WidgetPaintable(widget=snapshot_widget(widget)).snapshot(snap, ww, hh)
        renderer = Gsk.CairoRenderer.new()
        renderer.realize_for_display(widget.get_display())
        try:
            tex = renderer.render_texture(snap.to_node(), Graphene.Rect().init(0, 0, ww, hh))
            tex.save_to_tiff(str(path))
        finally:
            renderer.unrealize()
        img = Image.open(path)
        img.load()
        return img

    scratch = tmp / "frame.tiff"

    def popover_surface(scope: str, pop) -> None:
        """A popover must be one rounded surface: its own node paints nothing, so the
        corners of its widget stay clear (a user gtk.css rule such as `window,
        .background { background-color }` once filled the whole popup as a dark box)."""
        ww, hh = pop.get_width(), pop.get_height()
        if ww < 8 or hh < 8:
            return
        snap = Gtk.Snapshot()
        magenta = Gdk.RGBA()
        magenta.parse("#ff00ff")
        snap.append_color(magenta, Graphene.Rect().init(0, 0, ww, hh))
        Gtk.WidgetPaintable(widget=pop).snapshot(snap, ww, hh)
        renderer = Gsk.CairoRenderer.new()
        renderer.realize_for_display(pop.get_display())
        try:
            tex = renderer.render_texture(snap.to_node(), Graphene.Rect().init(0, 0, ww, hh))
            tex.save_to_tiff(str(scratch))
        finally:
            renderer.unrealize()
        img = Image.open(scratch).convert("RGB")
        for x, y in ((1, 1), (ww - 2, 1), (1, hh - 2), (ww - 2, hh - 2)):
            r, g, b = img.getpixel((x, y))
            if abs(r - 255) + abs(g) + abs(b - 255) > 90:
                emit(Finding("popover-surface", "fail", scope, "popover",
                             f"the popup surface is filled (corner {x},{y} is "
                             f"#{r:02x}{g:02x}{b:02x}, not clear): a dark box behind the "
                             "rounded list"))
                return

    def frame(widget=None) -> "Image.Image":
        return render_png(widget or app.window, scratch)

    def save_shot(img, fname: str) -> None:
        img.convert("RGB").save(shots / fname)

    auditor_holder = {}

    def steps(win):
        aud = qa.Auditor(win, emit)
        auditor_holder["a"] = aud
        hub = win.hub
        clock = {"t": time.monotonic() - 125.0}
        if real:  # live timer, real readings; give the graphs a few seconds of history
            def tick(n=1):
                return None
            yield 6000
        else:
            hub.hold = True
            hub.stop()
            hub.store.clear()
            hub.procs_wanted = True
            for i in range(120):
                if mode != "empty":
                    sh.simulate(root, i)
                clock["t"] += 1.0
                hub.collect_now(clock["t"])
            hub.procs_wanted = False

            def tick(n=1):
                for _ in range(n):
                    clock["t"] += 1.0
                    hub.collect_now(clock["t"], notify=True)
            tick()
            yield 700
        pages = [p for p in (job["pages"].split(",") if job["pages"] else PAGES) if p in win.pages]
        if job.get("quick") and not job["pages"]:
            pages = pages
        if mode == "ppt":
            pages = [p for p in ("performance", "dashboard") if p in pages]

        # -- theme pairs: a pass in the app's own scheme, then one in light ------------------
        do_pairs = not mode and size == "1280x720" or (not mode and job.get("pairs_all"))
        if do_pairs:
            for scheme_pass in (1, 2):
                if scheme_pass == 2 and variant != "light":
                    if variant == "usertheme":
                        break
                    Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
                    yield 700
                for page in THEME_PAIR_PAGES:
                    win.show_page(page)
                    if page == "keyboard":
                        win.pages["keyboard"].preview.fixed_t = 1.3
                    tick(3 if page == "processes" else 1)
                    yield 600
                    if scheme_pass == 2:
                        img = frame(win.get_content())
                        fname = (f"fresh-light-{size}-{page}.png" if variant == "light"
                                 else f"switched-{size}-{page}.png")
                        save_shot(img, fname)
                        if variant == "dark":  # stale colours also show as bad contrast
                            tag["v"] = " (after live switch)"
                            aud.audit(page, win.pages[page], png=frame(), structural=False)
                            tag["v"] = ""
            if variant == "dark":
                Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
                yield 700

        # -- every page ------------------------------------------------------------------------
        win.show_page("dashboard")
        yield 400
        if maximised:
            win.maximize()
            yield 600
        else:
            win.set_default_size(w, h)
            yield 500
        for key in pages:
            try:
                win.show_page(key)
                if key == "keyboard":
                    win.pages["keyboard"].preview.fixed_t = 1.3
                tick(3 if key == "processes" else 1)
                t0 = time.monotonic()
                yield 800
                page = win.pages[key]
                if mode == "gpu-lost" and key in ("dashboard", "dgpu", "gpu"):
                    text = (page.dgpu.foot.get_text() if key == "dashboard" else
                            page.sleep_note.get_text() if key == "dgpu" else
                            page.power_row.get_subtitle())
                    if "restart" not in text.lower() or "lost" not in text.lower():
                        emit(Finding("gpu-lost", "fail", key, "recovery message",
                                     "lost GPU recovery guidance missing without NVML"))
                if key == "processes":
                    pp = win.pages["processes"]
                    row = pp.tree.get_row(0)
                    if row is not None:
                        row.set_expanded(True)
                        pp.sel.set_selected(1)
                    yield 300
                if key == "fans":
                    c = win.pages["fans"].canvas
                    c.active = 4
                    win.pages["fans"]._changed()
                    c.queue_draw()
                    yield 200
                qa.check_min_width(emit, win, key, w, h, page)
                img = frame()
                if not real and (job.get("keep_shots") or (
                        mode == "" and size in ("1280x720", "360x640"))):
                    save_shot(img, f"page-{name}-{key}.png")
                n = aud.audit(key, page, png=img)
                info.setdefault("widgets", {})[key] = n
                aud.audit_blank(key, page, img,
                                texts={} if real else info.setdefault("texts", {}))
                yield from scroll_contrast(win, aud, key, page, frame)
                expected = qa.interactive_widgets(page)
                qa.focus_order(emit, win, key, page, expected)
                if job["deep"] or (size == "1280x720" and mode == ""):
                    yield from focus_ring(win, aud, key, page, frame, expected)
                if key == "agent":
                    info["agent_page_ms"] = int((time.monotonic() - t0) * 1000)
                if key == "updates" and not real and mode != "empty":
                    yield from updates_states(win, aud, frame)
            except Exception:
                emit(Finding("harness-exception", "fail", key, "page",
                             traceback.format_exc()[-600:]))
        # -- shell: sidebar + header -----------------------------------------------------------
        try:
            win.show_page("dashboard")
            yield 500
            qa.check_min_width(emit, win, "shell", w, h)
            aud.audit("shell", win, skip={win.stack}, png=frame())
            qa.check_header(emit, win)
            yield from shell_states(win, aud, frame, tick)
            yield from keyboard_popovers(win)
        except Exception:
            emit(Finding("harness-exception", "fail", "shell", "shell",
                         traceback.format_exc()[-600:]))
        # -- dialogs last (the window renders blank after an Adw.Dialog closes here) -------------
        if not mode or mode in ("long", "rtl", "scale"):
            try:
                yield from dialogs(win, aud, frame, tick)
            except Exception:
                emit(Finding("harness-exception", "fail", "dialogs", "dialogs",
                             traceback.format_exc()[-800:]))
        app.quit()

    # -- extra step generators ------------------------------------------------------------------
    def scroll_contrast(win, aud, key, page, frame):
        sw = biggest_scrolled(page)
        if sw is None:
            return
        adj = sw.get_vadjustment()
        span = adj.get_upper() - adj.get_page_size()
        if span < 20:
            return
        step = max(40.0, adj.get_page_size() * 0.85)
        pos = 0.0
        for _ in range(8):
            pos = min(span, pos + step)
            adj.set_value(pos)
            yield 220
            aud.audit(key, page, png=frame(), structural=False)
            if pos >= span:
                break
        adj.set_value(0)
        yield 150

    def focus_ring(win, aud, key, page, frame, expected):
        todo = [x for x in expected if x.get_mapped() and x.get_focusable()
                and not isinstance(x, Gtk.Editable)
                and not qa.has_ancestor(x, Adw.SwitchRow, Adw.ExpanderRow)][:2]
        for wdg in todo:
            r = qa.rect_in(wdg, win)
            if r is None or r[1] < 60 or r[1] + r[3] > win.get_height() - 4 or r[2] < 8:
                continue
            win.set_focus(None)
            win.set_focus_visible(True)
            yield 120
            a = frame()
            wdg.grab_focus()
            win.set_focus_visible(True)
            yield 180
            if win.get_focus() is not wdg:
                # the headless compositor never gives the window real keyboard focus and
                # a GtkListBox row then keeps the grab (a documented harness limit): the
                # widget never took focus here, so there is nothing to compare
                emit(Finding("focus-ring", "info", key, qa.desc(wdg),
                             "the grab did not land on this widget in the headless "
                             "compositor; ring not verifiable here"))
                continue
            b = frame()
            box = (max(0, int(r[0]) - 6), max(0, int(r[1]) - 6),
                   min(a.size[0], int(r[0] + r[2]) + 6), min(a.size[1], int(r[1] + r[3]) + 6))
            diff = ImageChops.difference(a.crop(box).convert("RGB"),
                                         b.crop(box).convert("RGB")).getbbox()
            if diff is None:
                emit(Finding("focus-ring", "fail", key, qa.desc(wdg),
                             "no visible change when this widget takes keyboard focus"))
        win.set_focus(None)
        yield 100

    def updates_states(win, aud, frame):
        """Exercise real UI state transitions against the fake transaction backend."""
        from tuf_control.updates.fake import FAKE_PENDING_ENV
        up = win.pages["updates"]
        for _ in range(40):
            if up.report is not None and not up.busy:
                break
            yield 100
        if up.report is None or up.busy:
            emit(Finding("updates-state", "fail", "updates", "report",
                         "fake update check did not finish"))
            return
        os.environ[FAKE_PENDING_ENV] = "apt:linux-image-generic:amd64"
        item = next(i for i in up.report.items() if i.name == "openssl")
        # Include the kept-back kernel to exercise the false-success regression.
        kernel = next(i for i in up.report.items() if i.name == "linux-image-generic")
        up._apply([item, kernel])
        yield 200
        up._tick()
        sh._scroll_to(up.prefs, up.progress_g)
        yield 150
        aud.audit("updates:progress", up.progress_g, png=frame())
        qa.check_min_width(emit, win, "updates:progress", w, h, up)
        for _ in range(80):
            if not up.busy:
                break
            yield 100
        if up.busy:
            emit(Finding("updates-state", "fail", "updates", "apply",
                         "fake update apply did not finish"))
            return
        yield 300
        if not up.last_run or not any(i.get("result") == "pending"
                                      for i in up.last_run.get("items", [])):
            emit(Finding("updates-state", "fail", "updates", "verified result",
                         "unchanged kernel did not appear as still pending"))
        hist = next((x for x in up.dynamic if x.get_title() == "History"), None)
        if hist is not None:
            sh._scroll_to(up.prefs, hist)
            yield 150
            aud.audit("updates:history", hist, png=frame())
        # A service failure is not proof that there are no updates.
        from tuf_control.updates.model import Report, SourceResult
        saved = up.report
        up.report = Report(checked_ts=time.time(), sources=[SourceResult(
            "apt", error="PackageKit did not return its package list")], fake=True)
        up.render()
        up._scroll_to_top()
        yield 150
        if up.hero_title.get_text() != "Update check incomplete":
            emit(Finding("updates-state", "fail", "updates", "failed source",
                         "failed check was presented as up to date"))
        aud.audit("updates:error", up, png=frame())
        up.report = saved
        up.render()
        up._scroll_to_top()
        yield 150

    def shell_states(win, aud, frame, tick):
        # toast: bottom of the window, not over the profile chip
        win.toast("Balanced profile on")
        yield 600
        toast = find_gtype(win.toasts, "AdwToastWidget")
        if toast is not None:
            tr = qa.rect_in(toast, win)
            cr = qa.rect_in(win.chip, win)
            if tr and cr and qa.overlap_area(tr, cr) > 0:
                emit(Finding("toast-overlap", "fail", "toast", "toast",
                             "toast overlaps the profile chip"))
            if tr and (tr[0] < -1 or tr[0] + tr[2] > win.get_width() + 1):
                emit(Finding("toast-bounds", "fail", "toast", "toast",
                             f"toast {tr[2]:.0f}px wide is cut off by the {win.get_width()}px "
                             "window"))
            aud.audit("toast", toast, png=frame())
        else:
            emit(Finding("toast-missing", "warn", "toast", "toast",
                         "no AdwToastWidget found after win.toast()"))
        win.toast("Couldn't write the fan curve: permission denied by the kernel "
                  "(asus_custom_fan_curve) while setting point 5 of the CPU curve", )
        yield 600
        toast = find_gtype(win.toasts, "AdwToastWidget")
        if toast is not None:
            tr = qa.rect_in(toast, win)
            if tr and (tr[0] < -1 or tr[0] + tr[2] > win.get_width() + 1):
                emit(Finding("toast-bounds", "fail", "toast", "long toast",
                             f"long toast {tr[2]:.0f}px wide is cut off by the "
                             f"{win.get_width()}px window"))
            aud.audit("toast:long", toast, png=frame())
        yield 3200
        # profile chip popover
        if win.chip.get_mapped():
            win.chip.popup()
            yield 500
            pop = win.chip.get_popover()
            if pop is not None and pop.get_mapped():
                popover_surface("popover:profile", pop)
                aud.audit("popover:profile", pop.get_child(), png=None)
                pr = pop.get_child()
                if pr.get_width() < 100:
                    emit(Finding("popover-size", "warn", "popover:profile", "popover",
                                 f"profile menu is only {pr.get_width()}px wide"))
            win.chip.popdown()
            yield 300
        # sidebar collapsed navigation: back button / reveal when collapsed
        if win.split.get_collapsed():
            win.split.set_show_content(False)
            yield 500
            aud.audit("sidebar", win.split.get_sidebar(), png=frame())
            win.split.set_show_content(True)
            yield 400

    def keyboard_popovers(win):
        kb = win.pages["keyboard"]
        win.show_page("keyboard")
        yield 700
        for scope, row in (("popover:keyboard-mode", kb.mode_row),):
            row.activate()
            yield 600
            pop = sh._find(row, Gtk.Popover)
            if pop is None and not row.get_sensitive():
                continue  # nothing to open: the keyboard has no effects here
            if pop is None:
                emit(Finding("popover-missing", "warn", scope, "popover", "did not open"))
                continue
            popover_surface(scope, pop)
            pop.popdown()
            yield 300

    def dialogs(win, aud, frame, tick):
        # fan curve apply
        win.show_page("fans")
        yield 500
        fans = win.pages["fans"]
        fans.models["CPU"].drag(4, 64, 170)
        fans._changed()
        fans._apply()
        yield from dialog_pass(win, aud, "dialog:fans-apply")
        # factory reset (destructive)
        fans._reset()
        yield from dialog_pass(win, aud, "dialog:fans-reset")
        # GPU switch + banner
        from tuf_control.core import GpuMode
        win.show_page("gpu")
        yield 500
        gpu = win.pages["gpu"]
        gpu.checks[GpuMode.ASUS_MUX_DGPU].set_active(True)
        gpu._switch()
        yield from dialog_pass(win, aud, "dialog:gpu-switch", accept=True)
        yield 600
        aud.audit("gpu:banner", gpu, png=frame())
        # colour chooser / keyboard popovers are separate toplevels: checked by screenshots.py
        # automation editor: new and edit
        win.show_page("automation")
        yield 900
        page = win.pages["automation"]
        for scope, rid in (("dialog:rule-edit", "games-get-performance"), ("dialog:rule-new", None)):
            dlg = page.open_editor(rid)
            yield 700
            if rid:
                dlg.act_rows["effect"].set_expanded(True)
                yield 300
            qa.dialog_geometry(emit, win, scope, dlg, dlg.get_child() or dlg)
            aud.audit(scope, dlg, png=frame())
            yield from scroll_contrast(win, aud, scope, dlg, frame)
            qa.focus_order(emit, win, scope, dlg, qa.interactive_widgets(dlg))
            dlg.force_close()
            yield 400

    def dialog_pass(win, aud, scope, accept=False):
        yield 700
        dlg = win.last_dialog
        content = find_gtype(dlg, "AdwAlertDialog") or dlg
        # the visible sheet is the child that is not the full-window dimming layer
        sheet = sheet_of(dlg, win)
        qa.dialog_geometry(emit, win, scope, dlg, sheet)
        aud.audit(scope, dlg, png=frame())
        qa.focus_order(emit, win, scope, dlg, qa.interactive_widgets(dlg))
        if accept:
            dlg.emit("response", "ok")
        dlg.force_close()
        yield 500

    def on_activate(_app):
        win = app.window
        win.set_default_size(w, h)
        it = steps(win)

        def tick():
            try:
                delay = next(it)
            except StopIteration:
                return False
            except Exception:
                emit(Finding("harness-exception", "fail", "run", "steps",
                             traceback.format_exc()[-800:]))
                traceback.print_exc()
                state["failed"] = True
                app.quit()
                return False
            GLib.timeout_add(delay, tick)
            return False

        def rapl():
            (root / "sys/class/powercap/intel-rapl:0/energy_uj").write_text(
                f"{int(time.monotonic() * 28e6)}\n")
            return True
        if not real:
            rapl()
            GLib.timeout_add(250, rapl)
        GLib.timeout_add(1500, tick)

    app.connect_after("activate", on_activate)
    t0 = time.time()
    try:
        app.run([])
    finally:
        if daemon is not None:
            daemon.stop()
    info["seconds"] = int(time.time() - t0)
    Path(job["result"]).write_text(json.dumps({"findings": findings, "info": info}))
    shutil.rmtree(tmp, ignore_errors=True)
    return 1 if state["failed"] else 0


# -- helpers used by the child -------------------------------------------------------------------
def find_gtype(root, gtype_name: str):
    from qa_audit import children

    def visit(w):
        if w.__gtype__.name == gtype_name:
            return w
        for c in children(w):
            hit = visit(c)
            if hit is not None:
                return hit
        return None
    return visit(root)


def sheet_of(dlg, win):
    """The dialog's visible card: the biggest descendant smaller than the window."""
    from qa_audit import children, rect_in
    best, area = dlg, 0.0
    ww, wh = win.get_width(), win.get_height()

    def visit(w, depth):
        nonlocal best, area
        r = rect_in(w, win)
        if r is None or depth > 4:
            return
        a = r[2] * r[3]
        if a < ww * wh * 0.999 and a > area and r[2] > 80:
            best, area = w, a
        for c in children(w):
            if c.get_mapped():
                visit(c, depth + 1)
    visit(dlg, 0)
    return best


def biggest_scrolled(root):
    from gi.repository import Gtk
    from qa_audit import children
    best, area = None, 0
    stack = [root]
    while stack:
        w = stack.pop()
        if isinstance(w, Gtk.ScrolledWindow) and w.get_mapped():
            a = w.get_width() * w.get_height()
            if a > area and w.get_vadjustment().get_upper() > w.get_vadjustment().get_page_size():
                best, area = w, a
        stack.extend(children(w))
    return best


def seed_audit_rows(n: int) -> None:
    from tuf_control.agent.tools import AgentService
    svc = AgentService()
    svc.set_client("log-filler", "1")
    for i in range(n):
        svc.call("get_status" if i % 2 else "get_telemetry", {})


def patch_long_text(root: Path) -> None:
    """80-character process names, a very long GPU / disk model, mount and interface."""
    from tuf_control.telemetry import fake
    longname = "an-extremely-long-process-name-that-never-seems-to-end-and-keeps-going-on-and-on"
    fake.PROCS[:] = [
        (p[0], p[1], p[2], (f"/opt/{longname}/bin/{longname}" if p[0] in (2101, 3488, 2610)
                            else p[3]), *p[4:]) if p[0] in (2101, 3488, 2610, 2150) else p
        for p in fake.PROCS]
    orig = fake.FakeWorld.advance

    def advance(self, now):
        orig(self, now)
        f = self.root / "fake/nvml.json"
        try:
            d = json.loads(f.read_text())
            d["static"]["name"] = ("NVIDIA GeForce RTX 5050 Laptop GPU with a name that goes "
                                   "on far longer than any real product string would")
            f.write_text(json.dumps(d))
        except (OSError, ValueError, KeyError):
            pass
    fake.FakeWorld.advance = advance
    fake.ensure(root)
    (root / "sys/block/nvme0n1/device/model").write_text(
        "Western Digital PC SN5000S SDEQNSJ-512G-1002 with an overlong model string\n")
    mounts = json.loads((root / "fake/mounts.json").read_text())
    mounts[0]["mount"] = "/run/media/user/an-unreasonably-long-volume-label-for-a-usb-drive"
    mounts[0]["source"] = "/dev/disk/by-id/nvme-WD_PC_SN5000S_SDEQNSJ-512G-1002_0000_0000"
    (root / "fake/mounts.json").write_text(json.dumps(mounts))


def strip_hardware(root: Path) -> None:
    """A machine with no ASUS firmware, battery, keyboard light, fans, dGPU or network."""
    for rel in ("sys/firmware/acpi", "sys/class/firmware-attributes", "sys/class/leds",
                "sys/class/hwmon/hwmon8", "sys/class/hwmon/hwmon9", "sys/class/power_supply",
                "sys/class/net", "sys/bus/pci/devices/0000:01:00.0", "fake/nvml.json",
                "fake/nvidia-smi.csv", "fake/mounts.json", "sys/class/drm", "sys/block"):
        p = root / rel
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        elif p.exists():
            p.unlink()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--sizes", default=",".join(SIZES))
    p.add_argument("--variants", default=",".join(VARIANTS))
    p.add_argument("--modes", default=",".join(MODES),
                   help="extra stress modes at 1280x720 and 640x700 (dark): " + ",".join(MODES))
    p.add_argument("--pages", default="", help="comma list; default all")
    p.add_argument("--quick", action="store_true",
                   help="sizes 1280x720 and 360x640, dark and light, no extra modes")
    p.add_argument("--deep", action="store_true", help="focus-ring checks at every size")
    p.add_argument("--report", default=str(HERE / "qa_report.md"))
    p.add_argument("--shots", default="", help="directory for the captured PNGs")
    p.add_argument("--keep-shots", action="store_true")
    p.add_argument("--real", action="store_true",
                   help="also one read-only pass against this machine's real telemetry "
                        "(no writes, no dialogs, no screenshots, no label texts kept)")
    p.add_argument("--no-scope", action="store_true", help="skip the systemd-run memory cap")
    p.add_argument("--list", action="store_true")
    args = p.parse_args()
    if os.environ.get("TUF_QA_JOB") and args.child:
        return child(json.loads(os.environ["TUF_QA_JOB"]))
    if args.quick:
        args.sizes, args.variants, args.modes = "1280x720,360x640", "dark,light", ""
    if args.list:
        for v, s, m in jobs(args):
            print(combo_name(v, s, m))
        return 0
    return reexec(args)


if __name__ == "__main__":
    sys.exit(main())
