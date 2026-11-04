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
| `window.py` | `Adw.NavigationSplitView` sidebar (Monitor / Control sections) + `Gtk.Stack`; pages imported and built on first visit (`LazyPages`), hidden monitor pages destroyed after 30 s; header profile chip and battery indicator, toasts, confirm dialogs |
| `live.py` | `LiveHub`: the one telemetry timer (1 Hz, 2 Hz on a detail page, stopped while the window is hidden) over `tuf_control.telemetry.Sampler`; process walk only while Processes is visible |
| `charts.py`, `chartmath.py` | Cairo `Chart` (axes, grid, time axis, hover readout, stacked areas), `CoreGrid`, `CompositionBar`, `Meter`; display-free windowing, downsampling and monotone curves |
| `lean.py` | Cairo renderer by default (`TUF_CONTROL_RENDERER=gl` to change), `malloc_trim`; see [PERFORMANCE.md](PERFORMANCE.md) |
| `viewmodels.py` | Display-free logic: formatting, `History`, power-limit view, `FanCurveModel` (clamped drags), `CurveGeometry`, GPU/keyboard/battery helpers, Mutter state parsing |
| `widgets.py` | `Sparkline`, `Tile`, `FanCurveCanvas`, `KeyboardPreview` (Cairo) |
| `display_config.py` | `org.gnome.Mutter.DisplayConfig` client (async Gio) and an in-memory fake |
| `tasks.py` | `run_bg()`: worker pool (`backends.pool.run_async`) -> result via `GLib.idle_add` |
| `theme.py`, `style.css`, `style-dark.css` | Wine palette, Geist Mono detection, dark overlay |
| `monitor.css` | Monitor pages (cards, stat cells, process table); named colours only |
| `pages/` | One module per page |

`data/`: desktop file, metainfo, scalable app icon.

## Rules the UI follows

- No blocking work on the main thread. Every controller call goes through `run_bg`; the
  backends carry their own D-Bus/subprocess timeouts. Display changes use async Gio.
- Nothing is invented. Power limits show sliders only when the firmware reports min and
  max; otherwise the reason from `Controller.capabilities()` is shown (on a TUF with
  `EnablePptGroup` off that is "no PPT values (-1)").
- Confirmation dialogs guard the risky writes: power limits, fan curves (apply and factory
  reset), GPU mode (with a restart warning for MUX changes), update installation
  (including firmware warnings), restart requests and high-risk agent approvals.
  Rule deletion and stopping the shared service from Keyboard also ask for confirmation.
  Profile, charge limit, keyboard and refresh rate apply directly with a toast
  (refresh rate has Undo).
- Every write ends in a toast with the result or the real error (`humanize_error`).
- With `TUF_CONTROL_FAKE_ROOT` set, only the fake backend and a fake display config exist.
  Dialogs and toasts say so.
- Popovers, dropdowns and menus are one rounded surface: `ui/style-popover.css` is loaded
  above user stylesheets (`STYLE_PROVIDER_PRIORITY_USER + 1`) and only clears the popover
  node itself, because a user `gtk.css` rule like `window, .background { background-color:
  … }` also matches `popover.background` and filled the whole popup surface (shadow margin
  and arrow area) as a dark rectangle behind the list. Check with `--user-theme --popovers`.
- The dGPU is never woken to read sensors: NVML (or `nvidia-smi`) is only used while the
  dGPU's runtime PM state is `active`, and released when it idles so it can sleep again.
- Nothing is cut off. Text wraps (or ellipsizes with the full text in a tooltip); numbers and
  their units use no-break spaces (`viewmodels.nb`) so a reading never splits across lines.
  Secondary text is 72 % opacity on light (about 5:1, WCAG AA) and the softer 55 % on dark.
- One width class drives the responsive layout: `MainWindow.responsive(fn)` calls `fn(level)`
  now and on every breakpoint change (0 wide, 1 up to 900 sp, 2 up to 640 sp, 3 up to
  460 sp). Process table columns drop out, button rows stack and the header sheds the battery
  readout as the window narrows; the minimum window width is 360 px. Do not add breakpoint
  setters after the fact: one added once a breakpoint is active is not applied.
- Controls that have no text of their own (switches, scales, radio buttons, icon-only
  buttons) get an accessible name (`pages.base.a11y_name`) and icon-only buttons a tooltip.
- Monitor values come from `tuf_control.telemetry` (/proc, /sys, NVML; no dependencies).
  A reading that is missing is a gap in the graph, never a zero; what needs root (RAPL
  package power, per-process network) says so.

## Pages

Monitor: Dashboard (live cards with 120 s graphs for CPU, memory, NVIDIA and Intel GPU,
CPU fan, GPU fan, storage, network, battery; a card opens its page), Processes (apps grouped
by systemd scope plus one "System processes" group, or all processes; columns CPU, Memory,
Swap (VmSwap), Drive (read + write), GPU, GPU memory and PID, each header carrying the
machine-wide figure like Mission Center; sort, filter; Stop = SIGTERM and Force Stop =
SIGKILL, both confirmed, refused for PID 1 and TUF Control itself; app names and icons come
from the cgroup scope, the .desktop id, StartupWMClass or the executable), Processor (stacked
user/system/iowait, 24-CPU
grid with P/E cores, clock, temperatures, load, governor/EPP), Memory (in use / cache stack,
composition bar, swap, zswap, pressure), NVIDIA graphics and Intel graphics (utilisation,
clocks, temperature, power, VRAM, engines, processes; sleeping state; a dGPU whose runtime PM
state is `error` shows "GPU unavailable: the NVIDIA driver lost the GPU" and is never polled),
Fans & temps (both fans,
all temperature sensors, link to Fan curves), Storage (per-disk rates, busy, temperature,
file systems), Network (per-interface rates; virtual interfaces hidden by default).

Graph axes never move with the data: percentages are 0 to 100, memory and VRAM 0 to the
total, temperatures 20 to 100 degrees, fans 0 to 7k rpm; the rest (rates, clocks, power) start
from a generous minimum span and only ever widen. The time axis is fixed (-120 s to now).

Control: Performance (profile cards,
power limits), Fan curves (8-point curve per fan and profile; drag or arrow keys;
Apply / Revert / Factory), GPU mode (Integrated / Hybrid / Ultimate via supergfxd;
pending-restart banner),
Display (refresh rate, panel overdrive, VRR note), Touchpad & gestures (touchpad
send-events, tap to click, natural scrolling, speed, click method; swipe and pinch bindings
for the Quick Settings extension's gestures, off by default; see [GESTURES.md](GESTURES.md)),
Battery (charge limit 20–100 with 60/80/100 presets, one-shot full charge, health), Keyboard (brightness, hardware effect, live preview,
software effects: picker, parameters, simulated preview, service status; see
[EFFECTS.md](EFFECTS.md)), Automation (tuf-controld service, rules with enable switches and an
"Active" badge, add/edit dialog with a dry-run Test, suggestions, recent activity; see
[AUTOMATION.md](AUTOMATION.md)), Agent access (access levels, standing approval for
permitted lower-risk changes, pending one-time approvals, high-risk confirmation,
Revoke all and audit log; see [AGENT_API.md](AGENT_API.md)), Updates (source status,
software and firmware items, confirmation before installation, live progress,
installed-version verification, transaction history and a restart banner; see
[UPDATES.md](UPDATES.md)), About (support table per capability).

## Controller additions used by the UI

`get/set_kbd_brightness`, `get_kbd_rgb`, `set_kbd_effect`, `get_fan_curves(profile)`,
`set_fan_curve`, `reset_fan_curves`, `get_gpu_status`, `set_gpu_mode` (returns the required
user action), `get_attributes`, `set_attribute`, `one_shot_full_charge`. The sysfs backend
can only read and write the active profile's fan curve; the Fan curves page follows the active
profile and says so when another one is picked.

D-Bus names and signatures were verified by read-only introspection of asusd 6.3.5 and
supergfxd: `SetFanCurve(u, (s, (yyyyyyyy), (yyyyyyyy), b))`, `SetCurvesToDefaults(u)` (there is no
`ResetProfileCurves`), `OneShotFullCharge()`, aura `LedModeData` is `(uu(yyy)(yyy)ss)` with speed
and direction as enum *names* ("Low"/"Med"/"High", "Right"/"Left"/"Up"/"Down"), supergfxd
`SetMode(u) -> u`, `PendingMode`, `PendingUserAction`, `Power`, `Supported`. The keyboard
`LedModeData`/`Brightness` writes were run against asusd 6.3.5 on the FX608JHR (byte-identical
to `asusctl aura effect`, read back after each Apply); the other write calls have only been
tested against a recording transport.

## Tests, QA lint and screenshots

    PYTHONPATH=src python3 -m unittest discover -s tests      # includes tests/ui (no display)
    python3 tests/ui/screenshots.py                           # docs/screenshots/*-{dark,light}.png
    python3 tests/ui/screenshots.py --exercise --dialog       # + drives the writes, checks the tree
    python3 tests/ui/screenshots.py --size 380x820 --tag narrow --schemes dark
    python3 tests/ui/screenshots.py --ppt --pages performance --tag ppt
    python3 tests/ui/screenshots.py --automation --pages automation   # automation-*, keyboard-effects-*
    python3 tests/ui/measure.py --idle 60       # RSS/PSS and idle CPU, real mode, headless
    PYTHONPATH=src python3 tests/hardware/validate_telemetry.py   # vs top, free, sensors, ...
    python3 tests/ui/screenshots.py --pages keyboard --popovers  # dropdowns open, colour chooser

The screenshot and measurement scripts first enter a 1500 MiB systemd scope with swap
disabled and a hard timeout. They use a private HOME/XDG tree and the keyfile GSettings
backend; optional user-theme files are copied as read-only inputs. The screenshot script
starts a private `dbus-run-session` with `mutter --headless --virtual-monitor`,
runs the app on that Wayland socket against a fresh fake tree (sensor values animated for
the graphs), and renders each page with `Gtk.WidgetPaintable` -> `Gsk.CairoRenderer` -> PNG.
`--user-theme` keeps `~/.config/gtk-4.0/gtk.css`; by default it is bypassed so the shots
show the app's own styling. The automation config and daemon status always live in the run's
temporary directory; `--automation` starts a real `tuf-controld` against the fake tree with demo
rules and stops it afterwards.

## UI QA lint

`tests/ui/qa_lint.py` runs every page, dialog, toast and popover headless and audits the widget
tree and the rendered pixels. It re-executes itself under
`systemd-run --user --scope -p MemoryMax=1500M -p MemorySwapMax=0`, one GTK process at a time,
each with a timeout, against the fake tree (nothing real is read or written).

    python3 tests/ui/qa_lint.py                 # full matrix, about 20 minutes
    python3 tests/ui/qa_lint.py --quick         # 1280x720 and 360x640, dark and light
    python3 tests/ui/qa_lint.py --sizes 360x640 --variants dark --pages fans,battery
    TUF_QA_SLOW=1 python3 -m unittest tests.ui.test_qa_lint   # the --quick run as a test

The matrix also drives a fake Updates transaction, checking the running progress panel,
version-verified pending result and expanded history at each normal size. The `gpu-lost`
stress mode forces a PCI runtime error with no NVML backend to check recovery advice.

Matrix: sizes 1920x1080 (maximised), 1280x720, 900x700, 640x700, 360x640; variants `dark`,
`light` and `usertheme` (dark with the real `~/.config/gtk-4.0/gtk.css` and `settings.ini`,
read-only); extra modes at 1280x720 and 640x700: `rtl`, `scale` (text scaling 1.5), `long`
(80-character process names, very long GPU, disk and mount names), `empty` (no sensors, battery,
dGPU or ASUS firmware) and `ppt` (readable power limits). It writes `tests/ui/qa_report.md`
(findings grouped by kind with the combinations they occur in) and exits 1 on any failure.

Checks: GTK/Adwaita/GLib warnings on stderr (harness noise is allow-listed in `qa_core.py`);
labels ellipsized without a tooltip or clipped; widgets outside the window, their parent or a
scrolled viewport; the page's minimum width against the window (with the widest widget named);
unexpected horizontal scrollbars; overlapping siblings; zero-size and too small interactive
widgets (under 24 px fail, under 32 px warn); icon-only buttons without tooltip or accessible
name, and unlabelled switches, scales and radio buttons; text contrast on the rendered pixels
(WCAG 4.5:1, 3:1 for large text) at every scroll position; keyboard focus (focusable chain,
reading order of the Tab stops, a visible focus ring); header bar overlap; blank pages; dialog
bounds and centring; toast overlap with the profile chip; popovers drawn as one clear
surface; stale colours after a live dark to light switch (palette comparison with a fresh
light start). `tests/ui/test_qa_lint.py` and `test_ui_geometry.py` cover the pure logic
(contrast maths, log allowlist, geometry, number formatting, fan editor hit sizes and readout
placement) with no display and run with the normal suite.

Limits: the headless compositor never gives the window keyboard focus, and `GtkListBox` then
refuses to step off a row, so Tab cannot be driven; focus is checked from the widget tree
instead. `--real` adds one read-only pass against this machine's real telemetry (no writes, dialogs,
screenshots or label texts kept; automation, agent and daemon state go to a temp dir). HiDPI
(scale 2) is not run: a headless mutter has no scale option, and layout is identical in
logical pixels; only the Cairo-drawn widgets would differ and they draw in logical units.

