#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
# Exercise install.sh in a throwaway HOME; the real one is never touched.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
export HOME=$T/home XDG_DATA_HOME=$T/home/.local/share XDG_STATE_HOME=$T/home/.local/state
unset TUF_CONTROL_BIN_DIR DBUS_SESSION_BUS_ADDRESS
# never talk to the real systemd user manager from the tests
export TUF_CONTROL_NO_SYSTEMD=1
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

# A match near the start of a large schema listing must not let grep exit early:
# under pipefail that closes the pipe, SIGPIPEs the producer, and hides the match.
streaming_keys() {
    printf 'mic-mute\n'
    for ((i = 0; i < 20000; i++)); do printf 'unused-%d\n' "$i"; done
}
if streaming_keys 2>/dev/null | grep -qx mic-mute >/dev/null; then
    fail "streaming fixture did not reproduce grep -q SIGPIPE"
fi
streaming_keys | grep -x mic-mute >/dev/null || fail "consuming grep missed an early schema key"
if grep -Eq 'list-keys[^|]*\|[[:space:]]*grep[^|]*-q' "$ROOT/install.sh"; then
    fail "gsettings list-keys pipeline still uses early-exit grep -q"
fi

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
[ -x "$HOME/.local/bin/tuf-controld" ] || fail "daemon launcher missing"
[ -f "$HOME/.config/systemd/user/tuf-controld.service" ] || fail "daemon unit missing"
[ -f "$XDG_DATA_HOME/gnome-shell/extensions/tuf-control-quick@hamza-gouda/extension.js" ] || fail "extension missing"
grep -Fqx "Exec=\"$HOME/.local/bin/tuf-control-gui\"" \
    "$XDG_DATA_HOME/applications/io.github.hamzagouda.TufControl.desktop" || fail "Exec not rewritten"
grep -Fqx "Exec=\"$HOME/.local/bin/tuf-control-gui\" --page updates" \
    "$XDG_DATA_HOME/applications/io.github.hamzagouda.TufControl.desktop" || fail "Updates action lost its page"
if command -v desktop-file-validate >/dev/null 2>&1; then
    desktop-file-validate "$XDG_DATA_HOME/applications/io.github.hamzagouda.TufControl.desktop"
fi
grep -q "my own" "$XDG_STATE_HOME/tuf-control/backup/${HOME//\//_}_.local_bin_tuf-control" || fail "launcher not backed up"
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
if [ "$HAVE_KB" = 1 ] && command -v glib-compile-schemas >/dev/null 2>&1; then
    # Focus Keys (bordeaux-gnome): with it installed and enabled, the app keys go into its
    # `bindings` and our earlier app custom shortcuts are migrated away. A stub of its schema
    # stands in for the extension.
    FK=org.gnome.shell.extensions.focus-keys
    FKS=$XDG_DATA_HOME/gnome-shell/extensions/focus-keys@hamza-gouda/schemas
    fk() { GSETTINGS_SCHEMA_DIR=$FKS gsettings "$@"; }
    TUF=io.github.hamzagouda.TufControl.desktop
    A="$KB.custom-keybinding:$KBP/asus-control/"
    WANT="['<Control>F9=spotify.desktop', 'XF86Launch3=$TUF', 'XF86Launch1=$TUF', '<Shift><Super>XF86TouchpadOff=com.anthropic.Claude.desktop']"
    kblist() { gsettings get "$KB" custom-keybindings; }
    stub_ext() {
        mkdir -p "$FKS"
        cat >"$FKS/$FK.gschema.xml" <<'XML'
<schemalist><schema id="org.gnome.shell.extensions.focus-keys" path="/org/gnome/shell/extensions/focus-keys/">
<key name="bindings" type="as"><default>[]</default></key>
<key name="cycle-windows" type="b"><default>true</default></key></schema></schemalist>
XML
        glib-compile-schemas "$FKS"
    }
    old_setup() {
        gsettings set "$KB" custom-keybindings "['$KBP/custom0/', '$KBP/asus-control/']"
        gsettings set "$A" name 'ASUS Armoury Control Center'
        gsettings set "$A" command "$HOME/.local/bin/asus-control"
        gsettings set "$A" binding 'Launch1'
        gsettings set "$KB" mic-mute "['XF86AudioMicMute']"
    }
    old_setup
    # 1) an earlier install made custom shortcuts (no extension yet)
    PATH="$T/stubbin:$PATH" "$ROOT/install.sh" >/dev/null
    [ "$(kblist)" = "['$KBP/custom0/', '$KBP/asus-control/', '$KBP/tuf-control/', '$KBP/tuf-control-claude/', '$KBP/tuf-control-playpause/']" ] \
        || fail "custom-shortcut install: $(kblist)"
    # 2) Focus Keys installed but not enabled: still custom shortcuts
    stub_ext
    out=$(PATH="$T/stubbin:$PATH" "$ROOT/install.sh")
    grep -q "installed but not enabled" <<<"$out" || fail "disabled Focus Keys not reported"
    case "$(kblist)" in *tuf-control/*) ;; *) fail "disabled Focus Keys still migrated" ;; esac
    # 3) enabled: migrate (dry run first)
    gsettings set org.gnome.shell enabled-extensions "['focus-keys@hamza-gouda']"
    fk set "$FK" bindings "['<Control>F9=spotify.desktop']"
    KFILE=$XDG_CONFIG_HOME/glib-2.0/settings/keyfile
    cp "$KFILE" "$T/kf.pre"
    PATH="$T/stubbin:$PATH" "$ROOT/install.sh" --dry-run >"$T/fk-dry.txt"
    cmp -s "$KFILE" "$T/kf.pre" || fail "Focus Keys dry run changed settings"
    grep -q "Focus Keys takes that key" "$T/fk-dry.txt" || fail "dry run does not show the migration"
    grep -qF "+ gsettings set $FK bindings" "$T/fk-dry.txt" || fail "dry run does not show the bindings write"
    PATH="$T/stubbin:$PATH" "$ROOT/install.sh" >/dev/null
    [ "$(fk get "$FK" bindings)" = "$WANT" ] || fail "Focus Keys bindings: $(fk get "$FK" bindings)"
    [ "$(kblist)" = "['$KBP/custom0/', '$KBP/tuf-control-playpause/']" ] || fail "migration left: $(kblist)"
    [ "$(gsettings get "$A" name)" = "''" ] || fail "taken-over Launch1 shortcut not reset"
    [ "$(gsettings get "$KB.custom-keybinding:$KBP/tuf-control/" command)" = "''" ] || fail "old TUF shortcut not reset"
    grep -q "asus-control/	ASUS Armoury Control Center" "$XDG_STATE_HOME/tuf-control/backup/takeover.tsv" \
        || fail "takeover.tsv lost"
    [ "$(gsettings get "$KB.custom-keybinding:$KBP/tuf-control-playpause/" command)" = "'playerctl play-pause'" ] \
        || fail "mic key shortcut lost"
    cp "$KFILE" "$T/kf.1"
    PATH="$T/stubbin:$PATH" "$ROOT/install.sh" >/dev/null
    cmp -s "$KFILE" "$T/kf.1" || fail "Focus Keys re-install not idempotent"
    # 4) uninstall: only our entries go, the old Armoury shortcut and mic-mute come back
    PATH="$T/stubbin:$PATH" "$ROOT/uninstall.sh" >/dev/null
    [ "$(fk get "$FK" bindings)" = "['<Control>F9=spotify.desktop']" ] || fail "uninstall bindings: $(fk get "$FK" bindings)"
    [ "$(kblist)" = "['$KBP/custom0/', '$KBP/asus-control/']" ] || fail "uninstall list: $(kblist)"
    [ "$(gsettings get "$A" name)" = "'ASUS Armoury Control Center'" ] || fail "old shortcut not restored"
    [ "$(gsettings get "$A" command)" = "'$HOME/.local/bin/asus-control'" ] || fail "old command not restored"
    [ "$(gsettings get "$KB" mic-mute)" = "['XF86AudioMicMute']" ] || fail "mic-mute not restored"
    # 5) fresh install straight into Focus Keys; nothing of ours left -> key reset
    fk reset "$FK" bindings
    old_setup
    PATH="$T/stubbin:$PATH" "$ROOT/install.sh" >/dev/null
    [ "$(kblist)" = "['$KBP/custom0/', '$KBP/tuf-control-playpause/']" ] || fail "fresh Focus Keys install list: $(kblist)"
    case "$(fk get "$FK" bindings)" in *XF86Launch1=*) ;; *) fail "fresh install lacks Launch1" ;; esac
    PATH="$T/stubbin:$PATH" "$ROOT/uninstall.sh" >/dev/null
    python3 -c "from gi.repository import Gio
s = Gio.SettingsSchemaSource.new_from_directory('$FKS', None, False).lookup('$FK', False)
assert Gio.Settings.new_full(s, None, None).get_user_value('bindings') is None" || fail "bindings not reset"
    [ "$(kblist)" = "['$KBP/custom0/', '$KBP/asus-control/']" ] || fail "fresh uninstall list: $(kblist)"
    # 6) a foreign shortcut on Launch1 keeps it; --no-focus-keys keeps custom shortcuts
    gsettings set "$A" name 'My launcher'
    gsettings set "$A" command 'true'
    out=$(PATH="$T/stubbin:$PATH" "$ROOT/install.sh")
    grep -q 'not adding XF86Launch1=.*"My launcher" uses that key' <<<"$out" || fail "Launch1 conflict not reported"
    case "$(fk get "$FK" bindings)" in *XF86Launch1=*) fail "conflicting Launch1 bound" ;; esac
    [ "$(gsettings get "$A" name)" = "'My launcher'" ] || fail "foreign Launch1 shortcut touched"
    PATH="$T/stubbin:$PATH" "$ROOT/uninstall.sh" >/dev/null
    PATH="$T/stubbin:$PATH" "$ROOT/install.sh" --no-focus-keys >/dev/null
    case "$(kblist)" in *tuf-control/*) ;; *) fail "--no-focus-keys ignored" ;; esac
    [ "$(fk get "$FK" bindings)" = "@as []" ] || fail "--no-focus-keys wrote bindings"
    # 7) the extension removed before our uninstall: no error
    rm -rf "$XDG_DATA_HOME/gnome-shell/extensions/focus-keys@hamza-gouda"
    PATH="$T/stubbin:$PATH" "$ROOT/uninstall.sh" >/dev/null
    gsettings reset org.gnome.shell enabled-extensions
fi
[ ! -e "$XDG_DATA_HOME/tuf-control" ] || fail "app dir remains"
[ ! -e "$XDG_DATA_HOME/gnome-shell/extensions/tuf-control-quick@hamza-gouda" ] || fail "extension remains"
[ ! -e "$HOME/.config/systemd/user/tuf-controld.service" ] || fail "daemon unit remains"
[ ! -e "$HOME/.local/bin/tuf-controld" ] || fail "daemon launcher remains"
grep -q "my own" "$HOME/.local/bin/tuf-control" || fail "original launcher not restored"
[ ! -e "$HOME/.local/bin/tuf-control-gui" ] || fail "gui launcher remains"
echo "install tests passed"

# Preserve pre-existing files/directories even after repeated installs. No real
# manager is contacted; a custom XDG config directory is deliberately used.
export XDG_CONFIG_HOME=$T/custom-config
unit=$XDG_CONFIG_HOME/systemd/user/tuf-controld.service
ext=$XDG_DATA_HOME/gnome-shell/extensions/tuf-control-quick@hamza-gouda
mkdir -p "$(dirname "$unit")" "$ext" "$XDG_DATA_HOME/tuf-control"
printf 'original unit\n' >"$unit"
printf 'original extension\n' >"$ext/original.txt"
printf 'original package\n' >"$XDG_DATA_HOME/tuf-control/original.txt"
"$ROOT/uninstall.sh" >/dev/null
[ "$(cat "$unit")" = 'original unit' ] || fail "no-state uninstall removed foreign service"
[ -f "$ext/original.txt" ] || fail "no-state uninstall removed foreign extension"
"$ROOT/install.sh" --no-hotkey >/dev/null
"$ROOT/install.sh" --no-hotkey >/dev/null
"$ROOT/uninstall.sh" >/dev/null
[ "$(cat "$unit")" = 'original unit' ] || fail "original daemon unit not restored"
[ "$(cat "$ext/original.txt")" = 'original extension' ] || fail "original extension not restored"
[ -f "$XDG_DATA_HOME/tuf-control/original.txt" ] || fail "original package not restored"
# Manager interactions are recorded by a stub, with no connection to live systemd.
mkdir -p "$T/manager-bin"
cat >"$T/manager-bin/systemctl" <<'STUB'
#!/bin/sh
printf '%s\n' "$*" >>"$TUF_TEST_MANAGER_LOG"
case "$*" in
    *is-enabled*) printf 'disabled\n'; exit 1 ;;
    *is-active*) printf 'active\n' ;;
esac
STUB
chmod +x "$T/manager-bin/systemctl"
export TUF_TEST_MANAGER_LOG=$T/manager.log
TUF_CONTROL_NO_SYSTEMD='' PATH="$T/manager-bin:$PATH" "$ROOT/install.sh" --no-hotkey >/dev/null
TUF_CONTROL_NO_SYSTEMD='' PATH="$T/manager-bin:$PATH" "$ROOT/uninstall.sh" >/dev/null
grep -qx -- '--user start tuf-controld.service' "$TUF_TEST_MANAGER_LOG" || fail "previous active state not restored"
[ "$(cat "$unit")" = 'original unit' ] || fail "service restore with manager lost original"
echo "asset and service restoration tests passed"

# Launcher code must quote literal installation paths, including shell syntax.
export HOME="$T/home with 'quotes' and \$literal"
export XDG_DATA_HOME=$HOME/.local/share XDG_STATE_HOME=$HOME/.local/state XDG_CONFIG_HOME=$HOME/.config
"$ROOT/install.sh" --no-hotkey --no-daemon >/dev/null
"$HOME/.local/bin/tuf-control" --help >/dev/null || fail "literal-path CLI launcher failed"
"$ROOT/uninstall.sh" >/dev/null
echo "literal installation path test passed"
