# Keyboard software effects

Single-zone RGB effects computed by `tuf-controld` and written to the keyboard as a static
colour through `Controller.set_kbd_effect` (asusd `LedModeData` on real systems). The
hardware mode is saved when an effect starts and written back when effects stop, the daemon
stops, or a rule turns effects off.

![Effects section](screenshots/keyboard-effects-dark.png)

## Using them

Keyboard page, **Effects (software)**: pick an effect, adjust its parameters, watch the
preview, **Apply** (writes the config and starts the service if needed). From the CLI:

    tuf-control kbd effect list
    tuf-control kbd effect temperature --param source=cpu --param high=85
    tuf-control kbd effect audio --param band=bass --param peak=#a3283a --notifications on
    tuf-control kbd effect off

Rules can switch effects too (`{"type": "effect", "name": "audio"}`); a rule's choice
overrides the configured one while the rule is active, then the configured one returns.

## Effects

| name | colour from | parameters |
| --- | --- | --- |
| `temperature` | CPU/GPU temperature on a cool → warm → hot gradient (lagged ~1.5 s) | `source` cpu/gpu/max, `low`, `high`, `cool`, `warm`, `hot` |
| `audio` | bass/mid/treble envelope of what is playing | `band`, `base`, `peak`, `gain` |
| `ambient` | average colour of the screen | `boost` (saturation) |
| `profile` | platform profile → palette entry | `quiet`, `balanced`, `performance` |
| `accent` | one colour, static or breathing; optionally the Bordeaux theme accent | `colour`, `breathe`, `period`, `follow_theme` (off by default) |
| `notifications` (overlay) | flashes on desktop notifications above any effect | `colour`, `flashes`, `duration_ms`, `apps` |

`follow_theme` reads, read-only: `~/.config/bordeaux-gnome/variant`, then
`~/.local/state/bordeaux-gnome/variant` (written by the Bordeaux installer), then
`gsettings get org.gnome.shell.extensions.user-theme name`. Bordeaux `#8E2A44`,
Oxblood-Noir `#A3283A`, Merlot-Day `#7A1C34`; anything else falls back to `colour`.

## Engine

- **Plugin interface** (`effects/base.py`): a class with `name`, `title`, `params` (typed
  `Param` schema: colour, int, float, bool, choice, list, with bounds) and
  `tick(dt) -> (r, g, b) | None`. Effects see the world only through `Inputs`
  (telemetry, profile, audio levels, screen frames, notification queue, theme accent), so
  each is tested with synthetic inputs. `None` means "nothing to show".
- **Stack**: layers by priority; the highest layer with a colour wins (the notification
  flash at 100 over the chosen effect at 0). All layers tick every frame.
- **Smoothing**: exponential approach towards the target (`effects.smoothing_s`, 0.25 s),
  frame-rate independent.
- **Rate limit**: compute at `effects.fps` capped per effect (temperature/profile 8 fps,
  ambient 15, accent 20, audio 30, notification flash 30 while flashing); write at most
  `effects.max_write_hz` (default 10) and only when the colour moved by a noticeable amount
  (or every 5 s as a keep-alive). asusd stores each `LedModeData` change in its config, so
  the write rate is deliberately low.
- **Brightness-aware**: the backlight level is read every 2 s; at 0 no writes are sent.
  The colour itself is not scaled: the hardware brightness still applies on top.
- **Failures**: a failed write is reported in the status and retried with back-off
  (2 s, 4 s, ... up to 30 s).
- If a rule sets a static colour while an effect runs, that colour becomes what the effect
  restores when it stops.

## Inputs

### Audio
`parec --device=@DEFAULT_MONITOR@` (PulseAudio or pipewire-pulse; `pw-record -P
stream.capture.sink=true` as a fallback) at 11025 Hz mono, read on a thread. Analysis per
46 ms block: numpy `rfft` when numpy imports, else a pure-Python Goertzel over 12 probe
frequencies (measured here: 0.07 ms vs 0.24 ms per block, well under 1% of a core either
way). Each band has a beat envelope (20 ms attack, 250 ms release) normalised to a slowly
decaying running peak, so quiet and loud music both pulse. The capture process only runs
while the audio effect is active. The monitor of the default sink is what you hear; no
microphone is opened.

### Screen (ambient) — needs permission
GNOME on Wayland gives no silent screen access. The effect uses the
`org.freedesktop.portal.ScreenCast` portal: `CreateSession` → `SelectSources` (one monitor,
cursor hidden, `persist_mode=2`) → `Start` → `OpenPipeWireRemote`, then GStreamer
`pipewiresrc` scaled to 16×9 at 4 fps; dark pixels are ignored and saturation boosted.

- The **first start shows GNOME's screen-sharing dialog**; you pick the monitor. The portal
  returns a restore token, kept in `~/.config/tuf-control/screencast-token` (mode 600), so
  later starts skip the dialog until you revoke it. Cancelling the dialog clears the token
  and the effect reports `denied`.
- While the effect runs, **GNOME Shell shows its screen-sharing indicator** in the top bar.
  That is intended by GNOME and cannot be hidden.
- Tested against a fake portal bus and fake frame source (handshake order, options, token
  persistence and reuse, denial, averaging). The real dialog and PipeWire stream have not
  been exercised by the test suite.

### Notifications
A second, private session-bus connection calls
`org.freedesktop.DBus.Monitoring.BecomeMonitor` with one match rule for
`org.freedesktop.Notifications.Notify` calls. No eavesdropping flags, no notification server
of our own; the bus allows this for the same user, and a monitor connection cannot send.
Verified by a test on a private `dbus-run-session` bus. Where the bus refuses (sandboxed
sessions), the overlay stays idle and the status says why. Only app name, summary and
urgency are read.

## Status and caveats

- Not run on real hardware: the asusd `LedModeData` writes at ~10 Hz (the call shape is
  the one the hardware-effect UI already uses), real audio capture, the real portal dialog.
- While a software effect runs, the hardware-effect section of the Keyboard page shows the
  colour the effect last wrote; set effects to Off before changing the hardware effect.
- The in-app preview runs the same effect classes on simulated inputs (live telemetry and
  profile, a synthetic 120 bpm beat, a slow colour drift for the screen) and never touches
  the keyboard.
