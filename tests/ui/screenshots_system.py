#!/usr/bin/env python3
"""Headless screenshots and a drive-through of the Updates and Services pages, fake data only.

    python3 tests/ui/screenshots_system.py [--out docs/screenshots] [--schemes dark,light]

Same machinery as screenshots.py (private D-Bus session, `mutter --headless`, off-screen
rendering); TUF_CONTROL_FAKE_ROOT is always set, so the update sources are the fixed fake
set, the services backend is in-memory, and every "install" or "stop" lands in a log in the
temporary tree. Nothing on the machine is updated or changed.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent))
import screenshots as base  # noqa: E402  (the shared outer: bus + compositor)


def inner(args) -> int:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    gi.require_version("Gsk", "4.0")
    gi.require_version("Graphene", "1.0")
    from gi.repository import Adw, GLib, Graphene, Gsk, Gtk

    from tuf_control.automation.config import CONFIG_DIR_ENV, RUNTIME_DIR_ENV
    from tuf_control.backends.fake import build_fake_tree
    from tuf_control.controller import FAKE_ROOT_ENV

    scheme = os.environ["TUF_SHOT_CHILD"]
    tmp = Path(tempfile.mkdtemp(prefix="tuf-shot-sys-"))
    root = build_fake_tree(tmp / "root")
    os.environ[FAKE_ROOT_ENV] = str(root)
    os.environ[CONFIG_DIR_ENV] = str(tmp / "config")
    os.environ[RUNTIME_DIR_ENV] = str(tmp / "run")
    from tuf_control.agent.store import STATE_DIR_ENV
    os.environ[STATE_DIR_ENV] = str(tmp / "state")
    os.environ["TUF_CONTROL_AGENT_NOTIFY"] = "0"
    # a fresh cached report, as tuf-controld would have left it twenty minutes ago
    import time

    from tuf_control.updates import check
    from tuf_control.updates.model import Report
    rep = check.collect(now=time.time() - 20 * 60)

    def put(d):
        d["report"] = rep.to_dict()
    check.update_cache(put)
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
        renderer = Gsk.CairoRenderer.new()
        renderer.realize_for_display(widget.get_display())
        try:
            renderer.render_texture(snap.to_node(), Graphene.Rect().init(0, 0, ww, hh)) \
                .save_to_png(str(path))
        finally:
            renderer.unrealize()
        print(f"wrote {path} ({ww}x{hh})")

    def steps(win):
        hub = win.hub
        hub.hold = True
        hub.stop()
        problems = []
        yield 500
        if win.updates_badge.get_label() != str(rep.total):
            problems.append(f"badge {win.updates_badge.get_label()!r} != {rep.total}")
        up = win.pages["updates"]
        win.show_page("updates")
        yield 900
        if up.busy:
            problems.append("fresh cache should not start a check")
        render(win.get_content(), out / f"updates-{scheme}.png")
        # expand the BIOS row and the first package
        g = next(x for x in up.dynamic if "Firmware" in (x.get_title() or ""))
        exp = base._find(g, Adw.ExpanderRow)
        if exp is not None:
            exp.set_expanded(True)
        yield 300
        base._scroll_bottom(up.prefs)
        yield 400
        render(win.get_content(), out / f"updates-details-{scheme}.png")

        sv = win.pages["services"]
        win.show_page("services")
        yield 900
        names = [sv.store.get_item(i).unit.name for i in range(sv.store.get_n_items())]
        if "cups.service" not in names:
            problems.append(f"services not listed: {names[:5]}")
        sv.sel.set_selected(names.index("cups.service"))
        yield 500
        render(win.get_content(), out / f"services-{scheme}.png")
        # safety: the protected unit has Stop disabled
        sv.sel.set_selected(names.index("NetworkManager.service"))
        yield 300
        if sv.op_btns["stop"].get_sensitive():
            problems.append("Stop allowed on NetworkManager")
        sv.search.set_text("blue")
        yield 600
        if sv.store.get_n_items() != 1:
            problems.append(f"filter left {sv.store.get_n_items()} rows")
        sv.search.set_text("")
        yield 400
        sv.scope_btn["user"].set_active(True)
        yield 900
        render(win.get_content(), out / f"services-user-{scheme}.png")
        # an action through the confirmation (fake backend): stop syncthing? it is stopped;
        # start it
        names = [sv.store.get_item(i).unit.name for i in range(sv.store.get_n_items())]
        sv.sel.set_selected(names.index("syncthing.service"))
        yield 300
        sv._ask("start")
        yield 400
        win.last_dialog.emit("response", "ok")
        win.last_dialog.force_close()
        yield 1500
        if "start syncthing.service" not in sv.backend().log:
            problems.append(f"start not applied: {sv.backend().log}")
        # dialogs last (a closed Adw.Dialog blanks this headless window afterwards)
        win.show_page("updates")
        yield 500
        up._update_all()
        yield 600
        render(win, out / f"updates-confirm-{scheme}.png")
        win.last_dialog.emit("response", "ok")
        win.last_dialog.force_close()
        for _ in range(40):
            yield 250
            if not up.busy:
                break
        log = check.cache_dir() / "actions.log"
        text = log.read_text() if log.exists() else ""
        # the fake battery is discharging: firmware must be skipped, the rest applied
        if "apply packagekit" not in text or "apply snapd" not in text:
            problems.append(f"apply not recorded: {text!r}")
        if "fwupd" in text:
            problems.append("firmware applied on battery")
        print("system pages:", "OK" if not problems else "; ".join(problems))
        if problems:
            raise AssertionError(problems)
        app.quit()

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
                import traceback
                traceback.print_exc()
                state["failed"] = True
                app.quit()
                return False
            GLib.timeout_add(delay, tick)
            return False
        GLib.timeout_add(1200, tick)

    app.connect_after("activate", on_activate)
    app.run([])
    shutil.rmtree(tmp, ignore_errors=True)
    _ = Report
    return 1 if state["failed"] else 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out", default=str(base.REPO / "docs" / "screenshots"))
    p.add_argument("--schemes", default="dark,light")
    p.add_argument("--size", default="1180x800")
    p.add_argument("--user-theme", action="store_true")
    args = p.parse_args()
    if os.environ.get("TUF_SHOT_CHILD"):
        return inner(args)
    base.__file__ = str(HERE)  # base.outer re-runs "this" script for each scheme
    return base.outer(args)


if __name__ == "__main__":
    sys.exit(main())
