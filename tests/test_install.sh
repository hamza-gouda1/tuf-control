#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Exercise install.sh in a throwaway HOME; the real one is never touched.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
export HOME=$T/home XDG_DATA_HOME=$T/home/.local/share XDG_STATE_HOME=$T/home/.local/state
unset TUF_CONTROL_BIN_DIR DBUS_SESSION_BUS_ADDRESS
mkdir -p "$HOME/.local/bin"
# GNOME shortcuts go to a keyfile settings backend inside the temp HOME, never the real dconf
export GSETTINGS_BACKEND=keyfile XDG_CONFIG_HOME=$T/home/.config
KB=org.gnome.settings-daemon.plugins.media-keys
KBP=/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings
HAVE_KB=0
if command -v gsettings >/dev/null 2>&1 && gsettings list-keys "$KB" >/dev/null 2>&1; then
    HAVE_KB=1
    gsettings set "$KB" custom-keybindings "['$KBP/custom0/']"
    gsettings set "$KB.custom-keybinding:$KBP/custom0/" name 'Mine'
    gsettings set "$KB.custom-keybinding:$KBP/custom0/" command 'true'
    gsettings set "$KB.custom-keybinding:$KBP/custom0/" binding '<Super>x'
fi
fail() { echo "FAIL: $*" >&2; exit 1; }

echo "# my own launcher" >"$HOME/.local/bin/tuf-control"

"$ROOT/install.sh" --dry-run --no-copilot-key --no-mic-key >"$T/dry.txt"
if [ "$HAVE_KB" = 1 ]; then
    grep -q "gsettings set" "$T/dry.txt" || fail "dry run did not show the hotkey commands"
    [ "$(gsettings get "$KB" custom-keybindings)" = "['$KBP/custom0/']" ] || fail "dry run changed shortcuts"
fi
[ ! -e "$XDG_DATA_HOME/tuf-control" ] || fail "dry run created files"
grep -q "my own" "$HOME/.local/bin/tuf-control" || fail "dry run touched launcher"

"$ROOT/install.sh" --system --no-copilot-key --no-mic-key >"$T/out.txt"
grep -q "sudo install -Dm0755" "$T/out.txt" || fail "--system did not print sudo commands"
"$ROOT/install.sh" --no-copilot-key --no-mic-key >/dev/null   # second run = idempotent

[ -f "$XDG_DATA_HOME/tuf-control/tuf_control/__init__.py" ] || fail "package missing"
[ -f "$XDG_DATA_HOME/tuf-control/tuf_control/helper.py" ] || fail "helper missing from package"
[ -x "$HOME/.local/bin/tuf-control-gui" ] || fail "gui launcher missing"
[ -f "$XDG_DATA_HOME/gnome-shell/extensions/tuf-control-quick@hamza-gouda/extension.js" ] || fail "extension missing"
grep -q "^Exec=$HOME/.local/bin/tuf-control-gui$" \
    "$XDG_DATA_HOME/applications/io.github.hamzagouda.TufControl.desktop" || fail "Exec not rewritten"
grep -q "my own" "$XDG_STATE_HOME/tuf-control/backup/"* || fail "launcher not backed up"
"$HOME/.local/bin/tuf-control" --help >/dev/null || fail "installed CLI does not start"
TUF_CONTROL_FAKE_ROOT=$T/none "$HOME/.local/bin/tuf-control" --help >/dev/null || fail "CLI start with fake root"

if [ "$HAVE_KB" = 1 ]; then
    [ "$(gsettings get "$KB" custom-keybindings)" = "['$KBP/custom0/', '$KBP/tuf-control/']" ] \
        || fail "shortcut not appended exactly once: $(gsettings get "$KB" custom-keybindings)"
    K="$KB.custom-keybinding:$KBP/tuf-control/"
    [ "$(gsettings get "$K" binding)" = "'XF86Launch3'" ] || fail "wrong binding"
    [ "$(gsettings get "$K" name)" = "'TUF Control'" ] || fail "wrong name"
    [ "$(gsettings get "$K" command)" = "'$HOME/.local/bin/tuf-control-gui'" ] || fail "wrong command"
    [ "$(gsettings get "$KB.custom-keybinding:$KBP/custom0/" binding)" = "'<Super>x'" ] || fail "own shortcut modified"
    grep -q custom0 "$XDG_STATE_HOME/tuf-control/backup/custom-keybindings.list" || fail "list not backed up"
fi

"$ROOT/install.sh" --no-extension --no-copilot-key --no-mic-key >/dev/null
"$ROOT/uninstall.sh" >/dev/null
if [ "$HAVE_KB" = 1 ]; then
    [ "$(gsettings get "$KB" custom-keybindings)" = "['$KBP/custom0/']" ] || fail "uninstall did not remove only our shortcut"
    [ "$(gsettings get "$KB.custom-keybinding:$KBP/custom0/" name)" = "'Mine'" ] || fail "own shortcut lost"
    "$ROOT/install.sh" --no-hotkey >/dev/null
    [ "$(gsettings get "$KB" custom-keybindings)" = "['$KBP/custom0/']" ] || fail "--no-hotkey still added a shortcut"
    gsettings set "$KB.custom-keybinding:$KBP/custom0/" binding 'XF86Launch3'
    "$ROOT/install.sh" --no-copilot-key --no-mic-key >"$T/conflict.txt"
    grep -q "already used" "$T/conflict.txt" || fail "conflicting shortcut not reported"
    [ "$(gsettings get "$KB" custom-keybindings)" = "['$KBP/custom0/']" ] || fail "conflict still added ours"
    "$ROOT/uninstall.sh" >/dev/null
fi
if [ "$HAVE_KB" = 1 ]; then
    # Launch1 takeover, Copilot key -> Claude, mic key -> play/pause (stubs in the temp HOME)
    mkdir -p "$T/stubbin" "$XDG_DATA_HOME/applications"
    printf '#!/bin/sh\nexit 0\n' >"$T/stubbin/playerctl"; chmod +x "$T/stubbin/playerctl"
    printf '[Desktop Entry]\nName=Claude\nExec=true\nType=Application\n' \
        >"$XDG_DATA_HOME/applications/com.anthropic.Claude.desktop"
    gsettings set "$KB" custom-keybindings "['$KBP/custom0/', '$KBP/asus-control/']"
    A="$KB.custom-keybinding:$KBP/asus-control/"
    gsettings set "$A" name 'ASUS Armoury Control Center'
    gsettings set "$A" command "$HOME/.local/bin/asus-control"
    gsettings set "$A" binding 'Launch1'
    gsettings set "$KB" mic-mute "['XF86AudioMicMute']"
    gsettings set "$KB" mic-mute-static "['XF86AudioMicMute']"
    gsettings set "$KB.custom-keybinding:$KBP/custom0/" binding '<Super>x'
    PATH="$T/stubbin:$PATH" "$ROOT/install.sh" --dry-run >"$T/dry2.txt"
    grep -q "retargeting" "$T/dry2.txt" || fail "dry run did not mention the takeover"
    [ "$(gsettings get "$A" name)" = "'ASUS Armoury Control Center'" ] || fail "dry run changed the old shortcut"
    PATH="$T/stubbin:$PATH" "$ROOT/install.sh" >/dev/null
    PATH="$T/stubbin:$PATH" "$ROOT/install.sh" >/dev/null   # idempotent
    [ "$(gsettings get "$A" name)" = "'TUF Control'" ] || fail "Launch1 shortcut not retargeted"
    [ "$(gsettings get "$A" command)" = "'$HOME/.local/bin/tuf-control-gui'" ] || fail "Launch1 command not retargeted"
    [ "$(gsettings get "$KB.custom-keybinding:$KBP/tuf-control-claude/" command)" = "'gtk-launch com.anthropic.Claude'" ] \
        || fail "Copilot shortcut missing"
    [ "$(gsettings get "$KB.custom-keybinding:$KBP/tuf-control-playpause/" command)" = "'playerctl play-pause'" ] \
        || fail "play/pause shortcut missing"
    [ "$(gsettings get "$KB" mic-mute)" = "@as []" ] || fail "mic-mute not emptied"
    [ "$(gsettings get "$KB" mic-mute-static)" = "@as []" ] || fail "mic-mute-static not emptied"
    n=$(gsettings get "$KB" custom-keybindings | grep -o "tuf-control-claude" | wc -l)
    [ "$n" = 1 ] || fail "Copilot shortcut added $n times"
    PATH="$T/stubbin:$PATH" "$ROOT/uninstall.sh" >/dev/null
    [ "$(gsettings get "$A" name)" = "'ASUS Armoury Control Center'" ] || fail "old shortcut name not restored"
    [ "$(gsettings get "$A" command)" = "'$HOME/.local/bin/asus-control'" ] || fail "old shortcut command not restored"
    [ "$(gsettings get "$KB" mic-mute-static)" = "['XF86AudioMicMute']" ] || fail "mic-mute-static not restored"
    [ "$(gsettings get "$KB" mic-mute)" = "['XF86AudioMicMute']" ] || fail "mic-mute not restored"
    case "$(gsettings get "$KB" custom-keybindings)" in *tuf-control*) fail "our shortcuts remain" ;; esac
    PATH="$T/stubbin:$PATH" "$ROOT/install.sh" --no-mic-key --no-copilot-key >/dev/null
    [ "$(gsettings get "$KB" mic-mute-static)" = "['XF86AudioMicMute']" ] || fail "--no-mic-key touched mic-mute"
    "$ROOT/uninstall.sh" >/dev/null
fi
[ ! -e "$XDG_DATA_HOME/tuf-control" ] || fail "app dir remains"
[ ! -e "$XDG_DATA_HOME/gnome-shell/extensions/tuf-control-quick@hamza-gouda" ] || fail "extension remains"
grep -q "my own" "$HOME/.local/bin/tuf-control" || fail "original launcher not restored"
[ ! -e "$HOME/.local/bin/tuf-control-gui" ] || fail "gui launcher remains"
echo "install tests passed"
