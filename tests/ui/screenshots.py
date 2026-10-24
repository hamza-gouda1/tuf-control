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


def seed_agent_demo() -> None:
    """Calls from a demo MCP client against the fake tree: reads, dry runs, two requests
    waiting for approval (one high-risk) and a change made under a standing grant."""
    from tuf_control.agent import policy
    from tuf_control.agent.tools import AgentService
    svc = AgentService()
    svc.set_client("desktop-assistant", "2.1")
    for name, a in (("get_status", {}), ("get_telemetry", {"sections": ["cpu", "fans"]}),
                    ("list_processes", {"limit": 5})):
        svc.call(name, a)
    policy.grant("control-all", high_risk=True)
    svc.call("set_charge_limit", {"percent": 80})
    svc.call("set_charge_limit", {"percent": 80, "dry_run": False,
                                  "reason": "Mostly plugged in today; protect the battery"})
    pts = [(40, 40), (50, 70), (58, 100), (65, 130), (72, 165), (80, 200), (88, 235),
           (97, 255)]
    svc.call("set_fan_curve", {"profile": "balanced", "fan": "cpu", "dry_run": False,
                               "points": [{"temp_c": t, "pwm": p} for t, p in pts],
                               "reason": "Fans ramp late under the current load"})
    svc.call("set_power_limit", {"name": "ppt_pl1_spl", "value": 300})
    policy.grant("control-basic", 3600)
    svc.call("set_keyboard_brightness", {"level": 1, "dry_run": False})


def agent_shots(win, out: Path, scheme: str, suffix: str, render):
    page = win.pages["agent"]
    win.show_page("agent")
    yield 900
    render(win, out / f"agent-access-{scheme}{suffix}.png")
    _scroll_bottom(page)
    yield 400
    render(win, out / f"agent-access-activity-{scheme}{suffix}.png")
    # drive the page: approve the basic request, then the kill switch
    from tuf_control.agent import policy
    from tuf_control.agent.present import pending_rows
    rows = pending_rows(page.queue.pending())
    basic = next(r for r in rows if not r["high"])
    page._decide(basic, True)
    yield 600
    problems = []
    if page.queue.get(basic["id"])["status"] != "approved":
        problems.append("approve did not land")
    page._revoke()
    yield 600
    if policy.load_access().enabled or page.queue.pending():
        problems.append("revoke did not land")
    if page.master.get_active() or page.pending_rows:
        problems.append("page did not follow the revoke")
    print("agent page:", "OK" if not problems else "; ".join(problems))
    if problems:
        raise AssertionError(problems)


def _find(widget, cls):
    """First mapped descendant of `widget` that is a `cls` (popovers live under their row)."""
    child = widget.get_first_child()
    while child is not None:
        if isinstance(child, cls) and child.get_mapped():
            return child
        hit = _find(child, cls)
        if hit is not None:
            return hit
        child = child.get_next_sibling()
    return None


def popover_shots(win, out: Path, scheme: str, suffix: str, render, render_over):
    """Keyboard page with its dropdowns open, and the colour chooser."""
    from gi.repository import Gtk
    from tuf_control.effects.registry import EFFECTS
    kb = win.pages["keyboard"]
    win.show_page("keyboard")
    kb.preview.fixed_t = 1.3
    yield 900
    for row, tag in ((kb.mode_row, "keyboard-dropdown"),
                     (kb.soft.pick, "keyboard-effects-dropdown")):
        if tag == "keyboard-effects-dropdown":
            _scroll_bottom(kb.prefs)
            yield 300
        row.activate()
        yield 600
        pop = _find(row, Gtk.Popover)
        if pop is None:
            raise AssertionError(f"{tag}: no popover opened")
        render_over(win, pop, out / f"{tag}-{scheme}{suffix}.png")
        pop.popdown()
        yield 300
    kb.soft.pick.set_selected(list(EFFECTS).index("spectrum") + 1)
    yield 600
    _scroll_bottom(kb.prefs)
    yield 300
    render(win, out / f"keyboard-effects-spectrum-{scheme}{suffix}.png")
    kb.c1.activate()
    yield 1200
    dlg = next((w for w in Gtk.Window.list_toplevels()
                if w is not win and isinstance(w, Gtk.Window) and w.get_mapped()), None)
    if dlg is None:
        raise AssertionError("colour chooser did not open")
    render(dlg, out / f"keyboard-colour-{scheme}{suffix}.png")
    dlg.close()
    yield 300


def keyboard_shots(win, out: Path, scheme: str, suffix: str, render):
    """Hardware Breathe on the single-colour (TUF) fake keyboard: firmware Colour 1 <-> off,
    then the Two colours switch (Colour 2, palettes, a fading swatch)."""
    kb = win.pages["keyboard"]
    win.show_page("keyboard")
    kb.preview.fixed_t = kb.swatch.fixed_t = 1.3
    yield 900
    kb.mode_row.set_selected(kb.form.modes.index(1))
    yield 400
    render(win.get_content(), out / f"keyboard-breathe-{scheme}{suffix}.png")
    kb.blend_row.set_active(True)
    kb.preview.fixed_t = kb.swatch.fixed_t = 2.0  # half a cycle: all Colour 2
    yield 500
    render(win.get_content(), out / f"keyboard-breathe-two-{scheme}{suffix}.png")
    kb.mode_row.set_selected(kb.form.modes.index(0))
    yield 300


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
    from tuf_control.agent.store import STATE_DIR_ENV  # agent audit log and approval queue
    os.environ[STATE_DIR_ENV] = str(tmp / "state")
    os.environ["TUF_CONTROL_AGENT_NOTIFY"] = "0"
    if args.agent:
        seed_agent_demo()
    daemon = start_demo_daemon() if args.automation else None
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    w, h = (int(x) for x in args.size.split("x"))

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

    def render_over(win, pop, path: Path) -> None:
        """The window with an open popover composited where the compositor put it."""
        ww, hh = win.get_width(), win.get_height()
        surf = pop.get_surface()
        px, py = surf.get_position_x(), surf.get_position_y()
        ptx, pty = pop.get_surface_transform()
        wtx, wty = win.get_surface_transform()
        x, y = px + ptx - wtx, py + pty - wty
        snap = Gtk.Snapshot()
        bg = win.get_style_context().lookup_color("window_bg_color")[1]
        snap.append_color(bg, Graphene.Rect().init(0, 0, ww, hh))
        Gtk.WidgetPaintable(widget=win).snapshot(snap, ww, hh)
        snap.save()
        snap.translate(Graphene.Point().init(x, y))
        Gtk.WidgetPaintable(widget=pop).snapshot(snap, pop.get_width(), pop.get_height())
        snap.restore()
        renderer = Gsk.CairoRenderer.new()
        renderer.realize_for_display(win.get_display())
        try:
            renderer.render_texture(snap.to_node(),
                                    Graphene.Rect().init(0, 0, ww, hh)).save_to_png(str(path))
        finally:
            renderer.unrealize()
        print(f"wrote {path} (popover at {x:.0f},{y:.0f} {pop.get_width()}x{pop.get_height()})")

    def steps(win):
        # telemetry: stop the live timer and replay two minutes of the fake world on a
        # virtual clock (1 Hz), so every graph has the same deterministic history
        hub = win.hub
        hub.hold = True
        hub.stop()
        hub.store.clear()
        clock = {"t": time.monotonic() - 125.0}
        hub.procs_wanted = True
        for i in range(120):
            simulate(root, i)
            clock["t"] += 1.0
            hub.collect_now(clock["t"])
        hub.procs_wanted = False

        def tick(n=1):
            for _ in range(n):
                clock["t"] += 1.0
                hub.collect_now(clock["t"], notify=True)
        tick()
        yield 600
        suffix = f"-{args.tag}" if args.tag else ""
        for key in [p for p in args.pages.split(",") if p in win.pages] or (
                [] if args.pages else list(win.pages)):
            win.show_page(key)
            if key == "keyboard":
                win.pages["keyboard"].preview.fixed_t = 1.3
            tick(3 if key == "processes" else 1)
            yield 700
            if key == "processes":  # open the busiest app and select its first process
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
        if args.keyboard:
            yield from keyboard_shots(win, out, scheme, suffix, render)
        if args.popovers:
            yield from popover_shots(win, out, scheme, suffix, render, render_over)
        if args.automation:
            yield from automation_shots(win, out, scheme, suffix, render)
        if args.agent:
            yield from agent_shots(win, out, scheme, suffix, render)
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
    p.add_argument("--keyboard", action="store_true",
                   help="keyboard page: hardware Breathe, and with its Two colours switch on")
    p.add_argument("--popovers", action="store_true",
                   help="keyboard page with its dropdowns open, and the colour chooser")
    p.add_argument("--agent", action="store_true",
                   help="seed agent access demo data (calls, pending requests) and capture "
                        "the Agent access page")
    p.add_argument("--automation", action="store_true",
                   help="run a demo tuf-controld on the fake tree; capture the Automation "
                        "page, rule dialogs and keyboard software effects")
    args = p.parse_args()
    if os.environ.get("TUF_SHOT_CHILD"):
        return inner(args)
    return outer(args)


if __name__ == "__main__":
    sys.exit(main())
