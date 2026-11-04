# Architecture

## Layers

1. `tuf_control.core`: pure data models (Profile, PowerLimit, FanCurve, GpuMode,
   BatteryHealth, Telemetry) and errors. Validation lives here; the firmware's own
   min/max are hard bounds.
2. `tuf_control.backends`: one `Backend` interface, several implementations:
   `dbus` (asusd, supergfxd, power-profiles-daemon via Gio, lazy import),
   `sysfs` (asus-armoury firmware attributes, platform_profile, LEDs, battery, hwmon),
   `cli` (asusctl wrapper, allow-listed arguments, no shell), `fake` (sysfs backend
   over a generated tree for tests).
3. `tuf_control.controller.Controller`: picks a backend per capability and reports
   `capabilities()` including why something is unavailable.
4. `tuf_control.cli`: argparse front end, JSON output, `--dry-run` on writes.
5. `tuf_control.daemon` (`tuf-controld`): per-user service running `tuf_control.automation`
   (rules) and `tuf_control.effects` (keyboard software effects). Both write only through the
   `Controller`; see [AUTOMATION.md](AUTOMATION.md) and [EFFECTS.md](EFFECTS.md).

6. `tuf_control.telemetry`: GTK-free system monitor sources (/proc, /sys, NVML via ctypes),
   one `Sampler`, preallocated ring buffers (`Store`), the process walker and a fake tree for
   development. The GUI drives it from one timer (`ui/live.py`); see
   [PERFORMANCE.md](PERFORMANCE.md).

## Backend order

D-Bus, then sysfs, then CLI. A capability is served by the first backend whose probe
reports it available. Reads fall through to the next backend if one fails; writes only
fall through on "unsupported/unavailable", never after a validation error.
When `TUF_CONTROL_FAKE_ROOT` is set only the fake sysfs backend is constructed, so
real hardware, D-Bus and asusctl are never touched.

## Privilege model

Reads need no privileges. Writes go through a `Writer`; the default writes directly
where sysfs permits (for example keyboard brightness for the `input` group) and raises
`PrivilegeRequired` otherwise. The optional polkit-guarded helper (`packaging/polkit/`)
provides a `Writer` for root-only attributes from an interactive GUI or CLI. The D-Bus
backend writes via asusd, which holds the privileges itself.

The shipped `tuf-controld` user unit keeps `NoNewPrivileges=yes`. It cannot invoke the
privileged sysfs fallback: those changes report `PrivilegeRequired` with an unavailable
reason. Background rules need asusd or power-profiles-daemon for protected changes;
attributes already writable by the user remain usable. Installing the helper does not
remove this daemon boundary.

## Time and threads

D-Bus calls use `call_sync` with an explicit timeout; `backends.pool.run_async` runs any
blocking call on a thread pool and returns a `concurrent.futures.Future`.
