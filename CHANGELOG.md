# Changelog

All notable changes to this project. Format follows Keep a Changelog; versions follow SemVer.

## [Unreleased]

### Added
- Quick Settings extension: an OSD names the new performance profile (Quiet / Balanced /
  Performance) when it changes outside the menu, e.g. Fn+F5 through asusd, with the
  power-profiles-daemon fallback too. Not for the startup value; bursts within 300 ms show only
  the last. `ProfileAnnouncer` in `profiles.js`, tested with a fake clock and enable/disable cycles.
- `install.sh`: when the Focus Keys extension (bordeaux-gnome) is installed and enabled, the
  Armoury keys (XF86Launch3, XF86Launch1) and the Copilot key go into its `bindings` (merged,
  recorded, removed again by `--uninstall`) so a press raises the open window instead of a
  "TUF Control is ready" notification. An earlier install is migrated: our TUF Control and
  Claude custom shortcuts and the retargeted XF86Launch1 one are removed; `takeover.tsv` is kept
  and `--uninstall` puts the original Armoury shortcut back in the list. `--no-focus-keys` keeps
  custom shortcuts. The mic key stays a custom shortcut.
- System monitor: `tuf_control.telemetry` (/proc, /sys, NVML via ctypes; one sampler, ring
  buffers), a new Dashboard with live cards, and Processes, Processor, Memory, NVIDIA graphics,
  Intel graphics, Fans & temps, Storage and Network pages. Pages build on first visit; Cairo
  renderer by default. 107 MB RSS / 1.2 % CPU idle on the Dashboard (docs/PERFORMANCE.md).
- `tests/hardware/validate_telemetry.py` (read-only cross-check against top, free, sensors,
  nvidia-smi, upower, sysfs) and `tests/ui/measure.py` (RSS and idle CPU, headless).
- `install.sh`: GNOME custom shortcut "TUF Control" (XF86Launch3, the Armoury Crate key) that opens
  or raises the app; appended to existing custom shortcuts, removed alone by `--uninstall`;
  `--no-hotkey`, `--hotkey=KEYSYM`.
- GNOME Shell 46 Quick Settings extension `tuf-control-quick@hamza-gouda`: Performance toggle
  with a Quiet / Balanced / Performance submenu, asusd with power-profiles-daemon fallback,
  GPU mode and charge limit readouts, "Open TUF Control".
- Polkit helper (`src/tuf_control/helper.py`, installed as `/usr/libexec/tuf-control-helper`) and
  policy `io.github.hamzagouda.tuf-control`: allow-listed sysfs writes only, validated against
  firmware bounds. The sysfs backend uses it only after a direct write fails with EACCES.
- `install.sh` / `uninstall.sh`: user-level install into `~/.local`, `--dry-run`, `--uninstall`,
  backups, `--system` prints (never runs) the sudo commands. `tests/test_install.sh`.
- README with comparison table, safety model and architecture; `docs/INSTALL.md`; CI workflow.

### Fixed
- D-Bus backend, verified against asusd 6.3.5 by read-only introspection:
  `ResetProfileCurves` does not exist, the method is `SetCurvesToDefaults`;
  `SetFanCurve` takes `(u, (s, (yyyyyyyy), (yyyyyyyy), b))`, not byte arrays;
  aura `LedModeData` is `(uu(yyy)(yyy)ss)` with speed/direction as names, not integers.

## [0.1.0]
- Core models, D-Bus / sysfs / asusctl backends, controller, CLI and GTK 4 / Libadwaita UI.
