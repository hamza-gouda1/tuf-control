# Touchpad & gestures

The **Touchpad & gestures** page (Control section) has two halves: the desktop's own
touchpad settings, and bindings that run an action when the Quick Settings extension sees a
touchpad swipe or pinch. The touchpad half works on any GNOME installation; the gesture
bindings need the extension installed and enabled ([INSTALL.md](INSTALL.md)).

## What is configurable

Touchpad settings are plain GSettings under `org.gnome.desktop.peripherals.touchpad`
(the schema ships with `gsettings-desktop-schemas`; `src/tuf_control/touchpad.py` wraps it
in `TouchpadSettings`):

| Control | Key | Values |
| --- | --- | --- |
| Touchpad (switch) | `send-events` | `enabled`, `disabled`, `disabled-on-external-mouse` |
| Turn off when a mouse is connected | `send-events` | sets `disabled-on-external-mouse` |
| Tap to click | `tap-to-click` | boolean |
| Natural scrolling | `natural-scroll` | boolean |
| Speed | `speed` | −1.0 to 1.0 slider, written 700 ms after the slider settles |
| Click method | `click-method` | the schema's own enum (default, none, areas, fingers on GNOME 46) |

The choices are read from the installed schema rather than hardcoded, so the click-method
dropdown follows whatever the desktop allows. If the schema cannot be read, the touchpad
rows are disabled and the page explains why instead.

Gesture bindings live in the extension's own schema
`org.gnome.shell.extensions.tuf-control-quick` (`extension/tuf-control-quick@hamza-gouda/
schemas/…gschema.xml`, wrapped by `GestureSettings` in `touchpad.py`). `gesture-enabled`
(default off) switches the whole thing, each of the seven gestures maps to one action, and
`gesture-launch-app` holds the desktop file id used by the launch-an-app action:

| Gesture | Setting | Default |
| --- | --- | --- |
| Three-finger swipe down | `gesture-swipe3-down` | `none` |
| Four-finger swipe up / down / left / right | `gesture-swipe4-up` / `-down` / `-left` / `-right` | `none` |
| Pinch in / pinch out | `gesture-pinch-in` / `gesture-pinch-out` | `none` |

**Show desktop is a toggle.** It minimises visible, minimisable windows on the active
workspace and restores exactly those windows on repeat. Previously minimised windows and
sticky windows stay as they were. Disabling the extension restores the surviving windows.
This optional gesture integration defaults off and remains experimental: synthetic events
prove recognition and lifecycle logic, while real touchpad delivery and competition with
GNOME's swipe tracker require a live-session check. For the polished fade and a reliable
keybinding fallback, use Focus Keys from bordeaux-gnome.

Every binding accepts one of: `none`, `overview`, `app-grid`, `show-desktop`,
`switch-windows`, `prev-workspace`, `next-workspace`, `launch-app`. The recognition
thresholds are fixed constants, not settings: a swipe must travel 50 logical px on its
dominant axis and carry 1.4× the other axis, a pinch must change its cumulative scale from 1.0 by 0.25
(`gesture_logic.js`, `SWIPE_DISTANCE`, `DOMINANCE`, `PINCH_SCALE`; 50/1.4 was measured
against the FX608JHR's touchpad, where a natural three-finger swipe accumulates ~73 px).
Completed gestures are logged once at the normal journal level under `[tuf-gesture]`.
Gesture starts, ends and rejected movements are emitted only at debug level; enable
GNOME Shell debug logging when diagnosing a binding (`journalctl -b 0 | grep tuf-gesture`).

## How it works

The extension connects to `captured-event` on the shell stage (`gestures.js`,
`GestureWatcher.enable()`) and watches only `TOUCHPAD_SWIPE` and `TOUCHPAD_PINCH` events.
Clutter delivers per-event motion for swipes and cumulative scale for pinches. The tracker
accumulates swipe motion and keeps the latest pinch scale, deciding only at `END`: BEGIN starts a gesture from zero, a mid-gesture finger change or
`CANCEL` drops it, and END answers with the gesture name — or nothing — and resets.

Every event returns `EVENT_PROPAGATE`. This avoids consuming the shell's own
3-finger events, but cannot guarantee ordering against the overview tracker in a live session. The one overlap is deliberate: a 3-finger swipe
down is bound only while the desktop is showing, because with the overview up the shell
itself uses that gesture to close it, and the watcher skips its own action there.

A completed gesture runs the action bound to it at that moment, read from GSettings as the
gesture finishes. Changing a binding in the page therefore applies to the next gesture with
no restart — as long as the extension is enabled, since the extension is the one reading
the settings. The actions, all in `shellActions()`:

| Action | What it does |
| --- | --- |
| `overview` | toggles the shell overview |
| `app-grid` | opens the app grid (the dash's show-apps behaviour) |
| `show-desktop` | toggles the visible windows on the active workspace, preserving pre-minimised windows |
| `switch-windows` | activates the next window across all workspaces, wrapping |
| `prev-workspace` / `next-workspace` | activates the neighbouring workspace |
| `launch-app` | launches the app from `gesture-launch-app`; nothing if it is empty, a notification if it is not installed |

The page writes both schemas directly (`touchpad.py`); GSettings reads and writes are
fast, so nothing goes through the background worker.

## What needs the extension installed

`install.sh` copies the extension to `~/.local/share/gnome-shell/extensions/` and compiles
its schema in place (`glib-compile-schemas`), so install.sh is what makes the bindings
reachable; then `gnome-extensions enable tuf-control-quick@hamza-gouda` (GNOME Shell 46,
Wayland). The page chains the extension's schema directory onto the default schema source,
so it can tell you which state you are in: the **Shell extension** row reads Installed or
Not installed, and without the extension the gesture rows are replaced by a note to run
install.sh and enable the extension. The touchpad half needs nothing beyond a normal GNOME
install.

## Testing

From the repository root:

    gjs -m tests/test_gestures.js                                # decision maths, schema, watcher
    PYTHONPATH=src python3 -m unittest tests.test_gestures_page  # the GSettings wrapper

Pinch reads `Clutter.Event.get_gesture_pinch_scale()` (1.0 at BEGIN), not motion deltas;
see the [Mutter API documentation](https://gnome.pages.gitlab.gnome.org/mutter/clutter/method.Event.get_gesture_pinch_scale.html).

The gjs test loads `gesture_logic.js` and `gestures.js` outside the shell — the watcher
runs against a fake stage and fake events, and the Clutter constants are checked against
the real typelib when `GI_TYPELIB_PATH` exposes it, otherwise mutter 46's verified values
stand in. The Python test compiles the extension's schema into a temporary directory and
runs `touchpad.py` under `GSETTINGS_BACKEND=keyfile`; the real dconf is never touched and
the page is never instantiated.

## Limitations (honest)

- GNOME binds the 3-finger up and horizontal swipes itself; only 3-finger down is ours, and
  the tracker rejects 3-finger swipes in the shell's own directions.
- Taps are not available as gestures: the touchpad reports taps as button clicks, and the
  watcher only sees swipe and pinch events.
- Bindings apply live only while the extension is enabled — it is the extension that reads
  them per gesture. With the extension off or not installed nothing listens, and after
  installing it on Wayland a log-out and log-in is needed before it can be enabled.
- Wayland only, GNOME Shell 46 only; X11 sessions and other shell versions are untested.
- Tested on one machine only (ASUS TUF F16 FX608JHR, Ubuntu 24.04).
