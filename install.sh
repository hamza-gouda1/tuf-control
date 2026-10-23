#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
#
# User-level installer for tuf-control. Nothing is downloaded and no root is needed:
# the package is copied into ~/.local/share/tuf-control and started through small
# launchers in ~/.local/bin. Root-only pieces (polkit helper) are only PRINTED
# (--system), never executed. Safe to run repeatedly.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
DATA_HOME=${XDG_DATA_HOME:-$HOME/.local/share}
STATE_HOME=${XDG_STATE_HOME:-$HOME/.local/state}
BIN_DIR=${TUF_CONTROL_BIN_DIR:-$HOME/.local/bin}
APP_DIR=$DATA_HOME/tuf-control
BACKUP_DIR=$STATE_HOME/tuf-control/backup
APP_ID=io.github.hamzagouda.TufControl
EXT_UUID=tuf-control-quick@hamza-gouda
EXT_DIR=$DATA_HOME/gnome-shell/extensions/$EXT_UUID
MARK="managed by tuf-control install.sh"

DRY=0 UNINSTALL=0 SYSTEM=0 WITH_EXT=1

usage() {
    cat <<USAGE
Usage: ./install.sh [--dry-run] [--uninstall] [--system] [--no-extension]

  (default)       install for the current user under ~/.local (no root, no downloads)
  --dry-run       show what would happen, change nothing
  --uninstall     remove what this script installed (backups are restored)
  --system        also print the exact sudo commands for the polkit helper and policy
                  (they are NOT executed; run them yourself if you want root-only
                  writes through pkexec)
  --no-extension  skip the GNOME Shell Quick Settings extension
USAGE
}

for arg in "$@"; do
    case $arg in
        --dry-run) DRY=1 ;;
        --uninstall) UNINSTALL=1 ;;
        --system) SYSTEM=1 ;;
        --no-extension) WITH_EXT=0 ;;
        -h | --help) usage; exit 0 ;;
        *) echo "unknown option: $arg" >&2; usage >&2; exit 2 ;;
    esac
done

say() { printf '%s\n' "$*"; }
# run a command, or only show it under --dry-run
act() {
    if [ "$DRY" = 1 ]; then say "+ $*"; else "$@"; fi
}
is_ours() { [ -f "$1" ] && grep -q "$MARK" "$1" 2>/dev/null; }

# Keep the first copy of a file that is not ours before replacing it.
backup() {
    local f=$1 name
    [ -e "$f" ] || return 0
    is_ours "$f" && return 0
    name=$(printf '%s' "$f" | tr '/' '_')
    if [ -e "$BACKUP_DIR/$name" ]; then return 0; fi
    say "backing up $f -> $BACKUP_DIR/$name"
    act mkdir -p "$BACKUP_DIR"
    act cp -p "$f" "$BACKUP_DIR/$name"
}

restore() {
    local f=$1 name
    name=$(printf '%s' "$f" | tr '/' '_')
    if [ -e "$BACKUP_DIR/$name" ]; then
        say "restoring $f"
        act cp -p "$BACKUP_DIR/$name" "$f"
        act rm -f "$BACKUP_DIR/$name"
    fi
}

write_launcher() {
    local path=$1 body=$2
    backup "$path"
    say "launcher $path"
    if [ "$DRY" = 1 ]; then return 0; fi
    mkdir -p "$(dirname "$path")"
    printf '#!/bin/sh\n# %s\n%s\n' "$MARK" "$body" >"$path"
    chmod 0755 "$path"
}

write_desktop() {
    local src=$ROOT/data/$APP_ID.desktop dst=$DATA_HOME/applications/$APP_ID.desktop line
    backup "$dst"
    say "desktop entry $dst"
    if [ "$DRY" = 1 ]; then return 0; fi
    mkdir -p "$(dirname "$dst")"
    {
        printf '# %s\n' "$MARK"
        while IFS= read -r line; do
            case $line in
                Exec=*) printf 'Exec=%s\n' "$BIN_DIR/tuf-control-gui" ;;
                *) printf '%s\n' "$line" ;;
            esac
        done <"$src"
    } >"$dst"
}

refresh_caches() {
    if command -v gtk-update-icon-cache >/dev/null 2>&1 && [ "$DRY" = 0 ]; then
        gtk-update-icon-cache -q -t "$DATA_HOME/icons/hicolor" 2>/dev/null || true
    fi
    if command -v update-desktop-database >/dev/null 2>&1 && [ "$DRY" = 0 ]; then
        update-desktop-database -q "$DATA_HOME/applications" 2>/dev/null || true
    fi
}

install_extension() {
    local src=$ROOT/extension/$EXT_UUID
    say "extension $EXT_DIR"
    act rm -rf "$EXT_DIR"
    act mkdir -p "$EXT_DIR"
    act cp -r "$src/." "$EXT_DIR/"
    if [ -d "$EXT_DIR/schemas" ] && command -v glib-compile-schemas >/dev/null 2>&1; then
        act glib-compile-schemas "$EXT_DIR/schemas"
    fi
}

print_system() {
    cat <<SYS

Optional, root-only part (NOT executed by this script). It lets the app write
root-only sysfs attributes through pkexec when a direct write is refused. The helper
only accepts an allow-list of exact sysfs paths and validated values. Run:

  sudo install -Dm0755 "$ROOT/src/tuf_control/helper.py" /usr/libexec/tuf-control-helper
  sudo install -Dm0644 "$ROOT/packaging/polkit/io.github.hamzagouda.tuf-control.policy" /usr/share/polkit-1/actions/io.github.hamzagouda.tuf-control.policy

Remove again with:

  sudo rm -f /usr/libexec/tuf-control-helper /usr/share/polkit-1/actions/io.github.hamzagouda.tuf-control.policy
SYS
}

do_install() {
    command -v python3 >/dev/null 2>&1 || { echo "python3 is required" >&2; exit 1; }
    say "installing tuf-control for $USER ($([ "$DRY" = 1 ] && echo dry run || echo for real))"
    act rm -rf "$APP_DIR/tuf_control"
    act mkdir -p "$APP_DIR"
    act cp -r "$ROOT/src/tuf_control" "$APP_DIR/tuf_control"
    if [ "$DRY" = 0 ]; then
        find "$APP_DIR" -name __pycache__ -type d -prune -exec rm -rf {} +
    fi
    write_launcher "$BIN_DIR/tuf-control" \
        "PYTHONPATH=\"$APP_DIR\${PYTHONPATH:+:\$PYTHONPATH}\" exec python3 -m tuf_control \"\$@\""
    write_launcher "$BIN_DIR/tuf-control-gui" \
        "PYTHONPATH=\"$APP_DIR\${PYTHONPATH:+:\$PYTHONPATH}\" exec python3 -c 'from tuf_control.ui.app import main; raise SystemExit(main())' \"\$@\""
    write_desktop
    act install -Dm0644 "$ROOT/data/icons/hicolor/scalable/apps/$APP_ID.svg" \
        "$DATA_HOME/icons/hicolor/scalable/apps/$APP_ID.svg"
    act install -Dm0644 "$ROOT/data/$APP_ID.metainfo.xml" "$DATA_HOME/metainfo/$APP_ID.metainfo.xml"
    if [ "$WITH_EXT" = 1 ]; then install_extension; fi
    refresh_caches
    say
    say "done. Run: $BIN_DIR/tuf-control status   (make sure $BIN_DIR is on your PATH)"
    if [ "$WITH_EXT" = 1 ]; then
        say "Quick Settings extension copied; enable it yourself with:"
        say "  gnome-extensions enable $EXT_UUID   (log out and in if GNOME does not list it yet)"
    fi
}

do_uninstall() {
    say "removing tuf-control for $USER"
    local f
    for f in "$BIN_DIR/tuf-control" "$BIN_DIR/tuf-control-gui" \
             "$DATA_HOME/applications/$APP_ID.desktop"; do
        if is_ours "$f"; then act rm -f "$f"; elif [ -e "$f" ]; then say "keeping $f (not ours)"; fi
        restore "$f"
    done
    act rm -f "$DATA_HOME/icons/hicolor/scalable/apps/$APP_ID.svg" \
        "$DATA_HOME/metainfo/$APP_ID.metainfo.xml"
    act rm -rf "$APP_DIR" "$EXT_DIR"
    refresh_caches
    say "done. User settings (if any) were left alone."
}

if [ "$UNINSTALL" = 1 ]; then do_uninstall; else do_install; fi
if [ "$SYSTEM" = 1 ]; then print_system; fi
