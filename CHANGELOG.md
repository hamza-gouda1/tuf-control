# Changelog

All notable changes to this project. Format follows Keep a Changelog; versions follow SemVer.

## [Unreleased]

### Added
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
