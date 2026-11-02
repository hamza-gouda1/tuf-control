# Changelog

All notable changes to this project. Format follows Keep a Changelog; versions follow SemVer.

## [Unreleased]

### Added
- Touchpad & gestures: a Control page for the touchpad (on/off with the
  turn-off-when-a-mouse-is-connected option, tap to click, natural scrolling, speed slider
  with a 700 ms debounce, click method) and for the Quick Settings extension's touchpad
  gestures: a swipe or pinch completed on the stage runs the action it is bound to (overview,
  app grid, show desktop, window switch, workspace switch, launch an app) and every event is
  propagated, so GNOME keeps its own 3-finger gestures; bindings are read per gesture, so a
  change applies live. Gestures default to off (`gesture-enabled` false, every binding
  `none`); the extension (GNOME Shell 46, Wayland) must be installed and enabled for them to
  act, and the page says so when it is not.
- Updates: every apply is verified against the installed versions afterwards (dpkg-query,
  snapd, flatpak, fwupd) and reported per item — Installed, Installs on restart, Still
  pending with the reason, Failed with the backend's error; "Updates installed" is never
  claimed while versions did not move ("6 of 7 installed, 1 still pending" instead).
- Updates: kept-back packages say why in plain language (new packages a plain upgrade never
  installs, a dpkg hold, a held dependency, Ubuntu's phased rollout, a dependency conflict)
  with the exact terminal command; the fixable case gets an "Install with dependencies"
  action (PackageKit InstallPackages of the simulation's new packages) behind a confirmation
  that lists every package. Nothing is ever removed and held packages are never touched.
- Updates: a live progress panel while updates install — per service phase, progress bar,
  current package, speed and time remaining (PackageKit signals, snapd change polling, the
  fwupd download), a scrolling log, elapsed time, a waiting-for-authentication state and
  Cancel where the backend allows it. A worker thread feeds it at 4 fps; the window never
  freezes.
- Updates: a persistent history at `~/.local/state/tuf-control/updates-history.jsonl` (0600,
  rotated at 1 MiB) with per-item verified results; a History section on the page (source
  filter, since-yesterday summary), `tuf-control updates history [--since 7d] [--json]`, a
  read-only `get_update_history` agent tool, and "detected" entries for updates installed
  outside this app.
- Updates: the desktop notification from the background check now opens the app on the
  Updates page when clicked (a `default` notification action; `tuf-control-gui --page KEY`
  and a matching desktop action were added).
- Quick Settings: the extension hides GNOME's built-in Power Mode tile while it runs —
  our Power Mode tile switches the same platform profile through asusd (with
  power-profiles-daemon as fallback), so the shell's own one is redundant. Matched by
  the tile's GObject class name (GNOME 46's is PowerProfilesToggle), restored when the
  extension is disabled. A switch on the Touchpad & gestures page brings the system
  tile back; disabling the extension restores it.
- Touchpad gestures actually fire: Mutter 14 names the finger-count getter
  `get_touchpad_gesture_finger_count` (the engine called a getter that does not exist,
  so every gesture threw before it could run), and the swipe threshold now matches the
  hardware — measured on the FX608JHR, a natural three-finger swipe accumulates ~73 px,
  so the floor is 50 px with 1.4× axis dominance (was 120 px / 1.5×, which no real
  swipe reached). Gestures log to the journal (`[tuf-gesture]`) so a miss says why.
  "Show desktop" is a toggle: the app that was focused when everything minimised is
  remembered, and the next three-finger swipe down — on the desktop, or while the
  overview is up (the shell closes it, this restores the app) — restores the windows that were visible on that workspace.
  A three-finger swipe down in the overview only toggles when Show desktop did the
  minimising; otherwise the shell keeps its own gesture.
- `install.sh` installs and enables `tuf-controld` (the per-user automation and
  keyboard-effects daemon, previously not wired up: after a restart, software keyboard
  effects and automation rules did not come back and background update checks only ran
  while the app was open). `--no-daemon` skips it; `--uninstall` stops and removes it.
  `TUF_CONTROL_NO_SYSTEMD=1` installs the unit without touching the user manager (used
  by the test harness).
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
- Installer preserves original package, extension, icon, metadata and daemon files across
  repeated installs, restores previous service state, and retains the Updates desktop action.
- Gesture pinch recognition uses Clutter's cumulative scale; show-desktop restores the exact
  active-workspace window set and disconnects handlers when disabled.
- D-Bus backend, verified against asusd 6.3.5 by read-only introspection:
  `ResetProfileCurves` does not exist, the method is `SetCurvesToDefaults`;
  `SetFanCurve` takes `(u, (s, (yyyyyyyy), (yyyyyyyy), b))`, not byte arrays;
  aura `LedModeData` is `(uu(yyy)(yyy)ss)` with speed/direction as names, not integers.

## [0.1.0]
- Core models, D-Bus / sysfs / asusctl backends, controller, CLI and GTK 4 / Libadwaita UI.
