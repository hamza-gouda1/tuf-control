#!/usr/bin/env python3
"""Headless screenshots of every page against a fake hardware tree.

    python3 tests/ui/screenshots.py [--out docs/screenshots] [--schemes dark,light]
                                    [--size 1180x800] [--user-theme]

How: a private D-Bus session runs `mutter --headless` with a virtual monitor; the app
connects to it over Wayland, and each page is rendered off-screen through
Gtk.WidgetPaintable -> Gsk renderer -> PNG. Nothing touches real hardware
(TUF_CONTROL_FAKE_ROOT is always set) and only one GTK process runs at a time.
"""
from __future__ import annotations

import argparse
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"


# -- outer: private bus + headless compositor ---------------------------------------------
def outer(args) -> int:
    if not os.environ.get("TUF_SHOT_BUS"):
        env = {**os.environ, "TUF_SHOT_BUS": "1"}
        return subprocess.call(["dbus-run-session", "--", sys.executable, __file__, *sys.argv[1:]],
                               env=env)
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", tempfile.gettempdir()))
    sock = f"tuf-shot-{os.getpid()}"
    w, h = args.size.split("x")
    mutter = subprocess.Popen(
        ["mutter", "--headless", "--no-x11", f"--wayland-display={sock}",
         "--virtual-monitor", f"{int(w) + 200}x{int(h) + 200}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={**os.environ, "GTK_A11Y": "none", "GDK_DEBUG": "no-portals"})
    try:
        for _ in range(100):
            if (runtime / sock).exists():
                break
            time.sleep(0.1)
        else:
            print("mutter did not start", file=sys.stderr)
            return 1
        rc = 0
        for scheme in args.schemes.split(","):
            env = {**os.environ, "GDK_BACKEND": "wayland", "WAYLAND_DISPLAY": sock,
                   "TUF_SHOT_CHILD": scheme, "PYTHONPATH": str(SRC),
                   "TUF_CONTROL_COLOR_SCHEME": scheme, "GSK_RENDERER": "cairo",
                   "ADW_DISABLE_PORTAL": "1", "GDK_DEBUG": "no-portals", "GTK_A11Y": "none"}
            env.pop("DISPLAY", None)
            if not args.user_theme:  # the app's own look, without ~/.config/gtk-4.0/gtk.css
                cfg = Path(tempfile.mkdtemp(prefix="tuf-shot-cfg-"))
                env["XDG_CONFIG_HOME"] = str(cfg)
            rc |= subprocess.call([sys.executable, __file__, *sys.argv[1:]], env=env,
                                  timeout=120)
        return rc
    finally:
        mutter.terminate()
        try:
            mutter.wait(5)
        except subprocess.TimeoutExpired:
            mutter.kill()


# -- inner: the app, driven page by page --------------------------------------------------
def simulate(root: Path, i: int) -> None:
    """Move the fake sensors like a short gaming burst, so graphs have shape."""
    def w(rel, v):
        (root / rel).write_text(f"{v}\n")
    load = 0.5 + 0.5 * math.sin(i / 14.0) + 0.15 * math.sin(i / 3.1)
    w("sys/class/hwmon/hwmon7/temp1_input", int((48 + 30 * load) * 1000))
    w("sys/class/hwmon/hwmon8/fan1_input", int(2600 + 2400 * load))
    w("sys/class/hwmon/hwmon8/fan2_input", int(2300 + 2200 * load))
    w("sys/class/power_supply/BAT0/power_now", int((18 + 22 * load) * 1e6))
    w("fake/nvidia-smi.csv", f"{44 + 26 * load:.0f}, {14 + 60 * load:.2f}, {10 + 80 * load:.0f}")


def exercise(win, root: Path, out: Path, scheme: str, suffix: str, render):
    """Drive the write paths end to end (fake tree only) and check what landed."""
    from tuf_control.core import GpuMode, Profile
    problems = []
    win.request_profile(Profile.PERFORMANCE)
    yield 500
    if (root / "sys/firmware/acpi/platform_profile").read_text().strip() != "performance":
        problems.append("profile not written")
    fans = win.pages["fans"]
    win.show_page("fans")
    yield 600
    fans.models["CPU"].drag(4, 64, 170)
    fans.canvas.active = 4
    fans.canvas.queue_draw()
    fans._changed()
    fans._apply()
    yield 300
    win.last_dialog.emit("response", "ok")
    win.last_dialog.force_close()
    yield 500
    pt = (root / "sys/class/hwmon/hwmon9/pwm1_auto_point5_pwm").read_text().strip()
    if pt != "170":
        problems.append(f"fan curve not written ({pt})")
    render(win, out / f"toast-{scheme}{suffix}.png")
    gpu = win.pages["gpu"]
    win.show_page("gpu")
    yield 500
    gpu.checks[GpuMode.ASUS_MUX_DGPU].set_active(True)
    gpu._switch()
    yield 300
    win.last_dialog.emit("response", "ok")
    win.last_dialog.force_close()
    yield 700
    render(win.get_content(), out / f"gpu-pending-{scheme}{suffix}.png")
    if not gpu.banner.get_revealed():
        problems.append("reboot banner not shown")
    kb = win.pages["keyboard"]
    win.show_page("keyboard")
    yield 500
    kb.bri_buttons[1].set_active(True)
    yield 400
    if (root / "sys/class/leds/asus::kbd_backlight/brightness").read_text().strip() != "1":
        problems.append("keyboard brightness not written")
    bat = win.pages["battery"]
    win.show_page("battery")
    yield 400
    bat.adj.set_value(80)
    yield 1300
    if (root / "sys/class/power_supply/BAT0/charge_control_end_threshold").read_text().strip() \
            != "80":
        problems.append("charge limit not written")
    print("exercise:", "OK" if not problems else "; ".join(problems))
    if problems:
        raise AssertionError(problems)


DEMO_RULES = [
    {"name": "Games get Performance", "priority": 80, "debounce_s": 3,
     "trigger": {"type": "app", "names": ["steam", "gamescope", "lutris"]},
     "actions": [{"type": "profile", "value": "performance"},
                 {"type": "panel_overdrive", "value": True},
                 {"type": "effect", "name": "audio"}]},
    {"name": "Unplugged: Quiet", "debounce_s": 0,
     "trigger": {"type": "power", "source": "battery"},
     "actions": [{"type": "profile", "value": "quiet"}, {"type": "charge_limit", "value": 80},
                 {"type": "kbd_brightness", "value": 1}]},
    {"name": "Quiet nights", "priority": 40, "debounce_s": 0,
     "trigger": {"type": "schedule", "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                 "start": "23:00", "end": "07:00"},
     "actions": [{"type": "kbd_brightness", "value": 0}]},
    {"name": "Red keys in Performance", "enabled": False, "debounce_s": 0,
     "trigger": {"type": "profile", "value": "performance"},
     "actions": [{"type": "kbd_colour", "value": "#c0182d"}]},
]


def start_demo_daemon():
    """A real tuf-controld against the fake tree, with demo rules and an effect."""
    from tuf_control.automation.config import update_config
    from tuf_control.automation.service import DaemonClient

    def edit(c):
        c["automation"]["rules"] = DEMO_RULES
        c["effects"]["active"] = "temperature"
        c["effects"]["notifications"]["enabled"] = True
    update_config(edit)
    client = DaemonClient()
    print("demo daemon:", client.start())
    for _ in range(50):
        time.sleep(0.1)
        if client.status()["running"] and client.status().get("log"):
            break
    return client


def _scroll_bottom(widget) -> None:
    from gi.repository import Gtk
    sw = widget
    while sw is not None and not isinstance(sw, Gtk.ScrolledWindow):
        sw = sw.get_first_child()
    if sw is not None:
        adj = sw.get_vadjustment()
        adj.set_value(adj.get_upper() - adj.get_page_size())


def automation_shots(win, out: Path, scheme: str, suffix: str, render):
    from tuf_control.effects.registry import EFFECTS
    page = win.pages["automation"]
    win.show_page("automation")
    yield 1500  # first daemon status poll
    page.poll_status()
    yield 400
    render(win, out / f"automation-{scheme}{suffix}.png")
    _scroll_bottom(page)
    yield 400
    render(win, out / f"automation-activity-{scheme}{suffix}.png")
    kb = win.pages["keyboard"]
    win.show_page("keyboard")
    kb.preview.fixed_t = 1.3
    yield 900
    for name, tag in (("audio", "keyboard-effects"), ("ambient", "keyboard-effects-ambient")):
        kb.soft.pick.set_selected(list(EFFECTS).index(name) + 1)
        yield 700
        _scroll_bottom(kb.prefs)
        yield 400
        render(win, out / f"{tag}-{scheme}{suffix}.png")
    # dialogs last: after an Adw.Dialog closes, this headless setup renders the window blank
    win.show_page("automation")
    yield 400
    dlg = page.open_editor("games-get-performance")
    yield 500
    dlg.act_rows["effect"].set_expanded(True)
    dlg.run_test()
    yield 900
    _scroll_bottom(dlg.prefs)
    yield 300
    render(win, out / f"automation-dialog-{scheme}{suffix}.png")
    dlg.force_close()
    new = page.open_editor(None)
    yield 300
    new.name.set_text("Battery saver")
    new.trig_row.set_selected(2)
    new.act_rows["profile"].set_enable_expansion(True)
    new.act_rows["profile"].set_expanded(True)
    yield 400
    render(win, out / f"automation-new-{scheme}{suffix}.png")
    new._save()  # through the page's locked config write
    yield 600
    from tuf_control.automation.config import load_config
    ids = [r["id"] for r in load_config()["automation"]["rules"]]
    if "battery-saver" not in ids:
        raise AssertionError(f"rule not saved: {ids}")
    print("automation: rule saved OK")


def inner(args) -> int:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    gi.require_version("Gsk", "4.0")
    gi.require_version("Graphene", "1.0")
    from gi.repository import Adw, GLib, Gsk, Gtk, Graphene

    from tuf_control.backends.fake import build_fake_tree
    from tuf_control.controller import FAKE_ROOT_ENV

    scheme = os.environ["TUF_SHOT_CHILD"]
    tmp = Path(tempfile.mkdtemp(prefix="tuf-shot-"))
    root = build_fake_tree(tmp / "root", ppt_readable=args.ppt)
    os.environ[FAKE_ROOT_ENV] = str(root)
    # never read or write the user's real automation config or daemon status
    from tuf_control.automation.config import CONFIG_DIR_ENV, RUNTIME_DIR_ENV
    os.environ[CONFIG_DIR_ENV] = str(tmp / "config")
    os.environ[RUNTIME_DIR_ENV] = str(tmp / "run")
    daemon = start_demo_daemon() if args.automation else None
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    w, h = (int(x) for x in args.size.split("x"))

    from tuf_control.core import Capability
    from tuf_control.ui.app import TufControlApp

    app = TufControlApp()
    state = {"failed": False}

    def render(widget, path: Path) -> None:
        ww, hh = widget.get_width(), widget.get_height()
        snap = Gtk.Snapshot()
        bg = widget.get_style_context().lookup_color("window_bg_color")[1]
        snap.append_color(bg, Graphene.Rect().init(0, 0, ww, hh))
        Gtk.WidgetPaintable(widget=widget).snapshot(snap, ww, hh)
        node = snap.to_node()
        renderer = Gsk.CairoRenderer.new()
        renderer.realize_for_display(widget.get_display())
        try:
            tex = renderer.render_texture(node, Graphene.Rect().init(0, 0, ww, hh))
            tex.save_to_png(str(path))
        finally:
            renderer.unrealize()
        print(f"wrote {path} ({ww}x{hh})")

    def steps(win):
        dash = win.pages["dashboard"]
        now = time.monotonic()
        for hist in dash.h.values():  # drop samples the window's own poll already took
            hist._data.clear()
        for i in range(120):  # two minutes of history from the simulated tree
            simulate(root, i)
            t = win.ctl.read(Capability.TELEMETRY)[0]
            dash.ingest(t, now - 120 + i + 1)
        yield 600
        suffix = f"-{args.tag}" if args.tag else ""
        for key in [p for p in args.pages.split(",") if p in win.pages] or (
                [] if args.pages else list(win.pages)):
            win.show_page(key)
            if key == "keyboard":
                win.pages["keyboard"].preview.fixed_t = 1.3
            yield 700
            if key == "fans":
                c = win.pages["fans"].canvas
                c.active = 4
                win.pages["fans"]._changed()
                c.queue_draw()
                yield 150
            render(win.get_content(), out / f"{key}-{scheme}{suffix}.png")
        if args.dialog:
            win.show_page("gpu")
            yield 300
            from tuf_control.core import GpuMode
            page = win.pages["gpu"]
            page.checks[GpuMode.ASUS_MUX_DGPU].set_active(True)
            page._switch()
            yield 600
            render(win, out / f"gpu-confirm-{scheme}{suffix}.png")
            win.last_dialog.force_close()
            page.refresh()
            yield 300
        if args.automation:
            yield from automation_shots(win, out, scheme, suffix, render)
        if args.exercise:
            yield from exercise(win, root, out, scheme, suffix, render)
        app.quit()

    def rapl():  # a steady ~28 W package draw for the fake RAPL counter
        (root / "sys/class/powercap/intel-rapl:0/energy_uj").write_text(
            f"{int(time.monotonic() * 28e6)}\n")
        return True

    def on_activate(_app):
        win = app.window
        rapl()
        GLib.timeout_add(250, rapl)
        win.set_default_size(w, h)
        it = steps(win)

        def tick():
            try:
                delay = next(it)
            except StopIteration:
                return False
            except Exception:
                import traceback
                traceback.print_exc()
                state["failed"] = True
                app.quit()
                return False
            GLib.timeout_add(delay, tick)
            return False

        GLib.timeout_add(1200, tick)

    app.connect_after("activate", on_activate)
    try:
        app.run([])
    finally:
        if daemon is not None:
            daemon.stop()
    shutil.rmtree(tmp, ignore_errors=True)
    return 1 if state["failed"] else 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out", default=str(REPO / "docs" / "screenshots"))
    p.add_argument("--schemes", default="dark,light")
    p.add_argument("--size", default="1180x800")
    p.add_argument("--pages", default="", help="comma list; default all")
    p.add_argument("--tag", default="", help="file name suffix, e.g. narrow")
    p.add_argument("--ppt", action="store_true",
                   help="fake firmware with readable power limits (default: hidden, like "
                        "EnablePptGroup off)")
    p.add_argument("--dialog", action="store_true", help="also capture the GPU confirm dialog")
    p.add_argument("--exercise", action="store_true",
                   help="drive profile/fan/GPU/keyboard/battery writes against the fake tree")
    p.add_argument("--user-theme", action="store_true", help="keep ~/.config/gtk-4.0 styles")
    p.add_argument("--automation", action="store_true",
                   help="run a demo tuf-controld on the fake tree; capture the Automation "
                        "page, rule dialogs and keyboard software effects")
    args = p.parse_args()
    if os.environ.get("TUF_SHOT_CHILD"):
        return inner(args)
    return outer(args)


if __name__ == "__main__":
    sys.exit(main())
