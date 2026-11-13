# Touchpad & gestures

The **Touchpad & gestures** page (Control section) has two halves: the desktop's own
touchpad settings, and bindings that run an action when the Quick Settings extension sees a
touchpad swipe or pinch. The touchpad half works on any GNOME installation; the gesture
bindings need the extension installed and enabled ([INSTALL.md](INSTALL.md)).

## What is configurable

Touchpad settings are plain GSettings under `org.gnome.desktop.peripherals.touchpad`
(the schema ships with `gsettings-desktop-schemas`; `src/asusius/touchpad.py` wraps it
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

Gesture bindings live in the extension's `org.gnome.shell.extensions.asusius-quick`
schema. Enable gestures in **Touchpad & gestures**, then choose the actions you want.
The extension defaults to off, and the bindings default to None.

| Gesture | Setting | Action for this layout |
| --- | --- | --- |
| Three fingers down | `gesture-swipe3-down` | Show desktop |
| Three fingers up | Paired with Show desktop | Restore tucked windows, or open GNOME Overview |
| Three fingers left | `gesture-swipe3-left` | Next app |
| Three fingers right | `gesture-swipe3-right` | Previous app |
| Four fingers up or down | `gesture-swipe4-up`, `gesture-swipe4-down` | Volume up or down |
| Four fingers tapped | `gesture-hold4` | Play or pause |
| Four fingers left or right | `gesture-swipe4-left`, `gesture-swipe4-right` | Configurable |
| Pinch in or out | `gesture-pinch-in`, `gesture-pinch-out` | Configurable |

**Show desktop** tucks windows toward the bottom centre of each monitor as your
fingers move. An up swipe brings them back along the same path and refocuses the
previous window. A second down swipe keeps the desktop visible. An incomplete swipe
settles back to where it began. Only windows hidden by Show desktop are restored.
Switching workspaces resets the gesture and restores the previous workspace's windows
in the background. App switching previews the next or previous application on the
focused monitor and active workspace, with one window per app.

The extension uses a Clutter event filter to route configured swipes before GNOME's
workspace and overview handlers. With no tucked windows, three fingers up opens
GNOME Overview.
Once it opens, GNOME handles its own navigation. Entering it resets any gesture
transforms so the overview can lay out the previews using the original geometry.

The preview moves the existing compositor actors. Release uses a short ease-out
transition, and GNOME's reduced-motion setting makes the committed action immediate.
Volume changes in steps during travel through GNOME's media keys. A four-finger tap —
four fingers down and lifted again within 450 ms, without moving — triggers play/pause.
Moving into a swipe, changing the finger count, or resting past the window cancels it:
the window is timed by the extension, because Mutter reports the contact as a hold, not
a tap. Next and previous song ride the same media keys, so any player exposing media
keys (playerctl, GNOME's MPRIS handling) responds to them.

Other bindings include Overview, App grid, Switch window, Previous or Next workspace,
Launch an app and Next or Previous song. Use **App to launch** to choose the target
for the launch-app action.
The installed schema supplies the available choices, including when an older version
of the extension is present.

The page writes both schemas directly (`touchpad.py`); GSettings reads and writes are
fast, so nothing goes through the background worker.

## What needs the extension installed

`install.sh` copies the extension to `~/.local/share/gnome-shell/extensions/` and compiles
its schema in place (`glib-compile-schemas`), so install.sh is what makes the bindings
reachable; then `gnome-extensions enable asusius-quick@hamza-gouda` (GNOME Shell 46,
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

- A configured three-finger horizontal binding takes priority over GNOME's workspace
  swipe. Leave it set to None to keep GNOME's workspace switching in that direction.
- The playback binding is a four-finger tap the extension times itself: Mutter reports
  the contact as a hold, so the bound action fires when four fingers lift within 450 ms
  without movement, and a swipe or a rest past the window cancels it.
- Bindings apply live only while the extension is enabled — it is the extension that reads
  them per gesture. With the extension off or not installed nothing listens, and after
  installing it on Wayland a log-out and log-in is needed before it can be enabled.
- Wayland only, GNOME Shell 46 only; X11 sessions and other shell versions are untested.
- Tested on one machine only (ASUS TUF F16 FX608JHR, Ubuntu 24.04).
