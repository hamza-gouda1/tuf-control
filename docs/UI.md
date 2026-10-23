# User interface

GTK 4 + Libadwaita 1.5 through PyGObject. Runs as the user; no root.

    PYTHONPATH=src python3 -m tuf_control.ui          # or: tuf-control-gui (after pip install)
    TUF_CONTROL_FAKE_ROOT=/tmp/tree PYTHONPATH=src python3 -m tuf_control.ui   # fake hardware

Create a fake tree with `python3 -c "from tuf_control.backends.fake import build_fake_tree;
build_fake_tree('/tmp/tree')"`. `TUF_CONTROL_COLOR_SCHEME=dark|light` forces a scheme;
otherwise the system preference is followed.

## Layout

`src/tuf_control/ui/`

| File | Role |
| --- | --- |
| `app.py` | `Adw.Application` (`io.github.hamzagouda.TufControl`), styles, icon path, `main()` |
| `window.py` | `Adw.NavigationSplitView` sidebar + `Gtk.Stack` pages, header profile chip and battery indicator, toasts, confirm dialogs, 1 s telemetry poll |
| `viewmodels.py` | Display-free logic: formatting, `History`, power-limit view, `FanCurveModel` (clamped drags), `CurveGeometry`, GPU/keyboard/battery helpers, Mutter state parsing |
| `widgets.py` | `Sparkline`, `Tile`, `FanCurveCanvas`, `KeyboardPreview` (Cairo) |
| `display_config.py` | `org.gnome.Mutter.DisplayConfig` client (async Gio) and an in-memory fake |
| `tasks.py` | `run_bg()`: worker pool (`backends.pool.run_async`) -> result via `GLib.idle_add` |
| `theme.py`, `style.css`, `style-dark.css` | Wine palette, Geist Mono detection, dark overlay |
| `pages/` | One module per page |

`data/`: desktop file, metainfo, scalable app icon.

## Rules the UI follows

- No blocking work on the main thread. Every controller call goes through `run_bg`; the
  backends carry their own D-Bus/subprocess timeouts. Display changes use async Gio.
- Nothing is invented. Power limits show sliders only when the firmware reports min and
  max; otherwise the reason from `Controller.capabilities()` is shown (on a TUF with
  `EnablePptGroup` off that is "no PPT values (-1)").
- Confirmation dialogs guard the risky writes: power limits, fan curves (apply and factory
  reset) and GPU mode (with a restart warning for MUX changes). Profile, charge limit,
  keyboard and refresh rate apply directly with a toast (refresh rate has Undo).
- Every write ends in a toast with the result or the real error (`humanize_error`).
- With `TUF_CONTROL_FAKE_ROOT` set, only the fake backend and a fake display config exist.
  Dialogs and toasts say so.
- The dGPU is never woken to read sensors: telemetry skips `nvidia-smi` unless the dGPU's
  runtime PM state is `active`.

## Pages

Dashboard (CPU, GPU, fans, battery tiles with 120 s graphs), Performance (profile cards,
power limits), Fans (8-point curve per fan and profile; drag or arrow keys; Apply / Revert /
Factory), Graphics (Integrated / Hybrid / Ultimate via supergfxd; pending-restart banner),
Display (refresh rate, panel overdrive, VRR note), Battery (charge limit 20–100 with 60/80/100
presets, one-shot full charge, health), Keyboard (brightness, hardware effect, live preview,
software effects placeholder), Automation (placeholder), About (support table per capability).

## Controller additions used by the UI

`get/set_kbd_brightness`, `get_kbd_rgb`, `set_kbd_effect`, `get_fan_curves(profile)`,
`set_fan_curve`, `reset_fan_curves`, `get_gpu_status`, `set_gpu_mode` (returns the required
user action), `get_attributes`, `set_attribute`, `one_shot_full_charge`. The sysfs backend
can only read and write the active profile's fan curve; the Fans page follows the active
profile and says so when another one is picked.

D-Bus method names used for the new writes (asusd `SetFanCurve`, `ResetProfileCurves`,
`OneShotFullCharge`, `LedModeData` property; supergfxd `SetMode`, `PendingMode`,
`PendingUserAction`, `Power`, `Supported`) follow asusctl 6 / supergfxctl 5 and have not
been exercised against real daemons yet.

## Tests and screenshots

    PYTHONPATH=src python3 -m unittest discover -s tests      # includes tests/ui (no display)
    python3 tests/ui/screenshots.py                           # docs/screenshots/*-{dark,light}.png
    python3 tests/ui/screenshots.py --exercise --dialog       # + drives the writes, checks the tree
    python3 tests/ui/screenshots.py --size 380x820 --tag narrow --schemes dark
    python3 tests/ui/screenshots.py --ppt --pages performance --tag ppt

The script starts a private `dbus-run-session` with `mutter --headless --virtual-monitor`,
runs the app on that Wayland socket against a fresh fake tree (sensor values animated for
the graphs), and renders each page with `Gtk.WidgetPaintable` -> `Gsk.CairoRenderer` -> PNG.
`--user-theme` keeps `~/.config/gtk-4.0/gtk.css`; by default it is bypassed so the shots
show the app's own styling.
