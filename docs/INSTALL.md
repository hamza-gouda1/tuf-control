# Install

Requirements: Linux with an ASUS TUF/ROG laptop, Python 3.12+, PyGObject, GTK 4 and
Libadwaita 1.5 (for the app). For full control: `asusd` (asusctl 6.x) and `supergfxd`.
Nothing is downloaded by the installer.

## User install (no root)

    ./install.sh --dry-run     # see what would happen
    ./install.sh               # copies into ~/.local, adds launchers and the .desktop file
    asusius status         # ~/.local/bin must be on PATH
    asusius-gui

Files: `~/.local/share/asusius/`, `~/.local/bin/asusius{,-gui}`, desktop entry, icon,
metainfo, and the extension in `~/.local/share/gnome-shell/extensions/`. Files that already exist
and are not ours are backed up to `~/.local/state/asusius/backup/` and restored by
`./uninstall.sh` (or `./install.sh --uninstall`). Skip the extension with `--no-extension`. The per-user `asusiusd` service is installed
and started by default (`--no-daemon` skips it). Its original unit and enabled/active state
are restored on uninstall. `ASUSIUS_NO_SYSTEMD=1` installs files without contacting the
manager, for isolated tests. `XDG_CONFIG_HOME` is respected for the user unit.

## Quick Settings extension

    gnome-extensions enable asusius-quick@hamza-gouda

GNOME Shell 46 only. On Wayland a new extension appears after you log out and in. It talks to
`xyz.ljones.Asusd` and falls back to power-profiles-daemon. It never sets colours: it follows the
shell theme.

When the profile changes outside its menu, for example with **Fn+F5** (asusd cycles the platform
profile and announces it with `PropertiesChanged`) or from another app, it shows the new profile
(Quiet, Balanced or Performance) in an on-screen display like the volume keys do. The value it
reads at startup is not announced, and several presses within 300 ms show only the last one.

## App keys with Focus Keys (recommended)

A GNOME custom shortcut runs a command. On Wayland a command cannot hand the focus to an app that
is already running, so pressing the Armoury key with Asusius open only gives a "Asusius is
ready" notification. The `focus-keys@hamza-gouda` extension from
[bordeaux-gnome](https://github.com/hamza-gouda1/bordeaux-gnome) has the shell raise the window
instead (on any workspace), or start the app when it is not running.

When it is installed for your user **and enabled** (bordeaux-gnome's `./install.sh` does both),
`./install.sh` writes our keys into its shortcut list instead of making custom shortcuts:

| Key | Opens |
| --- | --- |
| XF86Launch3 (Armoury Crate key, or the `--hotkey` you chose) | Asusius |
| XF86Launch1 (the other Armoury key code, only with the default `--hotkey`) | Asusius |
| Copilot key, `<Shift><Super>XF86TouchpadOff` (`--copilot-key`) | Claude (`com.anthropic.Claude.desktop`) |

The mic key stays a custom shortcut (`playerctl play-pause` is a command, not an app). Entries you
already have are kept; a key used by one of your own custom shortcuts is skipped with a note. Our
entries are listed in `~/.local/state/asusius/backup/focus-keys.added` and `--uninstall`
removes exactly those (the list as it was before is in `focus-keys.orig`).

**Migration from an earlier install:** the custom shortcuts "Asusius" (`.../asusius/`),
"Claude" (`.../asusius-claude/`) and the XF86Launch1 shortcut that was retargeted from the
old "ASUS Armoury Control Center" are removed, so the extension is the only owner of those keys
(the shell gives each key to one grab). `takeover.tsv` is kept, so `--uninstall` still restores
your original "ASUS Armoury Control Center" shortcut and puts it back in the list.

Order: run bordeaux-gnome's `./install.sh` first, then this one; log out and back in once so the
extension starts. Without the extension (or with `--no-focus-keys`) everything below applies
unchanged; running `./install.sh` again after removing the extension brings the custom shortcuts
back.

## Armoury Crate key shortcut (without Focus Keys)

`./install.sh` also adds a GNOME custom shortcut named "Asusius" that runs
`~/.local/bin/asusius-gui` on **XF86Launch3**. The app has a fixed application id, so
pressing the key again does not start a second copy (on Wayland GNOME may only notify you that it
is ready; see Focus Keys above). Your other
custom shortcuts are kept: the entry `.../custom-keybindings/asusius/` is appended to the list,
the previous list is saved to `~/.local/state/asusius/backup/custom-keybindings.list`, and
`--uninstall` removes only our entry. If another custom shortcut already uses the key, ours is not
added. Options: `--no-hotkey` skips it, `--hotkey=XF86Launch1` picks another key.

Why XF86Launch3: the kernel's asus-nb-wmi keymap sends WMI code 0x38 ("Armoury Crate") as KEY_PROG3
(202) from the input device "Asus WMI hotkeys"; XKB maps evdev 202 to keycode 210, `<I210>`, which
`xkb/symbols/inet` defines as XF86Launch3. Check it on your machine: Settings > Keyboard > Keyboard
Shortcuts > add a custom shortcut and press the key (it shows the captured name), or run `xev` from
a terminal, keep its window focused and press the key (look for `keycode 210 ... XF86Launch3`).
If it shows something else (for example XF86Launch1, KEY_PROG1 from WMI 0x86/0x8A, or XF86Launch4),
run `./install.sh --hotkey=<that name>`. Keysyms already bound by GNOME itself are not used.

## Background service permissions

The user service keeps `NoNewPrivileges=yes`. Automation cannot elevate through the
polkit sysfs helper, even when it is installed. Use asusd or power-profiles-daemon for
protected background changes; otherwise they are unavailable with a permission reason.
The interactive GUI and CLI can use the optional helper below.

## Root-only writes (optional, polkit)

Most settings go through `asusd`, which already holds the privileges. Direct sysfs writes
(for example without `asusd`) may be refused with EACCES; an allow-listed helper can then be
used through `pkexec`. `./install.sh --system` prints the exact commands, it never runs them:

    sudo install -Dm0755 src/asusius/helper.py /usr/libexec/asusius-helper
    sudo install -Dm0644 packaging/polkit/io.github.hamzagouda.asusius.policy \
        /usr/share/polkit-1/actions/io.github.hamzagouda.asusius.policy

The helper accepts only `write <path> <value>` for: `platform_profile`, asus-armoury
`attributes/*/current_value` (checked against the attribute's own min/max/possible_values),
`charge_control_end_threshold` (20-100), `asus::kbd_backlight` brightness and `kbd_rgb_*`, and
`asus_custom_fan_curve` hwmon points. Paths are resolved with `realpath` and must stay under
`/sys`. Authorisation is `auth_admin_keep`.

## Uninstall

    ./uninstall.sh
    sudo rm -f /usr/libexec/asusius-helper /usr/share/polkit-1/actions/io.github.hamzagouda.asusius.policy
