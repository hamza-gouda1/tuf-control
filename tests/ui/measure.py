#!/usr/bin/env python3
"""Measure the GUI's memory and idle CPU on this machine (read-only, headless).

    python3 tests/ui/measure.py [--idle 60] [--renderer gl|cairo|default] [--fake]

Starts a private `dbus-run-session` with `mutter --headless`, runs the app in its default
real mode (reads /proc, /sys, NVML and the system D-Bus daemons; it never writes unless
a user clicks), then:
  1. waits on the Dashboard and measures RSS/PSS and CPU over --idle seconds,
  2. visits every page once and records RSS after each,
  3. returns to the Dashboard, waits, and measures again.
Prints one JSON document. Only one GTK process runs; the compositor's own CPU is not
counted. RSS is resident memory as `ps -o rss` shows it; PSS divides shared pages
(libraries mapped by several processes) between them.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"


def mem(pid: int | str = "self") -> dict:
    out = {}
    try:
        with open(f"/proc/{pid}/smaps_rollup") as fh:
            for line in fh:
                k, _, v = line.partition(":")
                if k in ("Rss", "Pss", "Private_Dirty", "Shared_Clean", "Pss_Anon", "Pss_File"):
                    out[k.lower()] = int(v.split()[0])
    except OSError:
        pass
    return out


def cpu_s() -> float:
    t = os.times()
    return t.user + t.system


def outer(args) -> int:
    if not os.environ.get("TUF_MEASURE_BUS"):
        env = {**os.environ, "TUF_MEASURE_BUS": "1"}
        return subprocess.call(["dbus-run-session", "--", sys.executable, __file__,
                                *sys.argv[1:]], env=env)
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", tempfile.gettempdir()))
    sock = f"tuf-measure-{os.getpid()}"
    mutter = subprocess.Popen(
        ["mutter", "--headless", "--no-x11", f"--wayland-display={sock}",
         "--virtual-monitor", "1380x1000"],
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
        env = {**os.environ, "GDK_BACKEND": "wayland", "WAYLAND_DISPLAY": sock,
               "TUF_MEASURE_CHILD": "1", "PYTHONPATH": str(SRC), "ADW_DISABLE_PORTAL": "1",
               "GDK_DEBUG": "no-portals", "GTK_A11Y": "none"}
        env.pop("DISPLAY", None)
        if args.renderer != "default":
            env["GSK_RENDERER"] = {"gl": "ngl"}.get(args.renderer, args.renderer)
        else:
            env.pop("GSK_RENDERER", None)
        if args.fake:
            from_tree = tempfile.mkdtemp(prefix="tuf-measure-")
            env["TUF_MEASURE_FAKE"] = from_tree
        return subprocess.call([sys.executable, __file__, *sys.argv[1:]], env=env,
                               timeout=args.idle * 3 + 300)
    finally:
        mutter.terminate()
        try:
            mutter.wait(5)
        except subprocess.TimeoutExpired:
            mutter.kill()


def inner(args) -> int:
    fake = os.environ.get("TUF_MEASURE_FAKE")
    if fake:
        sys.path.insert(0, str(SRC))
        from tuf_control.backends.fake import build_fake_tree
        os.environ["TUF_CONTROL_FAKE_ROOT"] = str(build_fake_tree(Path(fake) / "root"))
        from tuf_control.automation.config import CONFIG_DIR_ENV, RUNTIME_DIR_ENV
        os.environ[CONFIG_DIR_ENV] = str(Path(fake) / "config")
        os.environ[RUNTIME_DIR_ENV] = str(Path(fake) / "run")
    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import GLib

    from tuf_control.ui.app import TufControlApp
    app = TufControlApp()
    result: dict = {"renderer": args.renderer, "fake": bool(fake), "pid": os.getpid()}

    def steps(win):
        yield 5000  # startup settles
        result["startup"] = mem()
        yield int(args.warmup * 1000)
        c0, t0 = cpu_s(), time.monotonic()
        yield int(args.idle * 1000)
        c1, t1 = cpu_s(), time.monotonic()
        result["dashboard_idle"] = {**mem(), "cpu_pct": round(100 * (c1 - c0) / (t1 - t0), 2),
                                    "seconds": round(t1 - t0, 1),
                                    "sample_ms": round(win.hub.sampler.cost_ms, 2)}
        visits = {}
        for key in list(win.pages):
            win.show_page(key)
            c0, t0 = cpu_s(), time.monotonic()
            yield 6000 if key == "processes" else 3000
            c1, t1 = cpu_s(), time.monotonic()
            visits[key] = {"rss": mem().get("rss"),
                           "cpu_pct": round(100 * (c1 - c0) / (t1 - t0), 1)}
        result["visits"] = visits
        result["after_all_pages"] = mem()
        win.show_page("dashboard")
        yield int(args.warmup * 1000)
        c0, t0 = cpu_s(), time.monotonic()
        yield int(args.idle * 1000)
        c1, t1 = cpu_s(), time.monotonic()
        result["dashboard_after"] = {**mem(), "cpu_pct": round(100 * (c1 - c0) / (t1 - t0), 2)}
        win.show_page("cpu")
        yield 2000
        c0, t0 = cpu_s(), time.monotonic()
        yield 20000
        c1, t1 = cpu_s(), time.monotonic()
        result["cpu_page_2hz"] = {"cpu_pct": round(100 * (c1 - c0) / (t1 - t0), 2)}
        win.show_page("processes")
        yield 3000
        c0, t0 = cpu_s(), time.monotonic()
        yield 20000
        c1, t1 = cpu_s(), time.monotonic()
        result["processes_page"] = {"cpu_pct": round(100 * (c1 - c0) / (t1 - t0), 2),
                                    "rss": mem().get("rss"), "procs": len(win.hub.procs)}
        win.show_page("dashboard")
        yield 1000
        mapped = Path(f"/proc/{os.getpid()}/maps").read_text()
        result["loads_nvidia_libs"] = "libnvidia" in mapped
        result["loads_llvm"] = "libLLVM" in mapped
        result["gsk_renderer"] = type(win.get_renderer()).__name__ if win.get_renderer() \
            else None
        try:
            fds = [os.readlink(f"/proc/self/fd/{f}") for f in os.listdir("/proc/self/fd")]
        except OSError:
            fds = []
        result["nvidia_device_fds"] = sum(1 for f in fds if f.startswith("/dev/nvidia"))
        print(json.dumps(result, indent=1))
        app.quit()

    def on_activate(_app):
        it = steps(app.window)

        def tick():
            try:
                delay = next(it)
            except StopIteration:
                return False
            GLib.timeout_add(delay, tick)
            return False
        GLib.timeout_add(100, tick)

    app.connect_after("activate", on_activate)
    app.run([])
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--idle", type=float, default=60.0)
    p.add_argument("--warmup", type=float, default=10.0)
    p.add_argument("--renderer", default="default", choices=["default", "gl", "cairo", "ngl"])
    p.add_argument("--fake", action="store_true", help="fake hardware tree instead of real")
    args = p.parse_args()
    if os.environ.get("TUF_MEASURE_CHILD"):
        return inner(args)
    return outer(args)


if __name__ == "__main__":
    sys.exit(main())
