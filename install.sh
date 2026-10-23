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

# GNOME custom keybinding for the dedicated Armoury Crate key (asus-nb-wmi code 0x38 ->
# KEY_PROG3 (202) -> XKB keycode 210 -> XF86Launch3). See docs/INSTALL.md.
KB_SCHEMA=org.gnome.settings-daemon.plugins.media-keys
KB_ITEM=$KB_SCHEMA.custom-keybinding
KB_BASE=/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings
HOTKEY=${TUF_CONTROL_HOTKEY:-XF86Launch3}
# Copilot key (Shift+Super+F23; XKB calls F23 XF86TouchpadOff) opens Claude, the mic key toggles music.
COPILOT_KEY=${TUF_CONTROL_COPILOT_KEY:-<Shift><Super>XF86TouchpadOff}
MIC_KEY=${TUF_CONTROL_MIC_KEY:-XF86AudioMicMute}

DRY=0 UNINSTALL=0 SYSTEM=0 WITH_EXT=1 WITH_HOTKEY=1 WITH_COPILOT=1 WITH_MIC=1

usage() {
    cat <<USAGE
Usage: ./install.sh [--dry-run] [--uninstall] [--system] [--no-extension]
                  [--no-hotkey] [--hotkey=KEYSYM]
                  [--no-copilot-key] [--copilot-key=ACCEL] [--no-mic-key] [--mic-key=KEYSYM]

  (default)       install for the current user under ~/.local (no root, no downloads)
  --dry-run       show what would happen, change nothing
  --uninstall     remove what this script installed (backups are restored)
  --system        also print the exact sudo commands for the polkit helper and policy
                  (they are NOT executed; run them yourself if you want root-only
                  writes through pkexec)
  --no-extension  skip the GNOME Shell Quick Settings extension
  --no-hotkey     do not add the GNOME shortcut that opens TUF Control from the
                  Armoury Crate key (a custom keybinding named "TUF Control", appended to
                  your existing custom shortcuts, removed again by --uninstall)
  --hotkey=KEYSYM use another key than XF86Launch3 for that shortcut. An existing custom
                  shortcut on XF86Launch1 that runs the retired asus-control app is
                  retargeted to TUF Control (old values saved, restored by --uninstall)
  --no-copilot-key  do not bind the Copilot key to the Claude desktop app
  --copilot-key=ACCEL  other key for that (default <Shift><Super>XF86TouchpadOff)
  --no-mic-key    do not bind the mic key to play/pause (this also leaves the
                  mic-mute settings of GNOME alone)
  --mic-key=KEYSYM  other key for play/pause (default XF86AudioMicMute)
USAGE
}

for arg in "$@"; do
    case $arg in
        --dry-run) DRY=1 ;;
        --uninstall) UNINSTALL=1 ;;
        --system) SYSTEM=1 ;;
        --no-extension) WITH_EXT=0 ;;
        --no-hotkey) WITH_HOTKEY=0 ;;
        --hotkey=*) HOTKEY=${arg#--hotkey=} ;;
        --no-copilot-key) WITH_COPILOT=0 ;;
        --copilot-key=*) COPILOT_KEY=${arg#--copilot-key=} ;;
        --no-mic-key) WITH_MIC=0 ;;
        --mic-key=*) MIC_KEY=${arg#--mic-key=} ;;
        -h | --help) usage; exit 0 ;;
        *) echo "unknown option: $arg" >&2; usage >&2; exit 2 ;;
    esac
done

for _v in "$HOTKEY" "$COPILOT_KEY" "$MIC_KEY"; do
    case $_v in
        *[!A-Za-z0-9_\<\>+-]* | "") echo "invalid key value: $_v" >&2; exit 2 ;;
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

# -- GNOME custom shortcuts -----------------------------------------------------------
# Current custom-keybindings list, one path per line (empty when unset).
kb_list() {
    local raw
    raw=$(gsettings get "$KB_SCHEMA" custom-keybindings 2>/dev/null) || return 1
    python3 -c 'import ast,sys
t=sys.argv[1]
t=t[t.index("["):] if "[" in t else "[]"
print("\n".join(ast.literal_eval(t)))' "$raw"
}

# Write the list given as arguments back as a GVariant string array.
kb_write() {
    local out="" p
    for p in "$@"; do out="$out${out:+, }'$p'"; done
    act gsettings set "$KB_SCHEMA" custom-keybindings "[$out]"
}

kb_available() {
    command -v gsettings >/dev/null 2>&1 || return 1
    gsettings list-keys "$KB_SCHEMA" >/dev/null 2>&1
}

# value of one key of a custom shortcut without the surrounding quotes
kb_get() { gsettings get "$KB_ITEM:$1" "$2" 2>/dev/null | sed "s/^'//; s/'\$//"; }

KB_CUR=() KB_NEW=()

# add_shortcut SLUG NAME COMMAND BINDING: appends one shortcut of ours unless another custom
# shortcut already uses the same key.
add_shortcut() {
    local slug=$1 name=$2 cmd=$3 bind=$4 p other path
    path="$KB_BASE/$slug/"
    for p in "${KB_CUR[@]}"; do
        [ -n "$p" ] && [ "$p" != "$path" ] || continue
        other=$(kb_get "$p" binding)
        if [ "${other#XF86}" = "${bind#XF86}" ]; then
            say "shortcut: $bind is already used by your custom shortcut $p, not adding \"$name\""
            return 0
        fi
    done
    say "shortcut $bind -> $cmd (GNOME shortcut \"$name\")"
    act gsettings set "$KB_ITEM:$path" name "$name"
    act gsettings set "$KB_ITEM:$path" command "$cmd"
    act gsettings set "$KB_ITEM:$path" binding "$bind"
    KB_NEW+=("$path")
}

# The owner's old shortcut on XF86Launch1 that opens the retired asus-control app is retargeted
# to TUF Control. Its old name/command/binding are kept in takeover.tsv for uninstall.
takeover_launch1() {
    local p n c tsv=$BACKUP_DIR/takeover.tsv
    for p in "${KB_CUR[@]}"; do
        [ -n "$p" ] || continue
        case $p in "$KB_BASE"/tuf-control*) continue ;; esac
        n=$(kb_get "$p" binding)
        [ "${n#XF86}" = Launch1 ] || continue
        n=$(kb_get "$p" name)
        c=$(kb_get "$p" command)
        case "$c|$n" in
            *asus-control*|*Armoury*) ;;
            *) say "shortcut: XF86Launch1 is used by \"$n\" ($c), left alone"; continue ;;
        esac
        if [ ! -e "$tsv" ] || ! grep -q "^$p	" "$tsv"; then
            act mkdir -p "$BACKUP_DIR"
            if [ "$DRY" = 1 ]; then
                say "+ save old shortcut \"$n\" to $tsv"
            else
                printf '%s\t%s\t%s\tXF86Launch1\n' "$p" "$n" "$c" >>"$tsv"
            fi
        fi
        say "shortcut XF86Launch1: retargeting \"$n\" -> \"TUF Control\" ($BIN_DIR/tuf-control-gui)"
        act gsettings set "$KB_ITEM:$p" name 'TUF Control'
        act gsettings set "$KB_ITEM:$p" command "$BIN_DIR/tuf-control-gui"
    done
}

# Stop GNOME from toggling the microphone on the mic key so it can pause/resume music instead.
claim_mic_key() {
    local k tsv=$BACKUP_DIR/media-keys.tsv v
    if [ ! -e "$tsv" ]; then
        act mkdir -p "$BACKUP_DIR"
        if [ "$DRY" = 1 ]; then
            say "+ save mic-mute / mic-mute-static to $tsv"
        else
            for k in mic-mute mic-mute-static; do
                gsettings list-keys "$KB_SCHEMA" | grep -qx "$k" || continue
                v=$(gsettings get "$KB_SCHEMA" "$k")
                printf '%s\t%s\n' "$k" "$v" >>"$tsv"
            done
        fi
    fi
    for k in mic-mute mic-mute-static; do
        gsettings list-keys "$KB_SCHEMA" | grep -qx "$k" || continue
        act gsettings set "$KB_SCHEMA" "$k" '[]'
    done
}

install_hotkey() {
    local raw p keep=()
    if ! kb_available; then
        say "shortcuts: gsettings or the GNOME media-keys schema is missing, skipping"
        return 0
    fi
    if ! raw=$(kb_list); then
        say "shortcuts: cannot read $KB_SCHEMA custom-keybindings, skipping"
        return 0
    fi
    mapfile -t KB_CUR <<<"$raw"
    if [ ! -e "$BACKUP_DIR/custom-keybindings.list" ]; then
        act mkdir -p "$BACKUP_DIR"
        if [ "$DRY" = 1 ]; then
            say "+ save current custom-keybindings list to $BACKUP_DIR/custom-keybindings.list"
        else
            printf '%s\n' "${KB_CUR[@]}" >"$BACKUP_DIR/custom-keybindings.list"
        fi
    fi
    add_shortcut tuf-control "TUF Control" "$BIN_DIR/tuf-control-gui" "$HOTKEY"
    if [ "$HOTKEY" = XF86Launch3 ]; then takeover_launch1; fi
    if [ "$WITH_COPILOT" = 1 ]; then
        if [ -e "$DATA_HOME/applications/com.anthropic.Claude.desktop" ] ||
           [ -e /usr/share/applications/com.anthropic.Claude.desktop ]; then
            add_shortcut tuf-control-claude "Claude" "gtk-launch com.anthropic.Claude" "$COPILOT_KEY"
        else
            say "shortcut: Claude desktop entry not found, skipping the Copilot key"
        fi
    fi
    if [ "$WITH_MIC" = 1 ]; then
        if command -v playerctl >/dev/null 2>&1; then
            add_shortcut tuf-control-playpause "Play/pause music" "playerctl play-pause" "$MIC_KEY"
            claim_mic_key
        else
            say "shortcut: playerctl not found, skipping the mic key"
        fi
    fi
    for p in "${KB_CUR[@]}" "${KB_NEW[@]}"; do
        [ -n "$p" ] || continue
        case " ${keep[*]:-} " in *" $p "*) continue ;; esac
        keep+=("$p")
    done
    if [ "${#KB_NEW[@]}" -gt 0 ]; then kb_write "${keep[@]}"; fi
}

uninstall_hotkey() {
    local cur=() keep=() p k v n c b found=0 raw
    kb_available || return 0
    raw=$(kb_list) || return 0
    mapfile -t cur <<<"$raw"
    for p in "${cur[@]}"; do
        [ -n "$p" ] || continue
        case $p in
            "$KB_BASE"/tuf-control*)
                found=1
                for k in name command binding; do act gsettings reset "$KB_ITEM:$p" "$k"; done ;;
            *) keep+=("$p") ;;
        esac
    done
    if [ "$found" = 1 ]; then
        say "removing our GNOME shortcuts (other custom shortcuts are untouched)"
        kb_write "${keep[@]}"
    fi
    if [ -e "$BACKUP_DIR/takeover.tsv" ]; then
        while IFS=$'\t' read -r p n c b; do
            [ -n "$p" ] || continue
            say "restoring your shortcut \"$n\""
            act gsettings set "$KB_ITEM:$p" name "$n"
            act gsettings set "$KB_ITEM:$p" command "$c"
            act gsettings set "$KB_ITEM:$p" binding "$b"
        done <"$BACKUP_DIR/takeover.tsv"
        act rm -f "$BACKUP_DIR/takeover.tsv"
    fi
    if [ -e "$BACKUP_DIR/media-keys.tsv" ]; then
        while IFS=$'\t' read -r k v; do
            [ -n "$k" ] || continue
            say "restoring $KB_SCHEMA $k"
            act gsettings set "$KB_SCHEMA" "$k" "$v"
        done <"$BACKUP_DIR/media-keys.tsv"
        act rm -f "$BACKUP_DIR/media-keys.tsv"
    fi
    act rm -f "$BACKUP_DIR/custom-keybindings.list"
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
    write_launcher "$BIN_DIR/tuf-control-mcp" \
        "PYTHONPATH=\"$APP_DIR\${PYTHONPATH:+:\$PYTHONPATH}\" exec python3 -m tuf_control mcp \"\$@\""
    write_launcher "$BIN_DIR/tuf-control-gui" \
        "PYTHONPATH=\"$APP_DIR\${PYTHONPATH:+:\$PYTHONPATH}\" exec python3 -c 'from tuf_control.ui.app import main; raise SystemExit(main())' \"\$@\""
    write_desktop
    act install -Dm0644 "$ROOT/data/icons/hicolor/scalable/apps/$APP_ID.svg" \
        "$DATA_HOME/icons/hicolor/scalable/apps/$APP_ID.svg"
    act install -Dm0644 "$ROOT/data/$APP_ID.metainfo.xml" "$DATA_HOME/metainfo/$APP_ID.metainfo.xml"
    if [ "$WITH_EXT" = 1 ]; then install_extension; fi
    refresh_caches
    if [ "$WITH_HOTKEY" = 1 ]; then install_hotkey; fi
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
    for f in "$BIN_DIR/tuf-control" "$BIN_DIR/tuf-control-gui" "$BIN_DIR/tuf-control-mcp" \
             "$DATA_HOME/applications/$APP_ID.desktop"; do
        if is_ours "$f"; then act rm -f "$f"; elif [ -e "$f" ]; then say "keeping $f (not ours)"; fi
        restore "$f"
    done
    act rm -f "$DATA_HOME/icons/hicolor/scalable/apps/$APP_ID.svg" \
        "$DATA_HOME/metainfo/$APP_ID.metainfo.xml"
    act rm -rf "$APP_DIR" "$EXT_DIR"
    refresh_caches
    uninstall_hotkey
    say "done. User settings (if any) were left alone."
}

if [ "$UNINSTALL" = 1 ]; then do_uninstall; else do_install; fi
if [ "$SYSTEM" = 1 ]; then print_system; fi
