# TUF Control

[![ci](https://github.com/hamza-gouda1/tuf-control/actions/workflows/ci.yml/badge.svg)](https://github.com/hamza-gouda1/tuf-control/actions/workflows/ci.yml)
![Python 3.12](https://img.shields.io/badge/python-3.12-3776ab)
![GTK 4 + Libadwaita](https://img.shields.io/badge/GTK%204-Libadwaita-4a86cf)
![License](https://img.shields.io/badge/license-GPL--3.0--or--later-blue)

A native control app for ASUS TUF and ROG laptops on Linux: performance profile, power
limits, fan curves, GPU mode, battery charge limit, keyboard lighting and display options,
in a GTK 4 / Libadwaita interface, a CLI, and a GNOME Quick Settings toggle.

<p align="center">
  <img src="docs/screenshots/dashboard-dark.png" alt="Dashboard" width="48%">
  <img src="docs/screenshots/performance-dark.png" alt="Performance" width="48%">
</p>

## Features

- **Performance**: Quiet / Balanced / Performance profile; PPT power limits when the
  firmware exposes them.
- **Fans**: 8-point curve per fan and profile (drag or arrow keys), apply / revert / factory reset.
- **Graphics**: Integrated / Hybrid / Ultimate through supergfxd, with the pending-restart state.
- **Battery**: charge limit 20-100 %, one-shot full charge, health.
- **Keyboard**: brightness, hardware effects and daemon-driven software effects with a live preview.
- **Display**: refresh rate (with Undo), panel overdrive.
- **Touchpad & gestures**: touchpad switches, speed and click method; swipe and pinch
  bindings that run through the Quick Settings extension ([GESTURES.md](docs/GESTURES.md)).
- **Monitor**: a dashboard of live cards and detail pages for processes/apps (with End task),
  per-core CPU, memory, both GPUs, both fans and temperatures, storage and network. An earlier single-machine run measured
  107 MB RSS on the dashboard; the ~210 MB Mission Center figure is an approximate reference,
  not a controlled comparison. Telemetry is checked against `top`, `free`, `sensors`,
  `nvidia-smi` and `upower` ([PERFORMANCE.md](docs/PERFORMANCE.md)). The dGPU is never woken
  to read sensors.
- **Updates**: checks apt, snap, Flatpak and firmware (plus a pending kernel, reboot-required and
  NVIDIA module match), applies them and verifies each item by comparing installed versions before
  and after.
- **Services**: start/stop/restart and enable/disable user and system services, with core units
  protected and systemd's own polkit prompt for system ones.
- **CLI**: `tuf-control status | profile | limit | battery ...`, `--json`, `--dry-run` on every write.
- **Agent access**: an MCP server (`tuf-control mcp`) and JSON CLI for agents and scripts.
  Read-only by default; every change is a dry run until you approve it. See below.
- **Quick Settings**: a "Performance" toggle with a profile submenu, GPU mode and charge limit
  readouts. Follows the shell theme.

<p align="center">
  <img src="docs/screenshots/fans-dark.png" alt="Fan curves" width="32%">
  <img src="docs/screenshots/gpu-dark.png" alt="Graphics" width="32%">
  <img src="docs/screenshots/battery-dark.png" alt="Battery" width="32%">
</p>

Light theme and narrow layouts are in [docs/screenshots](docs/screenshots/).

## Comparison

Armoury Crate and G-Helper are Windows programs; this table compares what each offers, not a
drop-in replacement. "Partial" means present but narrower than the original.

| | Armoury Crate (Windows) | G-Helper (Windows) | TUF Control (Linux) |
| --- | --- | --- | --- |
| Performance profiles | yes | yes | yes |
| PPT power limits | yes | yes | yes, if firmware exposes them (see limitations) |
| Fan curves | yes | yes | yes |
| GPU mode (Eco / Standard / Ultimate) | yes | yes | yes, via supergfxd |
| Charge limit | yes | yes | yes |
| Keyboard RGB effects | yes (Aura Sync) | yes | partial: single-zone hardware effects plus software effects run by tuf-controld; no per-key RGB |
| Automation | Scenario profiles (model dependent) | AC/battery profile, GPU and display rules | Rules for AC/battery, app launches, time windows and profile changes (run by the optional tuf-controld daemon) |
| Background operation | ASUS services; footprint not compared here | Single executable; footprint not compared here | Optional tuf-controld user service for rules, software effects and update checks; installed by default, skip with --no-daemon |
| Open source | no | yes | yes (GPL-3.0-or-later) |
| Runs on Linux | no | no | yes |

Comparison sources checked 2026-11-01: [G-Helper README](https://github.com/seerge/g-helper) and
[ASUS Armoury Crate documentation](https://www.asus.com/support/faq/1043747/).
Features and firmware support vary by model; this is not a full parity claim.

## Hardware compatibility

Only one model has been tested by the author. The other rows are what the design expects, not results.
Run `tuf-control capabilities --json` on your laptop and open a
[hardware report](https://github.com/hamza-gouda1/tuf-control/issues/new?template=hardware_report.yml)
so this table can be filled in with real results.

| Model | Status | Notes |
| :--- | :--- | :--- |
| TUF Gaming F16 FX608JHR | Tested | Power limits unavailable (missing kernel table entry); everything else works |
| Other TUF / ROG laptops with asusd | Untested | Expected to work where asusd and supergfxd expose the same D-Bus interfaces |
| Laptops without asusd | Not supported | The app needs asusd for profiles, fans and keyboard lighting |

## Limitations

- **Power limits require support for the model in the kernel driver.** On the tested FX608JHR,
  PPT attributes read ENODEV and asusd reports -1, consistent with a missing power-limit table
  entry. Sliders remain hidden when real bounds are unavailable. `EnablePptGroup` is a separate
  per-profile tuning switch; enabling it cannot supply missing kernel bounds.
  The draft upstream request for the FX608JHR is in
  [docs/upstream/FX608JHR-power-limits.md](docs/upstream/FX608JHR-power-limits.md).
- Fan curves exist only for fans the firmware lists; through plain sysfs only the active profile's
  curve can be edited.
- Keyboard: per-key RGB is not supported. Hardware effects are single-zone, and the software effects
  work by writing one colour to the whole keyboard a few times a second (tuf-controld).
- Keyboard (TUF): hardware Breathe shows only one colour (asusd keeps Colour 2, but it never reaches
  the keys), so the app's "Two colours" switch drives the software two-colour gradient
  (Gradient breathing) instead.
- GPU mode changes need a log-out or reboot; supergfxd decides, the app only shows the required action.
- The D-Bus names and signatures were checked read-only against asusd 6.3.5 and supergfxd; the write
  paths have been tested with a recording transport and a fake sysfs tree, not exercised on every
  model. Daemons were introspected on one TUF laptop only.
- Tested on one machine only: ASUS TUF F16 FX608JHR on Ubuntu 24.04, GNOME Wayland; other models and
  X11 sessions are untested.
- Real PackageKit, snap, Flatpak and fwupd installations have not been exercised in this
  development; tests simulate transactions and verify installed-version results. Firmware
  pending a restart is reported separately from a confirmed installed version.
- The GUI and Shell integration target Wayland; X11 is untested.
- GNOME Shell 46 is the only version the Quick Settings extension declares.

## Install

    git clone --branch v0.1.1 --depth 1 https://github.com/hamza-gouda1/tuf-control.git
    cd tuf-control
    ./install.sh --dry-run
    ./install.sh
    gnome-extensions enable tuf-control-quick@hamza-gouda

Install instructions pin the tested release tag. Contributors can clone `main` with
`git clone --branch main https://github.com/hamza-gouda1/tuf-control.git`; see [CONTRIBUTING.md](CONTRIBUTING.md).

See [docs/INSTALL.md](docs/INSTALL.md). User-level, no root, no downloads. The optional polkit
helper is printed with `./install.sh --system` and never run for you.

The installer also binds the Armoury Crate key (XF86Launch3) to TUF Control, takes over an existing
XF86Launch1 shortcut only when it still opens the retired asus-control app, and binds the Copilot
key to Claude when that app is installed. With the Focus Keys extension from bordeaux-gnome
installed and enabled,
those go into its shortcut list, so a press brings the open window to the front instead of a
"TUF Control is ready" notification, and earlier custom shortcuts for them are migrated away;
without it they are GNOME custom shortcuts. Skip them with `--no-hotkey`, pick another key with
`--hotkey=KEYSYM`. The Quick Settings extension also shows the new profile in an OSD when Fn+F5
changes it. Details in [docs/INSTALL.md](docs/INSTALL.md).

## Usage

    tuf-control status
    tuf-control profile set quiet --dry-run
    tuf-control battery limit 80
    tuf-control-gui

Development without hardware: `TUF_CONTROL_FAKE_ROOT=/tmp/tree tuf-control status` (see
[docs/UI.md](docs/UI.md)).

## Agent access

Agents use the app through a protocol instead of a window. `tuf-control mcp` is a Model
Context Protocol server (JSON-RPC over stdio, standard library only) with read tools
(status, telemetry, processes, fan curves, battery health, automation rules, "why is this
unavailable"), write tools for profile, charge limit and keyboard, and high-risk tools for
fan curves, GPU mode, power limits and restoring defaults.

    tuf-control mcp --print-config          # client config snippet
    tuf-control agent grant control-basic   # allow change requests (each one approved by you)
    tuf-control agent pending | approve ID | deny ID | audit | revoke

- Read-only by default; only you raise the level (Agent access page or `agent grant`).
- Writes are dry runs unless `dry_run: false` **and** you approved: once per call, or with a
  time-boxed grant (`--for 1h`) for the basic tools. High-risk tools are off until you enable
  them and always ask per call.
- Hardware bounds enforced through the same Controller, rate limits, strict schemas with no
  path or command parameters, untrusted process names cleaned and marked, an audit log,
  and a kill switch (`tuf-control agent revoke`, **Revoke all** on the page).

<p align="center">
  <img src="docs/screenshots/agent-access-dark.png" alt="Agent access" width="60%">
</p>

Tools, safety model, threat model and an example session: [docs/AGENT_API.md](docs/AGENT_API.md).

## Architecture

```
 GTK app  |  CLI  |  GNOME Quick Settings extension (talks to the bus itself)
    \        |
     Controller  -- picks a backend per capability, reports why something is unavailable
        |
   +----+-----------+-----------+
 D-Bus (asusd,     sysfs        asusctl CLI
 supergfxd, PPD)   (+ writer)   (allow-listed args)
                      |
              direct write -> EACCES -> pkexec tuf-control-helper (allow-listed)
```

Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/UI.md](docs/UI.md).

## Safety model

- Every write is validated against the firmware's own min/max before it is sent; dry runs never write.
- Confirmation dialogs for power limits, fan curves and GPU mode.
- The app runs as you. Privileged work is done by `asusd`, or by a tiny root helper that only
  accepts exact sysfs paths and values, resolves symlinks, uses no shell, and sits behind a
  polkit prompt (`auth_admin_keep`). It is optional and installed by you.
- The CLI wrapper uses an argument allow-list and no shell. Tests run on a fake tree and never touch hardware.

## Development

    PYTHONPATH=src python3 -m unittest discover -s tests
    shellcheck install.sh uninstall.sh tests/test_install.sh && bash tests/test_install.sh
    gjs -m tests/test_quick_profiles.js
    gjs -m tests/test_gestures.js

## Credits and license

Written by Hamza Gouda. Built on the work of the asus-linux project (asusd, supergfxd) and the
kernel's asus-wmi and asus-armoury drivers; this project is not affiliated with ASUS.
Version 3 or, at your option, any later version (SPDX: GPL-3.0-or-later). See [LICENSE](LICENSE).
