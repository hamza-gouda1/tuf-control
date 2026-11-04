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
CONFIG_HOME=${XDG_CONFIG_HOME:-$HOME/.config}
DAEMON_UNIT=$CONFIG_HOME/systemd/user/tuf-controld.service
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
# The Focus Keys GNOME Shell extension (from bordeaux-gnome) raises an app's open window from
# a shortcut; a custom shortcut running a launcher only gets an "is ready" notification on
# Wayland. When it is installed and enabled, the app keys are written into its `bindings`.
FOCUS_UUID=focus-keys@hamza-gouda
FOCUS_SCHEMA=org.gnome.shell.extensions.focus-keys
FOCUS_SCHEMAS=$DATA_HOME/gnome-shell/extensions/$FOCUS_UUID/schemas
CLAUDE_ID=com.anthropic.Claude

DRY=0 UNINSTALL=0 SYSTEM=0 WITH_EXT=1 WITH_HOTKEY=1 WITH_COPILOT=1 WITH_MIC=1 WITH_FOCUS=1 \
WITH_DAEMON=1

usage() {
    cat <<USAGE
Usage: ./install.sh [--dry-run] [--uninstall] [--system] [--no-extension]
                  [--no-hotkey] [--hotkey=KEYSYM]
                  [--no-copilot-key] [--copilot-key=ACCEL] [--no-mic-key] [--mic-key=KEYSYM]
                  [--no-focus-keys] [--no-daemon]

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
  --no-focus-keys use GNOME custom shortcuts for the app keys even when the Focus Keys
                  extension (bordeaux-gnome) is installed. With it, the Armoury keys
                  (XF86Launch3 and XF86Launch1) and the Copilot key go into its shortcut
                  list instead, so a second press brings the open window to the front;
                  our earlier custom shortcuts for them are removed
  --no-daemon     do not install or start tuf-controld (the per-user automation and
                  keyboard-effects service). Without it, software keyboard effects and
                  rules run only while you start the daemon yourself, and they do not
                  come back after a restart
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
        --no-focus-keys) WITH_FOCUS=0 ;;
        --no-daemon) WITH_DAEMON=0 ;;
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

# Snapshot each replaced asset once, including an absent destination. Reinstalling
# must not overwrite the original; uninstall without a record must leave it alone.
snapshot_asset() {
    local path=$2 slot=$BACKUP_DIR/assets/$1
    [ ! -e "$slot/managed" ] || return 0
    say "saving original $path"
    [ "$DRY" = 0 ] || return 0
    mkdir -p "$slot"
    chmod 0700 "$BACKUP_DIR" "$BACKUP_DIR/assets" "$slot"
    if [ -e "$path" ] || [ -L "$path" ]; then cp -a "$path" "$slot/original"; fi
    touch "$slot/managed"
}

restore_asset() {
    local path=$2 slot=$BACKUP_DIR/assets/$1
    [ -e "$slot/managed" ] || return 0
    say "restoring original $path"
    [ "$DRY" = 0 ] || return 0
    rm -rf -- "$path"
    if [ -e "$slot/original" ] || [ -L "$slot/original" ]; then
        mkdir -p "$(dirname "$path")"
        cp -a "$slot/original" "$path"
    fi
    rm -rf -- "$slot"
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
    local src=$ROOT/data/$APP_ID.desktop dst=$DATA_HOME/applications/$APP_ID.desktop
    backup "$dst"
    say "desktop entry $dst"
    [ "$DRY" = 0 ] || return 0
    mkdir -p "$(dirname "$dst")"
    python3 - "$src" "$dst" "$BIN_DIR/tuf-control-gui" "$MARK" <<'PYTHON'
from pathlib import Path
import sys
src, dst, launcher, marker = sys.argv[1:]
# Desktop Entry string escaping precedes Exec quoting. Percent is a field code.
quoted = launcher.replace('\\', '\\\\').replace('"', '\\"')
quoted = quoted.replace('`', '\\`').replace('$', '\\$').replace('%', '%%')
quoted = ('"' + quoted + '"').replace('\\', '\\\\')
lines = []
for line in Path(src).read_text().splitlines():
    if line.startswith('Exec=tuf-control-gui'):
        line = 'Exec=' + quoted + line[len('Exec=tuf-control-gui'):]
    lines.append(line)
Path(dst).write_text('# ' + marker + '\n' + '\n'.join(lines) + '\n')
PYTHON
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
    snapshot_asset extension "$EXT_DIR"
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
# to TUF Control (MODE retarget) or, when Focus Keys takes the key, removed (MODE remove: the
# path goes to MIGRATE). Its old name/command/binding are kept in takeover.tsv for uninstall.
takeover_launch1() {
    local mode=${1:-retarget} p n c b tsv=$BACKUP_DIR/takeover.tsv
    # Taken over earlier and since removed by a Focus Keys install: bring it back as TUF Control.
    if [ "$mode" = retarget ] && [ -e "$tsv" ]; then
        while IFS=$'\t' read -r p n c b; do
            [ -n "$p" ] || continue
            case " ${KB_CUR[*]} " in *" $p "*) continue ;; esac
            say "shortcut $b: re-adding \"TUF Control\" ($BIN_DIR/tuf-control-gui)"
            act gsettings set "$KB_ITEM:$p" name 'TUF Control'
            act gsettings set "$KB_ITEM:$p" command "$BIN_DIR/tuf-control-gui"
            act gsettings set "$KB_ITEM:$p" binding "$b"
            KB_NEW+=("$p")
        done <"$tsv"
    fi
    for p in "${KB_CUR[@]}"; do
        [ -n "$p" ] || continue
        case $p in "$KB_BASE"/tuf-control*) continue ;; esac
        n=$(kb_get "$p" binding)
        [ "${n#XF86}" = Launch1 ] || continue
        n=$(kb_get "$p" name)
        c=$(kb_get "$p" command)
        if [ -e "$tsv" ] && grep -q "^$p	" "$tsv"; then
            : # ours since an earlier run (retargeted to TUF Control)
        else
            case "$c|$n" in
                *asus-control*|*Armoury*) ;;
                *) say "shortcut: XF86Launch1 is used by \"$n\" ($c), left alone"; continue ;;
            esac
        fi
        if [ ! -e "$tsv" ] || ! grep -q "^$p	" "$tsv"; then
            act mkdir -p "$BACKUP_DIR"
            if [ "$DRY" = 1 ]; then
                say "+ save old shortcut \"$n\" to $tsv"
            else
                printf '%s\t%s\t%s\tXF86Launch1\n' "$p" "$n" "$c" >>"$tsv"
            fi
        fi
        if [ "$mode" = remove ]; then
            MIGRATE+=("$p")
            continue
        fi
        say "shortcut XF86Launch1: retargeting \"$n\" -> \"TUF Control\" ($BIN_DIR/tuf-control-gui)"
        act gsettings set "$KB_ITEM:$p" name 'TUF Control'
        act gsettings set "$KB_ITEM:$p" command "$BIN_DIR/tuf-control-gui"
    done
}

# -- Focus Keys extension ---------------------------------------------------------------
MIGRATE=()

focus_gs() { GSETTINGS_SCHEMA_DIR="$FOCUS_SCHEMAS${GSETTINGS_SCHEMA_DIR:+:$GSETTINGS_SCHEMA_DIR}" gsettings "$@"; }
focus_schema() {
    [ -f "$FOCUS_SCHEMAS/gschemas.compiled" ] &&
        focus_gs list-keys "$FOCUS_SCHEMA" 2>/dev/null | grep -x bindings >/dev/null
}

# True when the extension is installed for this user (with its schema) and enabled.
focus_available() {
    [ "$WITH_FOCUS" = 1 ] && focus_schema || return 1
    case $(gsettings get org.gnome.shell enabled-extensions 2>/dev/null) in
        *"'$FOCUS_UUID'"*) return 0 ;;
    esac
    say "shortcuts: Focus Keys is installed but not enabled, using custom shortcuts (enable it, then run this again)"
    return 1
}

claude_installed() {
    [ -e "$DATA_HOME/applications/$CLAUDE_ID.desktop" ] || [ -e "/usr/share/applications/$CLAUDE_ID.desktop" ]
}

# focus_merge CURRENT IGNORED ENTRY...: CURRENT is the bindings value (GVariant text), IGNORED
# the custom-shortcut paths (one per line) that are being removed. Prints the merged list, then
# "add|have|skip<TAB>entry[<TAB>why]" per entry. A key that already has an entry (the user's own
# choice) or is used by another GNOME custom shortcut is skipped.
focus_merge() {
    python3 - "$@" <<'PY'
import re
import sys

from gi.repository import Gio, GLib

ALIAS = {"ctrl": "control", "ctl": "control", "primary": "control", "mod1": "alt", "mod4": "super"}


def norm(a):
    mods, rest = set(), a.strip()
    while (m := re.match(r"<([^>]+)>", rest)):
        n = m.group(1).lower()
        mods.add(ALIAS.get(n, n))
        rest = rest[m.end():]
    k = rest.strip().lower()
    return "".join(f"<{x}>" for x in sorted(mods)) + (k[4:] if k.startswith("xf86") else k)


def custom(ignored):
    mk = "org.gnome.settings-daemon.plugins.media-keys"
    if not Gio.SettingsSchemaSource.get_default().lookup(mk, True):
        return {}
    out = {}
    for p in Gio.Settings.new(mk).get_strv("custom-keybindings"):
        if p in ignored:
            continue
        c = Gio.Settings.new_with_path(mk + ".custom-keybinding", p)
        if c.get_string("binding"):
            out[norm(c.get_string("binding"))] = c.get_string("name")
    return out


cur = list(GLib.Variant.parse(GLib.VariantType("as"), sys.argv[1]).unpack())
have = {norm(e.rpartition("=")[0]) for e in cur if "=" in e}
taken = custom(set(sys.argv[2].split("\n")))
lines = []
for e in sys.argv[3:]:
    k = norm(e.rpartition("=")[0])
    if e in cur:
        lines.append(f"have\t{e}")
    elif k in have:
        lines.append(f"skip\t{e}\tthat key already has a Focus Keys shortcut")
    elif k in taken:
        lines.append(f"skip\t{e}\tthe GNOME custom shortcut \"{taken[k]}\" uses that key")
    else:
        cur.append(e)
        have.add(k)
        lines.append(f"add\t{e}")
print(GLib.Variant("as", cur).print_(False))
print("\n".join(lines))
PY
}

# Our earlier custom shortcuts for the app keys; Focus Keys replaces them.
collect_migrate() {
    local p
    for p in "${KB_CUR[@]}"; do
        case $p in "$KB_BASE/tuf-control/" | "$KB_BASE/tuf-control-claude/") MIGRATE+=("$p") ;; esac
    done
    if [ "$HOTKEY" = XF86Launch3 ]; then takeover_launch1 remove; fi
}

install_focus_bindings() {
    local entries=("$HOTKEY=$APP_ID.desktop") cur merged new verb entry why added=$BACKUP_DIR/focus-keys.added
    if [ "$HOTKEY" = XF86Launch3 ]; then entries+=("XF86Launch1=$APP_ID.desktop"); fi
    if [ "$WITH_COPILOT" = 1 ]; then
        if claude_installed; then
            entries+=("$COPILOT_KEY=$CLAUDE_ID.desktop")
        else
            say "shortcut: Claude desktop entry not found, skipping the Copilot key"
        fi
    fi
    cur=$(focus_gs get "$FOCUS_SCHEMA" bindings)
    if [ ! -e "$BACKUP_DIR/focus-keys.orig" ]; then
        act mkdir -p "$BACKUP_DIR"
        if [ "$DRY" = 1 ]; then
            say "+ save the Focus Keys bindings to $BACKUP_DIR/focus-keys.orig"
        else
            printf '%s\n' "$cur" >"$BACKUP_DIR/focus-keys.orig"
        fi
    fi
    merged=$(focus_merge "$cur" "$(printf '%s\n' "${MIGRATE[@]}")" "${entries[@]}")
    new=$(head -1 <<<"$merged")
    while IFS=$'\t' read -r verb entry why; do
        case $verb in
            add | have)
                [ "$verb" = have ] || say "Focus Keys: ${entry%=*} -> ${entry##*=} (brings the open window to the front)"
                if [ "$DRY" = 0 ] && ! grep -qxF "$entry" "$added" 2>/dev/null; then
                    printf '%s\n' "$entry" >>"$added"
                fi ;;
            skip) say "Focus Keys: not adding $entry: $why" ;;
        esac
    done < <(sed 1d <<<"$merged")
    if [ "$new" != "$cur" ]; then
        if [ "$DRY" = 1 ]; then say "+ gsettings set $FOCUS_SCHEMA bindings $new"; else focus_gs set "$FOCUS_SCHEMA" bindings "$new"; fi
    fi
}

uninstall_focus_bindings() {
    local added=$BACKUP_DIR/focus-keys.added left
    [ -e "$added" ] || return 0
    if focus_schema; then
        left=$(python3 - "$(focus_gs get "$FOCUS_SCHEMA" bindings)" "$added" <<'PY'
import sys

from gi.repository import GLib

cur = GLib.Variant.parse(GLib.VariantType("as"), sys.argv[1]).unpack()
ours = set(open(sys.argv[2]).read().splitlines())
left = [e for e in cur if e not in ours]
print(GLib.Variant("as", left).print_(False) if left else "RESET")
PY
)
        say "removing our Focus Keys shortcuts (yours are kept)"
        if [ "$DRY" = 1 ]; then
            say "+ gsettings $([ "$left" = RESET ] && echo "reset $FOCUS_SCHEMA bindings" || echo "set $FOCUS_SCHEMA bindings $left")"
        elif [ "$left" = RESET ]; then
            focus_gs reset "$FOCUS_SCHEMA" bindings
        else
            focus_gs set "$FOCUS_SCHEMA" bindings "$left"
        fi
    fi
    act rm -f "$added" "$BACKUP_DIR/focus-keys.orig"
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
                gsettings list-keys "$KB_SCHEMA" | grep -x "$k" >/dev/null || continue
                v=$(gsettings get "$KB_SCHEMA" "$k")
                printf '%s\t%s\n' "$k" "$v" >>"$tsv"
            done
        fi
    fi
    for k in mic-mute mic-mute-static; do
        gsettings list-keys "$KB_SCHEMA" | grep -x "$k" >/dev/null || continue
        act gsettings set "$KB_SCHEMA" "$k" '[]'
    done
}

install_hotkey() {
    local raw p k keep=()
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
    if focus_available; then
        # Focus Keys raises the open window; our app custom shortcuts would only launch
        # (and clash with its grabs), so they go.
        collect_migrate
        for p in "${MIGRATE[@]}"; do
            say "shortcut: removing \"$(kb_get "$p" name)\" on $(kb_get "$p" binding) ($p), Focus Keys takes that key"
            for k in name command binding; do act gsettings reset "$KB_ITEM:$p" "$k"; done
        done
        install_focus_bindings
    else
        add_shortcut tuf-control "TUF Control" "$BIN_DIR/tuf-control-gui" "$HOTKEY"
        if [ "$HOTKEY" = XF86Launch3 ]; then takeover_launch1; fi
        if [ "$WITH_COPILOT" = 1 ]; then
            if claude_installed; then
                add_shortcut tuf-control-claude "Claude" "gtk-launch $CLAUDE_ID" "$COPILOT_KEY"
            else
                say "shortcut: Claude desktop entry not found, skipping the Copilot key"
            fi
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
        case " ${MIGRATE[*]:-} " in *" $p "*) continue ;; esac
        keep+=("$p")
    done
    if [ "${#KB_NEW[@]}" -gt 0 ] || [ "${#MIGRATE[@]}" -gt 0 ]; then kb_write "${keep[@]}"; fi
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
    fi
    if [ -e "$BACKUP_DIR/takeover.tsv" ]; then
        while IFS=$'\t' read -r p n c b; do
            [ -n "$p" ] || continue
            say "restoring your shortcut \"$n\""
            act gsettings set "$KB_ITEM:$p" name "$n"
            act gsettings set "$KB_ITEM:$p" command "$c"
            act gsettings set "$KB_ITEM:$p" binding "$b"
            # A Focus Keys install took it out of the list; put it back.
            case " ${keep[*]:-} " in *" $p "*) ;; *) keep+=("$p"); found=1 ;; esac
        done <"$BACKUP_DIR/takeover.tsv"
        act rm -f "$BACKUP_DIR/takeover.tsv"
    fi
    if [ "$found" = 1 ]; then kb_write "${keep[@]}"; fi
    uninstall_focus_bindings
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

install_daemon() {
    local state=$BACKUP_DIR/daemon-state enabled active
    snapshot_asset daemon "$DAEMON_UNIT"
    say "daemon $DAEMON_UNIT"
    if [ "$DRY" = 0 ]; then
        mkdir -p "$(dirname "$DAEMON_UNIT")"
        { printf '# %s\n' "$MARK"; cat "$ROOT/data/systemd/tuf-controld.service"; } >"$DAEMON_UNIT"
    fi
    if command -v systemctl >/dev/null 2>&1 && [ "$DRY" = 0 ] \
            && [ -z "${TUF_CONTROL_NO_SYSTEMD:-}" ]; then
        if [ ! -f "$state" ]; then
            enabled=$(systemctl --user is-enabled tuf-controld.service 2>/dev/null) || true
            active=$(systemctl --user is-active tuf-controld.service 2>/dev/null) || true
            printf '%s\n%s\n' "$enabled" "$active" >"$state"
            chmod 0600 "$state"
        fi
        act systemctl --user daemon-reload
        act systemctl --user enable --now tuf-controld.service
    fi
}

restore_daemon_state() {
    local state=$BACKUP_DIR/daemon-state enabled active
    [ -f "$state" ] && [ "$DRY" = 0 ] || return 0
    [ -z "${TUF_CONTROL_NO_SYSTEMD:-}" ] || return 0
    command -v systemctl >/dev/null 2>&1 || return 0
    enabled=$(sed -n '1p' "$state")
    active=$(sed -n '2p' "$state")
    systemctl --user daemon-reload
    case $enabled in
        enabled) systemctl --user enable tuf-controld.service ;;
        enabled-runtime) systemctl --user enable --runtime tuf-controld.service ;;
    esac
    if [ "$active" = active ]; then systemctl --user start tuf-controld.service; fi
    rm -f "$state"
}

do_install() {
    local app_literal
    command -v python3 >/dev/null 2>&1 || { echo "python3 is required" >&2; exit 1; }
    say "installing tuf-control for $USER ($([ "$DRY" = 1 ] && echo dry run || echo for real))"
    snapshot_asset package "$APP_DIR"
    act rm -rf "$APP_DIR/tuf_control"
    act mkdir -p "$APP_DIR"
    act cp -r "$ROOT/src/tuf_control" "$APP_DIR/tuf_control"
    if [ "$DRY" = 0 ]; then
        find "$APP_DIR" -name __pycache__ -type d -prune -exec rm -rf {} +
    fi
    app_literal=$(python3 -c 'import shlex,sys; print(shlex.quote(sys.argv[1]))' "$APP_DIR")
    write_launcher "$BIN_DIR/tuf-control" \
        "PYTHONPATH=$app_literal\"\${PYTHONPATH:+:\$PYTHONPATH}\" exec python3 -m tuf_control \"\$@\""
    write_launcher "$BIN_DIR/tuf-control-mcp" \
        "PYTHONPATH=$app_literal\"\${PYTHONPATH:+:\$PYTHONPATH}\" exec python3 -m tuf_control mcp \"\$@\""
    write_launcher "$BIN_DIR/tuf-control-gui" \
        "PYTHONPATH=$app_literal\"\${PYTHONPATH:+:\$PYTHONPATH}\" exec python3 -c 'from tuf_control.ui.app import main; raise SystemExit(main())' \"\$@\""
    write_launcher "$BIN_DIR/tuf-controld" \
        "PYTHONPATH=$app_literal\"\${PYTHONPATH:+:\$PYTHONPATH}\" exec python3 -m tuf_control.daemon \"\$@\""
    write_desktop
    snapshot_asset icon "$DATA_HOME/icons/hicolor/scalable/apps/$APP_ID.svg"
    snapshot_asset metainfo "$DATA_HOME/metainfo/$APP_ID.metainfo.xml"
    act install -Dm0644 "$ROOT/data/icons/hicolor/scalable/apps/$APP_ID.svg" \
        "$DATA_HOME/icons/hicolor/scalable/apps/$APP_ID.svg"
    act install -Dm0644 "$ROOT/data/$APP_ID.metainfo.xml" "$DATA_HOME/metainfo/$APP_ID.metainfo.xml"
    if [ "$WITH_EXT" = 1 ]; then install_extension; fi
    if [ "$WITH_DAEMON" = 1 ]; then install_daemon; fi
    refresh_caches
    if [ "$WITH_HOTKEY" = 1 ]; then install_hotkey; fi
    say
    say "done. Run: $BIN_DIR/tuf-control status   (make sure $BIN_DIR is on your PATH)"
    if [ "$WITH_DAEMON" = 1 ] && command -v systemctl >/dev/null 2>&1; then
        say "daemon: tuf-controld unit installed (automation, keyboard effects and update"
        say "  checks run at login; systemctl --user status tuf-controld shows it)"
    fi
    if [ "$WITH_EXT" = 1 ]; then
        say "Quick Settings extension copied; enable it yourself with:"
        say "  gnome-extensions enable $EXT_UUID   (log out and in if GNOME does not list it yet)"
    fi
}

do_uninstall() {
    say "removing tuf-control for $USER"
    local f
    # the unit file goes no matter what; the manager is only told when we may talk to it
    if [ -e "$BACKUP_DIR/assets/daemon/managed" ]; then
        if command -v systemctl >/dev/null 2>&1 && [ "$DRY" = 0 ] \
                && [ -z "${TUF_CONTROL_NO_SYSTEMD:-}" ]; then
            act systemctl --user disable --now tuf-controld.service
        fi
        restore_asset daemon "$DAEMON_UNIT"
        if command -v systemctl >/dev/null 2>&1 && [ "$DRY" = 0 ] \
                && [ -z "${TUF_CONTROL_NO_SYSTEMD:-}" ]; then
            act systemctl --user daemon-reload
        fi
    fi
    for f in "$BIN_DIR/tuf-control" "$BIN_DIR/tuf-control-gui" "$BIN_DIR/tuf-control-mcp" \
             "$BIN_DIR/tuf-controld" \
             "$DATA_HOME/applications/$APP_ID.desktop"; do
        if is_ours "$f"; then act rm -f "$f"; elif [ -e "$f" ]; then say "keeping $f (not ours)"; fi
        restore "$f"
    done
    restore_asset icon "$DATA_HOME/icons/hicolor/scalable/apps/$APP_ID.svg"
    restore_asset metainfo "$DATA_HOME/metainfo/$APP_ID.metainfo.xml"
    restore_asset package "$APP_DIR"
    restore_asset extension "$EXT_DIR"
    restore_daemon_state
    refresh_caches
    uninstall_hotkey
    say "done. Original files and managed shortcuts were restored; app configuration was retained."
}

if [ "$UNINSTALL" = 1 ]; then do_uninstall; else do_install; fi
if [ "$SYSTEM" = 1 ]; then print_system; fi
