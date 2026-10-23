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
fail() { echo "FAIL: $*" >&2; exit 1; }

echo "# my own launcher" >"$HOME/.local/bin/tuf-control"

"$ROOT/install.sh" --dry-run >/dev/null
[ ! -e "$XDG_DATA_HOME/tuf-control" ] || fail "dry run created files"
grep -q "my own" "$HOME/.local/bin/tuf-control" || fail "dry run touched launcher"

"$ROOT/install.sh" --system >"$T/out.txt"
grep -q "sudo install -Dm0755" "$T/out.txt" || fail "--system did not print sudo commands"
"$ROOT/install.sh" >/dev/null   # second run = idempotent

[ -f "$XDG_DATA_HOME/tuf-control/tuf_control/__init__.py" ] || fail "package missing"
[ -f "$XDG_DATA_HOME/tuf-control/tuf_control/helper.py" ] || fail "helper missing from package"
[ -x "$HOME/.local/bin/tuf-control-gui" ] || fail "gui launcher missing"
[ -f "$XDG_DATA_HOME/gnome-shell/extensions/tuf-control-quick@hamza-gouda/extension.js" ] || fail "extension missing"
grep -q "^Exec=$HOME/.local/bin/tuf-control-gui$" \
    "$XDG_DATA_HOME/applications/io.github.hamzagouda.TufControl.desktop" || fail "Exec not rewritten"
grep -q "my own" "$XDG_STATE_HOME/tuf-control/backup/"* || fail "launcher not backed up"
"$HOME/.local/bin/tuf-control" --help >/dev/null || fail "installed CLI does not start"
TUF_CONTROL_FAKE_ROOT=$T/none "$HOME/.local/bin/tuf-control" --help >/dev/null || fail "CLI start with fake root"

"$ROOT/install.sh" --no-extension >/dev/null
"$ROOT/uninstall.sh" >/dev/null
[ ! -e "$XDG_DATA_HOME/tuf-control" ] || fail "app dir remains"
[ ! -e "$XDG_DATA_HOME/gnome-shell/extensions/tuf-control-quick@hamza-gouda" ] || fail "extension remains"
grep -q "my own" "$HOME/.local/bin/tuf-control" || fail "original launcher not restored"
[ ! -e "$HOME/.local/bin/tuf-control-gui" ] || fail "gui launcher remains"
echo "install tests passed"
