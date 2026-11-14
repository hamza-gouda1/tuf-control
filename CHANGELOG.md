# Changelog

All notable changes to this project. Format follows Keep a Changelog; versions follow SemVer.

## [Unreleased]

## [0.2.1] - 2026-11-14

### Added
- Brand assets: refreshed application icon, GNOME symbolic icon
  (`io.github.hamzagouda.Asusius-symbolic.svg`), and dark/light README hero headers.
- Upfront runtime dependency checks in `install.sh` (`python3 >= 3.12`, GTK 4,
  libadwaita, PangoCairo, `glib-compile-schemas`) with per-distro package install hints.
- Packaging and CI gates for desktop/AppStream validation, wheel/sdist build checks,
  and version synchronization across package metadata, AppStream, Arch `PKGBUILD`,
  and `asusius.__version__`.
- Optional `[qa]` extra in `pyproject.toml` (including `Pillow`) and Arch `asusius.install`
  post-install setup instructions.

### Changed
- **Safer keybind defaults**: `install.sh` no longer binds `XF86AudioMicMute` or the
  Copilot key unless `--with-mic-key` or `--with-copilot-key` is passed explicitly.
- GNOME Shell extension installation checks the active desktop environment, attempts
  `gnome-extensions enable` when already registered, and gives explicit Wayland re-login
  guidance instead of silently skipping missing schema compilers.
- Arch `PKGBUILD` moves `asusctl` to `optdepends` alongside `gst-plugins-base`,
  `gst-python`, `playerctl`, and `python-numpy`.
- Expanded GNOME Shell compatibility metadata and refreshed documentation for hardware
  compatibility, agent grants, and release verification.

### Fixed
- `asusius-gui` wrapper now executes `python3 -m asusius.ui "$@"` cleanly instead of
  running `-c` (fixing `sys.argv[0]` / process metadata).
- `install.sh` substitutes the chosen `--bin-dir` into `asusiusd.service`, excludes
  `.mimosa`/`__pycache__` when copying sources, guards `systemctl --user` in
  non-systemd/container environments, installs symbolic icons cleanly, and warns on
  leftover elevated helpers during `--uninstall`.
- `pyproject.toml` bundles UI SVG icon assets in package data; CI avoids PEP 668
  `--user` pip failures on Ubuntu 24.04 and handles fork PR checkouts gracefully.

## [0.2.0] - 2026-11-12

### Changed
- The touchpad gesture for play/pause media is now a four-finger tap; it replaces the
  hold-and-release gesture from 0.1.2.

### Added
- Media next/previous song actions are available as touchpad gestures.
- GNOME's Night Light tile is hidden while the Quick Settings extension runs, next to
  the Power Mode tile it already hides.

### Fixed
- The snap update check is resilient: a snapd failure no longer breaks the update flow.

### Rebrand — TUF Control is now Asusius
- Full rename of the project, binaries, service, extension and paths:
  `tuf-control`/`tuf-control-gui`/`tuf-controld`/`tuf-control-mcp` →
  `asusius`/`asusius-gui`/`asusiusd`/`asusius-mcp`;
  `tuf-controld.service` → `asusiusd.service`;
  `tuf-control-quick@hamza-gouda` → `asusius-quick@hamza-gouda`;
  `~/.local/share/tuf-control` → `~/.local/share/asusius`;
  `~/.local/state/tuf-control` → `~/.local/state/asusius`.
- Migration: the installer writes only the new names; TUF Control 0.1.x files are not
  removed automatically. Uninstall the old version first (`./install.sh --uninstall`
  from a 0.1.x checkout) or remove the old launchers in `~/.local/bin`, the old
  extension folder and `~/.config/systemd/user/tuf-controld.service` by hand. Data
  under `~/.local/state/tuf-control` is not migrated; copy it to
  `~/.local/state/asusius` to keep update history and backups.

## [0.1.2] - 2026-11-07

- Keep the MCP session open after malformed input and restrict approval polling to
  the server session that requested it.
- Use three fingers down to show the desktop and up to restore the previous window.
  An up swipe still opens GNOME Overview when there are no tucked windows.
- Tuck windows toward the bottom of their monitor and restore them along the same
  path. Both directions follow the touchpad until release, and switching workspaces
  clears the preview without pulling focus back to the previous workspace.
- Track finger movement directly in the desktop toggle and previous/next app previews.
  Restore window transforms before the overview opens, and add four-finger volume
  swipes and hold-and-release play/pause.


## [0.1.1] - 2026-11-04

### Fixed
- Bind approvals to server sessions and show launcher identity; protect important audit
  history from routine-call floods and validate sysfs writes against the opened attribute.
- Restore original keyboard lighting after crashes and refresh capabilities when services
  appear after startup. Protect thermal services and pause unreadable GPU telemetry.
- Retry failed update notifications, eliminate click-watch busy polling, bound package
  diagnostics and firmware transfers, and preserve complete firmware device identities.
- Confirm rule deletion and shared-service stops; improve fan-editor accessibility,
  startup-page diagnostics, background-check controls and process-column explanations.
- Generate shared app colours from Bordeaux tokens and keep state and token files private.
- Preserve shortcut backup and schema detection when settings listings are streamed.

### Added
- Full scheduled UI QA, source archive/checksum automation, pinned installation instructions,
  AppStream screenshots and fixture coverage for the primary package resolver.

## [0.1.0] - 2026-11-02

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
  swipe reached). Gestures log to the journal (`[asusius-gesture]`) so a miss says why.
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

- Core models, D-Bus / sysfs / asusctl backends, controller, CLI and GTK 4 / Libadwaita UI.
