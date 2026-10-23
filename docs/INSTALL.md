# Install

Requirements: Linux with an ASUS TUF/ROG laptop, Python 3.12+, PyGObject, GTK 4 and
Libadwaita 1.5 (for the app). For full control: `asusd` (asusctl 6.x) and `supergfxd`.
Nothing is downloaded by the installer.

## User install (no root)

    ./install.sh --dry-run     # see what would happen
    ./install.sh               # copies into ~/.local, adds launchers and the .desktop file
    tuf-control status         # ~/.local/bin must be on PATH
    tuf-control-gui

Files: `~/.local/share/tuf-control/`, `~/.local/bin/tuf-control{,-gui}`, desktop entry, icon,
metainfo, and the extension in `~/.local/share/gnome-shell/extensions/`. Files that already exist
and are not ours are backed up to `~/.local/state/tuf-control/backup/` and restored by
`./uninstall.sh` (or `./install.sh --uninstall`). Skip the extension with `--no-extension`.

## Quick Settings extension

    gnome-extensions enable tuf-control-quick@hamza-gouda

GNOME Shell 46 only. On Wayland a new extension appears after you log out and in. It talks to
`xyz.ljones.Asusd` and falls back to power-profiles-daemon. It never sets colours: it follows the
shell theme.

## Root-only writes (optional, polkit)

Most settings go through `asusd`, which already holds the privileges. Direct sysfs writes
(for example without `asusd`) may be refused with EACCES; an allow-listed helper can then be
used through `pkexec`. `./install.sh --system` prints the exact commands, it never runs them:

    sudo install -Dm0755 src/tuf_control/helper.py /usr/libexec/tuf-control-helper
    sudo install -Dm0644 packaging/polkit/io.github.hamzagouda.tuf-control.policy \
        /usr/share/polkit-1/actions/io.github.hamzagouda.tuf-control.policy

The helper accepts only `write <path> <value>` for: `platform_profile`, asus-armoury
`attributes/*/current_value` (checked against the attribute's own min/max/possible_values),
`charge_control_end_threshold` (20-100), `asus::kbd_backlight` brightness and `kbd_rgb_*`, and
`asus_custom_fan_curve` hwmon points. Paths are resolved with `realpath` and must stay under
`/sys`. Authorisation is `auth_admin_keep`.

## Uninstall

    ./uninstall.sh
    sudo rm -f /usr/libexec/tuf-control-helper /usr/share/polkit-1/actions/io.github.hamzagouda.tuf-control.policy
