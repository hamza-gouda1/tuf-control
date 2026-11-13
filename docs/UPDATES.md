# Updates and Services

## Updates

The **Updates** page (Control → Updates; the sidebar shows a count badge) lists everything on
the laptop that needs an update, grouped by source:

| Source | How it is read (no root, changes nothing) | How it is applied |
|---|---|---|
| System packages (apt: kernel, NVIDIA driver, everything else) | python-apt over the local package cache, like `apt list --upgradable`; fallback `apt-get -s -o Debug::NoLocking=1 dist-upgrade`, then `apt list --upgradable`. Shows when the package lists were last refreshed and marks packages apt holds back (phased). | PackageKit `UpdatePackages` (polkit) |
| Snaps | snapd REST on `/run/snapd.socket`: `/v2/snaps` + `/v2/find?select=refresh` | snapd `POST /v2/snaps` with `X-Allow-Interaction` (polkit) |
| Flatpak (system and per-user) | `flatpak remote-ls --updates --cached` (offline-safe) + `flatpak list` | `flatpak update --user` (no prompt) / `--system` (flatpak's helper, polkit) |
| Firmware (BIOS/UEFI, EC, Thunderbolt, webcam, Secure Boot dbx…) | fwupd D-Bus `GetDevices`, `GetUpgrades` per updatable device, `GetRemotes` for the LVFS metadata age | download the cabinet from the LVFS (https, checksum from the metadata), fwupd `Install` (fwupd verifies the signature; polkit) |
| Restart needed | newer installed kernel of the running flavour, `/run/reboot-required`, NVIDIA kernel module older than the installed driver | GNOME's own restart dialog (`org.gnome.SessionManager.Reboot`) |
| Asusius / Bordeaux GNOME checkouts (off by default) | `git rev-list HEAD..@{u}` as of the last fetch (no network) | not applied; pull yourself |

A missing source (no flatpak, no fwupd, no snapd) is shown under *Not available* with the reason;
a source that fails (offline, timeout) shows the error and the rest of the report is kept.

**Refresh** renews the metadata through the system services (PackageKit `RefreshCache`,
`fwupdmgr refresh`, flatpak remote summaries) and checks again. **Update all**, per-source
**Update** and per-item **Update** are all behind a confirmation that lists what will happen.
Firmware gets a strong warning (charger connected, battery above 30 %, do not interrupt) and is
never started on battery: the rest of an *Update all* goes ahead and firmware is skipped.
**Open in Software** opens `gnome-software --mode=updates`. The app never runs `sudo` or
`pkexec`; each system service asks polkit, which shows the desktop's own password dialog.

### What really installed (verified, not claimed)

After every apply the installed versions are read again (dpkg-query, snapd, flatpak, fwupd)
and each item is reported honestly: **Installed** (the version actually changed), **Installs
on restart** (fwupd staged firmware), **Still pending** — with the reason — or **Failed** —
with the backend's error. A backend returning success is not taken as proof: the toast and
the history say "6 of 7 installed, 1 still pending", never "Updates installed" when versions
did not move.

### Why a package is kept back

A package apt holds back is classified read-only (python-apt's own upgrade simulation, the
dpkg hold list, the Phased-Update-Percentage, PackageKit never consulted for this) and the
row says which case it is, in plain language, with the exact terminal command that would
take it:

- **Needs new packages** — a new kernel ABI means `linux-image-generic` needs
  `linux-image-6.8.0-147-generic` and friends that a plain upgrade never installs. The row
  offers **Install with dependencies** (PackageKit `InstallPackages` of exactly the new
  packages of the simulation) behind a confirmation that lists every package to install;
  nothing is ever removed and held packages are never touched.
- **Held by you / Blocked by a hold** — a dpkg hold (`apt-mark hold`), yours or one the
  update depends on. Listed, never applied; removing a hold stays a documented terminal
  command, never a button.
- **Phased rollout** — Ubuntu is releasing it gradually; it arrives on its own.
- **Would remove packages** — a dependency conflict; the app never removes anything.

### Live progress

While updates install, a panel shows each system service (packages, snaps, flatpak,
firmware) with its phase, a progress bar, the current package, download speed and time
remaining where the backend provides them (PackageKit PropertiesChanged and ItemProgress signals, snapd change
polling, the fwupd download), a scrolling log, the elapsed time, a clear *waiting for your
password* state while polkit is open, and **Cancel** where the backend allows it (PackageKit
while AllowCancel, snapd changes and the Flatpak child process; a firmware install is not cancellable once started). The
window never freezes: a worker thread does the work and the panel repaints at about 4 fps.

### History

Every run is appended to `~/.local/state/asusius/updates-history.jsonl` (mode 0600,
rotated at 1 MiB, with locked append/rotation, symlink refusal and malformed-record tolerance): start/end, trigger, duration, per item the old → new version and the
verified result with its reason, reboot flag. The **History** section of the page lists runs
newest first, expandable, with a source filter and a "since yesterday" summary; updates that
happened outside Asusius (unattended-upgrades, GNOME Software, a terminal) are noticed
by the next healthy background check and recorded only when installed-version changes
are proven. A withdrawn candidate or unavailable source is not counted as an installation.

### Background checks

`asusiusd` checks at most every 6 hours (configurable, minimum 6), skips metered
connections (NetworkManager `Metered`), runs the check in a short-lived child process (one at a
time per user, `flock`), caches the report in `~/.cache/asusius/updates.json`, and posts a
desktop notification such as *“4 updates available (apt 3, snap 1) · 2 security”* only when
something new appears (a new item or a newer version). Updates already announced, or seen on
the Updates page, never notify again; fewer updates never notify.

```json
"updates": {"enabled": true, "interval_h": 6, "respect_metered": true, "notify": true,
            "sources": {"apt": true, "snap": true, "flatpak": true, "firmware": true,
                        "system": true},
            "check_repos": false, "repos": ["~/projects/asusius", "~/projects/bordeaux-gnome"]}
```
(in `~/.config/asusius/config.json`).

### CLI and agents

```
asusius updates [list] [--check] [--json]   # cached report, or check now
asusius updates check
asusius updates refresh [--source apt|firmware|flatpak]
asusius updates apply [--source S] [--id ID]   # prints the plan; --yes applies (polkit),
                                                   # then re-checks and reports what really installed
asusius updates history [--since 7d] [--source S] [--limit N] [--json]
```
`updates apply --yes` verifies like the GUI: it prints the backend outcomes and then the
per-item verified results ("4 of 5 installed, 1 still pending" with the reason), and records
the run in the history. MCP agents get two read-only tools: `get_updates` (the cached check)
and `get_update_history` (past runs); neither checks nor installs.

## Services

**Monitor → Services** lists systemd units like Mission Center: name, state chip, enabled,
PID, memory, for **System** or **User** units, with a text filter (name, description, PID),
a type filter (services, timers, sockets, all) and a state filter (running, failed, stopped,
enabled). Selecting a unit shows its description, unit file, main PID and since-when, and
Start / Stop / Restart / Enable / Disable.

* Listing: one `ListUnits` D-Bus call per refresh (every 3 s, only while the page is visible;
  the rows are dropped when it is hidden), `ListUnitFiles` every tenth refresh, PID and memory
  from the unit's cgroup files. No root.
* Actions: systemd's own D-Bus API (`StartUnit`, `StopUnit`, `RestartUnit`,
  `EnableUnitFiles`/`DisableUnitFiles` + `Reload`); user units on the session bus, system units
  on the system bus with interactive authorisation, so systemd asks polkit. Every action has a
  confirmation.
* Safety: only services, sockets, timers and paths; masked/not-found units are refused;
  enable/disable only for `enabled`/`disabled` unit files; stop/restart/disable are refused for
  core units (D-Bus, `systemd-*`, polkit, NetworkManager and Wi-Fi, display managers, `user@`,
  gettys, the GNOME Shell/session/portals/PipeWire, asusd/supergfxd/power-profiles-daemon,
  AppArmor/ufw, and Asusius's own units). The CLI and the backend apply the same rules.

```
asusius services [--user] [list --filter X --state running|failed|... --type timer --sort memory]
asusius services show NAME
asusius services start|stop|restart|enable|disable NAME [--user] [--dry-run]
```

## Tests and screenshots

`tests/test_updates.py`, `tests/test_updates_live.py` and `tests/test_backend_audit.py` feed every parser recorded output
(`tests/fixtures/updates/`), test the notification dedupe, the scheduler cadence and its
"installed outside Asusius" detection, the kept-back classifier, the verifier (a backend
claiming success with unchanged versions is reported as still pending), the history
writer/reader/rotation and the end-to-end runner in fake mode; `tests/test_services.py`
covers the services safety rules and the D-Bus calls against recorders; a stand-in snapd
runs on a private socket. Actions are only exercised in fake mode (`ASUSIUS_FAKE_ROOT`),
where they are written to a log. Streaming/cancellation logic also runs against a harmless
Python child standing in for Flatpak; no real update backend is mutated. `tests/ui/screenshots_system.py` renders both pages (dark
and light) with fake data and drives an update-all and a service start through their
confirmations, capturing the live progress panel, the verified result with one still-pending
item, and the history section.

![Updates](screenshots/updates-dark.png) ![Services](screenshots/services-light.png)


## Transaction safety and verification limits

Dependency installation re-checks the apt plan after confirmation. A changed candidate,
new hold, removal, held dependency or broken resolver stops the run and asks for a refresh.
The confirmation lists every newly installed/upgraded package. PackageKit then simulates
InstallPackages; unexpected changes and unsupported/empty simulations are refused before
installation. No action removes a hold or applies package removals.

Installed versions come from dpkg-query, snapd's installed snap list (including revision),
Flatpak list/info (including commit), and fwupd. A changed display version alone is not
required for snap or Flatpak: a new revision/commit proves the update. If either before or
after cannot be read, the item remains unverified/pending. Firmware is shown as awaiting a
restart only when a successful apply and fwupd history confirm the offered release is
pending; a generic reboot-required bit is not proof. State values follow
[fwupd UpdateState](https://fwupd.github.io/libfwupd/enum.UpdateState.html). Firmware cabinets are limited to
256 MiB in memory; larger updates should use the system's firmware tool.

Real PackageKit/fwupd installs remain untested on this machine in this audit. Backend
signals, error cases, cancellation and verification are exercised using fake transactions
and local fixtures. Authentication delivery and real installations require an owner's
live-session test.
