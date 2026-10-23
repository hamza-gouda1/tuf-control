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

## Backend order

D-Bus, then sysfs, then CLI. A capability is served by the first backend whose probe
reports it available. Reads fall through to the next backend if one fails; writes only
fall through on "unsupported/unavailable", never after a validation error.
When `TUF_CONTROL_FAKE_ROOT` is set only the fake sysfs backend is constructed, so
real hardware, D-Bus and asusctl are never touched.

## Privilege model

Reads need no privileges. Writes go through a `Writer`; the default writes directly
where sysfs permits (for example keyboard brightness for the `input` group) and raises
`PrivilegeRequired` otherwise. A later small polkit-guarded helper (`packaging/polkit/`)
will provide a `Writer` for root-only attributes. The D-Bus backend writes via asusd,
which holds the privileges itself.

## Time and threads

D-Bus calls use `call_sync` with an explicit timeout; `backends.pool.run_async` runs any
blocking call on a thread pool and returns a `concurrent.futures.Future`.
