#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
#
# User-level installer for asusius. Nothing is downloaded and no root is needed:
# the package is copied into ~/.local/share/asusius and started through small
# launchers in ~/.local/bin. Root-only pieces (polkit helper) are only PRINTED
# (--system), never executed. Safe to run repeatedly.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
DATA_HOME=${XDG_DATA_HOME:-$HOME/.local/share}
STATE_HOME=${XDG_STATE_HOME:-$HOME/.local/state}
BIN_DIR=${ASUSIUS_BIN_DIR:-$HOME/.local/bin}
APP_DIR=$DATA_HOME/asusius
BACKUP_DIR=$STATE_HOME/asusius/backup
APP_ID=io.github.hamzagouda.Asusius
EXT_UUID=asusius-quick@hamza-gouda
EXT_DIR=$DATA_HOME/gnome-shell/extensions/$EXT_UUID
CONFIG_HOME=${XDG_CONFIG_HOME:-$HOME/.config}
DAEMON_UNIT=$CONFIG_HOME/systemd/user/asusiusd.service
MARK="managed by asusius install.sh"

# GNOME custom keybinding for the dedicated Armoury Crate key (asus-nb-wmi code 0x38 ->
# KEY_PROG3 (202) -> XKB keycode 210 -> XF86Launch3). See docs/INSTALL.md.
KB_SCHEMA=org.gnome.settings-daemon.plugins.media-keys
KB_ITEM=$KB_SCHEMA.custom-keybinding
KB_BASE=/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings
HOTKEY=${ASUSIUS_HOTKEY:-XF86Launch3}
# Copilot key (Shift+Super+F23; XKB calls F23 XF86TouchpadOff) opens Claude, the mic key toggles music.
COPILOT_KEY=${ASUSIUS_COPILOT_KEY:-<Shift><Super>XF86TouchpadOff}
MIC_KEY=${ASUSIUS_MIC_KEY:-XF86AudioMicMute}
# The Focus Keys GNOME Shell extension (from bordeaux-gnome) raises an app's open window from
# a shortcut; a custom shortcut running a launcher only gets an "is ready" notification on
# Wayland. When it is installed and enabled, the app keys are written into its `bindings`.
FOCUS_UUID=focus-keys@hamza-gouda
FOCUS_SCHEMA=org.gnome.shell.extensions.focus-keys
FOCUS_SCHEMAS=$DATA_HOME/gnome-shell/extensions/$FOCUS_UUID/schemas
CLAUDE_ID=com.anthropic.Claude

DRY=0 UNINSTALL=0 SYSTEM=0 WITH_EXT=1 WITH_HOTKEY=1 WITH_COPILOT=0 WITH_MIC=0 WITH_FOCUS=1 \
WITH_DAEMON=1 EXT_ENABLED=0

usage() {
    cat <<USAGE
Usage: ./install.sh [--dry-run] [--uninstall] [--system] [--no-extension]
                  [--no-hotkey] [--hotkey=KEYSYM]
                  [--with-copilot-key] [--copilot-key=ACCEL]
                  [--with-mic-key] [--mic-key=KEYSYM]
                  [--no-focus-keys] [--no-daemon]

  (default)       install for the current user under ~/.local (no root, no downloads)
  --dry-run       show what would happen, change nothing
  --uninstall     remove what this script installed (backups are restored)
  --system        also print the exact sudo commands for the polkit helper and policy
                  (they are NOT executed; run them yourself if you want root-only
                  writes through pkexec)
  --no-extension  skip the GNOME Shell Quick Settings extension
  --no-hotkey     do not add the GNOME shortcut that opens Asusius from the
                  Armoury Crate key (a custom keybinding named "Asusius", appended to
                  your existing custom shortcuts, removed again by --uninstall)
  --hotkey=KEYSYM use another key than XF86Launch3 for that shortcut. An existing custom
                  shortcut on XF86Launch1 that runs the retired asus-control app is
                  retargeted to Asusius (old values saved, restored by --uninstall)
  --with-copilot-key  opt in to binding the Copilot key to the Claude desktop app
  --no-copilot-key    do not bind the Copilot key (default)
  --copilot-key=ACCEL  other key for that (default <Shift><Super>XF86TouchpadOff)
  --with-mic-key      opt in to binding the mic key to play/pause (silences GNOME mic-mute)
  --no-mic-key        do not bind the mic key (default; leaves GNOME mic-mute untouched)
  --mic-key=KEYSYM  other key for play/pause (default XF86AudioMicMute)
  --no-focus-keys use GNOME custom shortcuts for the app keys even when the Focus Keys
                  extension (bordeaux-gnome) is installed. With it, the Armoury keys
                  (XF86Launch3 and XF86Launch1) and the optional Copilot key go into its
                  shortcut list instead, so a second press brings the open window to the
                  front; our earlier custom shortcuts for them are removed
  --no-daemon     do not install or start asusiusd (the per-user automation and
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
        --with-copilot-key) WITH_COPILOT=1 ;;
        --no-copilot-key) WITH_COPILOT=0 ;;
        --copilot-key=*) COPILOT_KEY=${arg#--copilot-key=} ;;
        --with-mic-key) WITH_MIC=1 ;;
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
    local src=$ROOT/data/Asusius.desktop dst=$DATA_HOME/applications/$APP_ID.desktop
    backup "$dst"
    say "desktop entry $dst"
    [ "$DRY" = 0 ] || return 0
    mkdir -p "$(dirname "$dst")"
    python3 - "$src" "$dst" "$BIN_DIR/asusius-gui" "$MARK" <<'PYTHON'
from pathlib import Path
import sys
src, dst, launcher, marker = sys.argv[1:]
# Desktop Entry string escaping precedes Exec quoting. Percent is a field code.
quoted = launcher.replace('\\', '\\\\').replace('"', '\\"')
quoted = quoted.replace('`', '\\`').replace('$', '\\$').replace('%', '%%')
quoted = ('"' + quoted + '"').replace('\\', '\\\\')
lines = []
for line in Path(src).read_text().splitlines():
    if line.startswith('Exec=asusius-gui'):
        line = 'Exec=' + quoted + line[len('Exec=asusius-gui'):]
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

is_gnome_desktop() {
    local d=${XDG_CURRENT_DESKTOP:-}
    [ -z "$d" ] && return 0
    case ${d,,} in *gnome*) return 0 ;; esac
    return 1
}

systemd_user_available() {
    [ -z "${ASUSIUS_NO_SYSTEMD:-}" ] || return 1
    command -v systemctl >/dev/null 2>&1 || return 1
    systemctl --user show-environment >/dev/null 2>&1
}

print_distro_install_hints() {
    cat >&2 <<'HINTS'
Install required system packages first:
  Ubuntu / Debian: sudo apt install python3 python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-pango-1.0 libglib2.0-bin
  Fedora:          sudo dnf install python3 python3-gobject gtk4 libadwaita cairo-gobject glib2
  Arch Linux:      sudo pacman -S python python-gobject python-cairo gtk4 libadwaita glib2
  openSUSE:        sudo zypper install python3-gobject python3-gobject-cairo typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1 typelib-1_0-Pango-1.0 glib2-tools
HINTS
}

check_deps() {
    [ -z "${ASUSIUS_SKIP_DEP_CHECK:-}" ] || return 0
    local missing=() gi_report
    if ! command -v python3 >/dev/null 2>&1; then
        missing+=("python3 (>= 3.12)")
    elif ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)'; then
        missing+=("python3 >= 3.12 (found $(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:3])))'))")
    else
        gi_report=$(python3 - <<'PY' 2>/dev/null || true
missing = []
try:
    import gi
except Exception:
    missing.append("PyGObject (gi)")
else:
    for ns, ver in (("Gtk", "4.0"), ("Adw", "1"), ("PangoCairo", "1.0")):
        try:
            gi.require_version(ns, ver)
        except Exception:
            missing.append(f"{ns} {ver}")
try:
    import cairo  # noqa: F401
except Exception:
    missing.append("pycairo (cairo)")
if missing:
    print(", ".join(missing))
PY
)
        if [ -n "$gi_report" ]; then
            missing+=("$gi_report")
        fi
    fi

    if [ "${#missing[@]}" -gt 0 ]; then
        echo "Missing required runtime dependencies: ${missing[*]}" >&2
        print_distro_install_hints
        if [ "$DRY" = 0 ]; then
            exit 1
        fi
    fi

    if [ "$WITH_EXT" = 1 ] && is_gnome_desktop && ! command -v glib-compile-schemas >/dev/null 2>&1; then
        say "WARNING: glib-compile-schemas not found; extension schemas will remain uncompiled."
        say "  Ubuntu/Debian: sudo apt install libglib2.0-bin | Fedora/Arch: glib2 | openSUSE: glib2-tools"
    fi

    if ! command -v asusd >/dev/null 2>&1 && ! command -v asusctl >/dev/null 2>&1; then
        say "note: asusd/asusctl not found; generic sensors, battery and updates still work, while ASUS fan curves and Aura lighting require asusd."
    fi
    if ! command -v supergfxd >/dev/null 2>&1 && ! command -v supergfxctl >/dev/null 2>&1; then
        say "note: supergfxd/supergfxctl not found; GPU mode switching requires supergfxd."
    fi
    if ! command -v pkexec >/dev/null 2>&1; then
        say "note: pkexec (polkit) not found; privileged direct sysfs writes will not elevate."
    fi
}

install_extension() {
    local src=$ROOT/extension/$EXT_UUID
    if ! is_gnome_desktop; then
        say "extension: skipped (desktop is '${XDG_CURRENT_DESKTOP}', not GNOME)"
        return 0
    fi
    say "extension $EXT_DIR"
    snapshot_asset extension "$EXT_DIR"
    act rm -rf "$EXT_DIR"
    act mkdir -p "$EXT_DIR"
    act cp -r "$src/." "$EXT_DIR/"
    if [ -d "$EXT_DIR/schemas" ]; then
        if command -v glib-compile-schemas >/dev/null 2>&1; then
            act glib-compile-schemas "$EXT_DIR/schemas"
        else
            say "WARNING: glib-compile-schemas not found; skipping schema compilation for $EXT_UUID (extension settings will fail until compiled)."
        fi
    fi
    if [ "$DRY" = 0 ] && [ -n "${DBUS_SESSION_BUS_ADDRESS:-}" ] \
            && [ -z "${GSETTINGS_BACKEND:-}" ] \
            && command -v gnome-extensions >/dev/null 2>&1; then
        if gnome-extensions list 2>/dev/null | grep -qxF "$EXT_UUID"; then
            if gnome-extensions enable "$EXT_UUID" >/dev/null 2>&1; then
                EXT_ENABLED=1
                say "extension $EXT_UUID enabled"
            fi
        fi
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
# to Asusius (MODE retarget) or, when Focus Keys takes the key, removed (MODE remove: the
# path goes to MIGRATE). Its old name/command/binding are kept in takeover.tsv for uninstall.
takeover_launch1() {
    local mode=${1:-retarget} p n c b tsv=$BACKUP_DIR/takeover.tsv
    # Taken over earlier and since removed by a Focus Keys install: bring it back as Asusius.
    if [ "$mode" = retarget ] && [ -e "$tsv" ]; then
        while IFS=$'\t' read -r p n c b; do
            [ -n "$p" ] || continue
            case " ${KB_CUR[*]} " in *" $p "*) continue ;; esac
            say "shortcut $b: re-adding \"Asusius\" ($BIN_DIR/asusius-gui)"
            act gsettings set "$KB_ITEM:$p" name 'Asusius'
            act gsettings set "$KB_ITEM:$p" command "$BIN_DIR/asusius-gui"
            act gsettings set "$KB_ITEM:$p" binding "$b"
            KB_NEW+=("$p")
        done <"$tsv"
    fi
    for p in "${KB_CUR[@]}"; do
        [ -n "$p" ] || continue
        case $p in "$KB_BASE"/asusius*) continue ;; esac
        n=$(kb_get "$p" binding)
        [ "${n#XF86}" = Launch1 ] || continue
        n=$(kb_get "$p" name)
        c=$(kb_get "$p" command)
        if [ -e "$tsv" ] && grep -q "^$p	" "$tsv"; then
            : # ours since an earlier run (retargeted to Asusius)
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
        say "shortcut XF86Launch1: retargeting \"$n\" -> \"Asusius\" ($BIN_DIR/asusius-gui)"
        act gsettings set "$KB_ITEM:$p" name 'Asusius'
        act gsettings set "$KB_ITEM:$p" command "$BIN_DIR/asusius-gui"
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
        case $p in
            "$KB_BASE/asusius/") MIGRATE+=("$p") ;;
            "$KB_BASE/asusius-claude/") [ "$WITH_COPILOT" = 1 ] && MIGRATE+=("$p") ;;
        esac
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
    if ! is_gnome_desktop; then
        say "shortcuts: skipped (desktop is '${XDG_CURRENT_DESKTOP}', not GNOME)"
        return 0
    fi
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
        add_shortcut asusius "Asusius" "$BIN_DIR/asusius-gui" "$HOTKEY"
        if [ "$HOTKEY" = XF86Launch3 ]; then takeover_launch1; fi
        if [ "$WITH_COPILOT" = 1 ]; then
            if claude_installed; then
                add_shortcut asusius-claude "Claude" "gtk-launch $CLAUDE_ID" "$COPILOT_KEY"
            else
                say "shortcut: Claude desktop entry not found, skipping the Copilot key"
            fi
        fi
    fi
    if [ "$WITH_MIC" = 1 ]; then
        if command -v playerctl >/dev/null 2>&1; then
            add_shortcut asusius-playpause "Play/pause music" "playerctl play-pause" "$MIC_KEY"
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
            "$KB_BASE"/asusius*)
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

  sudo install -Dm0755 "$ROOT/src/asusius/helper.py" /usr/libexec/asusius-helper
  sudo install -Dm0644 "$ROOT/packaging/polkit/io.github.hamzagouda.asusius.policy" /usr/share/polkit-1/actions/io.github.hamzagouda.asusius.policy

Remove again with:

  sudo rm -f /usr/libexec/asusius-helper /usr/share/polkit-1/actions/io.github.hamzagouda.asusius.policy
SYS
}

install_daemon() {
    local state=$BACKUP_DIR/daemon-state enabled active
    snapshot_asset daemon "$DAEMON_UNIT"
    say "daemon $DAEMON_UNIT"
    if [ "$DRY" = 0 ]; then
        mkdir -p "$(dirname "$DAEMON_UNIT")"
        python3 - "$ROOT/data/systemd/asusiusd.service" "$DAEMON_UNIT" "$BIN_DIR" "$MARK" <<'PYTHON'
from pathlib import Path
import sys

src, dst, bin_dir, marker = sys.argv[1:]
escaped_bin = bin_dir.replace("%", "%%")
path_value = f"{escaped_bin}:/usr/local/bin:/usr/bin:/bin"
if any(ch.isspace() or ch in '"\\' for ch in path_value):
    quoted = path_value.replace("\\", "\\\\").replace('"', '\\"')
    env_line = f'Environment="PATH={quoted}"'
else:
    env_line = f"Environment=PATH={path_value}"

lines = []
for line in Path(src).read_text(encoding="utf-8").splitlines():
    if line.startswith("Environment=PATH="):
        line = env_line
    lines.append(line)
Path(dst).write_text(f"# {marker}\n" + "\n".join(lines) + "\n", encoding="utf-8")
PYTHON
    fi
    if [ "$DRY" = 0 ] && systemd_user_available; then
        active=$(systemctl --user is-active asusiusd.service 2>/dev/null) || true
        if [ ! -f "$state" ]; then
            enabled=$(systemctl --user is-enabled asusiusd.service 2>/dev/null) || true
            printf '%s\n%s\n' "$enabled" "$active" >"$state"
            chmod 0600 "$state"
        fi
        act systemctl --user daemon-reload
        if [ "$active" = active ]; then
            act systemctl --user enable asusiusd.service
            act systemctl --user restart asusiusd.service
        else
            act systemctl --user enable --now asusiusd.service
        fi
    elif [ "$DRY" = 0 ] && [ -z "${ASUSIUS_NO_SYSTEMD:-}" ]; then
        say "daemon: systemd user manager not available (container or non-systemd session); unit written to $DAEMON_UNIT"
        say "  enable later in a systemd session with: systemctl --user enable --now asusiusd.service"
    fi
}

restore_daemon_state() {
    local state=$BACKUP_DIR/daemon-state enabled active
    [ -f "$state" ] && [ "$DRY" = 0 ] || return 0
    systemd_user_available || return 0
    enabled=$(sed -n '1p' "$state")
    active=$(sed -n '2p' "$state")
    systemctl --user daemon-reload
    case $enabled in
        enabled) systemctl --user enable asusiusd.service ;;
        enabled-runtime) systemctl --user enable --runtime asusiusd.service ;;
    esac
    if [ "$active" = active ]; then systemctl --user start asusiusd.service; fi
    rm -f "$state"
}

copy_package() {
    say "package $APP_DIR/asusius"
    if [ "$DRY" = 1 ]; then
        say "+ copy $ROOT/src/asusius -> $APP_DIR/asusius (excluding .mimosa, __pycache__, *.pyc)"
        return 0
    fi
    mkdir -p "$APP_DIR"
    python3 - "$ROOT/src/asusius" "$APP_DIR/asusius" <<'PYTHON'
import shutil
import sys

shutil.copytree(
    sys.argv[1],
    sys.argv[2],
    ignore=shutil.ignore_patterns(".mimosa", "__pycache__", "*.pyc", "*.pyo"),
)
PYTHON
}

do_install() {
    local app_literal sym_icon helper_found=0 helper
    check_deps
    say "installing asusius for $USER ($([ "$DRY" = 1 ] && echo dry run || echo for real))"
    snapshot_asset package "$APP_DIR"
    act rm -rf "$APP_DIR/asusius"
    copy_package
    app_literal=$(python3 -c 'import shlex,sys; print(shlex.quote(sys.argv[1]))' "$APP_DIR")
    write_launcher "$BIN_DIR/asusius" \
        "PYTHONPATH=$app_literal\"\${PYTHONPATH:+:\$PYTHONPATH}\" exec python3 -m asusius \"\$@\""
    write_launcher "$BIN_DIR/asusius-mcp" \
        "PYTHONPATH=$app_literal\"\${PYTHONPATH:+:\$PYTHONPATH}\" exec python3 -m asusius mcp \"\$@\""
    write_launcher "$BIN_DIR/asusius-gui" \
        "PYTHONPATH=$app_literal\"\${PYTHONPATH:+:\$PYTHONPATH}\" exec python3 -m asusius.ui \"\$@\""
    write_launcher "$BIN_DIR/asusiusd" \
        "PYTHONPATH=$app_literal\"\${PYTHONPATH:+:\$PYTHONPATH}\" exec python3 -m asusius.daemon \"\$@\""
    write_desktop
    snapshot_asset icon "$DATA_HOME/icons/hicolor/scalable/apps/$APP_ID.svg"
    snapshot_asset metainfo "$DATA_HOME/metainfo/$APP_ID.metainfo.xml"
    act install -Dm0644 "$ROOT/data/icons/hicolor/scalable/apps/$APP_ID.svg" \
        "$DATA_HOME/icons/hicolor/scalable/apps/$APP_ID.svg"
    sym_icon=$ROOT/data/icons/hicolor/scalable/apps/$APP_ID-symbolic.svg
    if [ -f "$sym_icon" ]; then
        snapshot_asset icon_symbolic "$DATA_HOME/icons/hicolor/scalable/apps/$APP_ID-symbolic.svg"
        act install -Dm0644 "$sym_icon" \
            "$DATA_HOME/icons/hicolor/scalable/apps/$APP_ID-symbolic.svg"
    fi
    act install -Dm0644 "$ROOT/data/Asusius.metainfo.xml" "$DATA_HOME/metainfo/$APP_ID.metainfo.xml"
    if [ "$WITH_EXT" = 1 ]; then install_extension; fi
    if [ "$WITH_DAEMON" = 1 ]; then install_daemon; fi
    refresh_caches
    if [ "$WITH_HOTKEY" = 1 ]; then install_hotkey; fi
    say
    case ":$PATH:" in
        *":$BIN_DIR:"*) say "done. Run: asusius status" ;;
        *)
            say "done. Run: $BIN_DIR/asusius status"
            say "WARNING: $BIN_DIR is not in your PATH. Add it to your shell profile (~/.bashrc or ~/.zshrc):"
            say "  export PATH=\"$BIN_DIR:\$PATH\"" ;;
    esac
    if [ "$WITH_DAEMON" = 1 ] && [ "$DRY" = 0 ] && systemd_user_available; then
        say "daemon: asusiusd unit enabled (automation, keyboard effects and update"
        say "  checks run at login; systemctl --user status asusiusd shows it)"
    fi
    if [ "$WITH_EXT" = 1 ] && is_gnome_desktop; then
        if [ "$EXT_ENABLED" = 1 ]; then
            say "Quick Settings extension enabled ($EXT_UUID)."
        elif [ "${XDG_SESSION_TYPE:-}" = x11 ]; then
            say "Quick Settings extension copied; restart GNOME Shell (Alt+F2 -> r) or log out and in, then run:"
            say "  gnome-extensions enable $EXT_UUID"
        else
            say "Quick Settings extension copied; on Wayland log out and back in first, then enable it with:"
            say "  gnome-extensions enable $EXT_UUID"
        fi
    fi
    if [ "${XDG_SESSION_TYPE:-}" = x11 ]; then
        say "note: X11 session detected; GNOME Wayland is recommended for native touchpad gesture handling."
    fi
    for helper in /usr/libexec/asusius-helper /usr/lib/asusius/asusius-helper; do
        if [ -e "$helper" ]; then helper_found=1; break; fi
    done
    if [ "$SYSTEM" = 0 ] && [ "$helper_found" = 0 ]; then
        say "note: optional root-only sysfs writes through pkexec need the helper (run ./install.sh --system to inspect sudo commands)."
    fi
}

do_uninstall() {
    say "removing asusius for $USER"
    local f helper found_helpers=()
    # the unit file goes no matter what; the manager is only told when we may talk to it
    if [ -e "$BACKUP_DIR/assets/daemon/managed" ]; then
        if [ "$DRY" = 0 ] && systemd_user_available; then
            act systemctl --user disable --now asusiusd.service || true
        fi
        restore_asset daemon "$DAEMON_UNIT"
        if [ "$DRY" = 0 ] && systemd_user_available; then
            act systemctl --user daemon-reload || true
        fi
    fi
    for f in "$BIN_DIR/asusius" "$BIN_DIR/asusius-gui" "$BIN_DIR/asusius-mcp" \
             "$BIN_DIR/asusiusd" \
             "$DATA_HOME/applications/$APP_ID.desktop"; do
        if is_ours "$f"; then act rm -f "$f"; elif [ -e "$f" ]; then say "keeping $f (not ours)"; fi
        restore "$f"
    done
    restore_asset icon "$DATA_HOME/icons/hicolor/scalable/apps/$APP_ID.svg"
    restore_asset icon_symbolic "$DATA_HOME/icons/hicolor/scalable/apps/$APP_ID-symbolic.svg"
    restore_asset metainfo "$DATA_HOME/metainfo/$APP_ID.metainfo.xml"
    restore_asset package "$APP_DIR"
    restore_asset extension "$EXT_DIR"
    restore_daemon_state
    refresh_caches
    uninstall_hotkey
    for helper in /usr/libexec/asusius-helper /usr/lib/asusius/asusius-helper; do
        if [ -e "$helper" ]; then found_helpers+=("$helper"); fi
    done
    if [ "${#found_helpers[@]}" -gt 0 ]; then
        say "WARNING: root-level helper still installed: ${found_helpers[*]}"
        say "  remove it with: sudo rm -f /usr/libexec/asusius-helper /usr/lib/asusius/asusius-helper /usr/share/polkit-1/actions/io.github.hamzagouda.asusius.policy"
    fi
    say "done. Original files and managed shortcuts were restored; app configuration was retained."
}

if [ "$UNINSTALL" = 1 ]; then do_uninstall; else do_install; fi
if [ "$SYSTEM" = 1 ]; then print_system; fi
