# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Bordeaux GNOME theme accent, read-only.

Order: ~/.config/bordeaux-gnome/variant, ~/.local/state/bordeaux-gnome/variant (written by
the Bordeaux installer), then the GNOME Shell user-theme name from gsettings (read with
`gsettings get`, never set)."""
from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from ..brand_palette import ACCENTS
from .colour import RGB, hex_to_rgb

_ALIASES = {"noir": "oxblood-noir", "oxblood": "oxblood-noir", "merlot": "merlot-day"}
_USER_THEME_SCHEMA = "org.gnome.shell.extensions.user-theme"
_MISSING_SCHEMA = False


def normalise_variant(name: str | None) -> str | None:
    if not name:
        return None
    key = name.strip().strip("'\"").lower().replace("_", "-").replace(" ", "-")
    key = _ALIASES.get(key, key)
    return key if key in ACCENTS else None


def _gsettings_theme() -> str | None:
    global _MISSING_SCHEMA
    try:
        from gi.repository import Gio
        src = Gio.SettingsSchemaSource.get_default()
        if src is None or src.lookup(_USER_THEME_SCHEMA, True) is None:
            return None
        return Gio.Settings.new(_USER_THEME_SCHEMA).get_string("name") or None
    except Exception:
        pass
    if _MISSING_SCHEMA or not shutil.which("gsettings"):
        return None
    try:
        out = subprocess.run(["gsettings", "get", _USER_THEME_SCHEMA,
                              "name"], capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        if "No such schema" in (out.stderr or ""):
            _MISSING_SCHEMA = True
        return None
    return out.stdout.strip()


def theme_variant(home: Path | None = None,
                  shell_theme: Callable[[], str | None] = _gsettings_theme) -> str | None:
    home = home or Path.home()
    cfg = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    state = Path(os.environ.get("XDG_STATE_HOME") or home / ".local/state")
    for f in (cfg / "bordeaux-gnome/variant", state / "bordeaux-gnome/variant"):
        try:
            v = normalise_variant(f.read_text(encoding="utf-8"))
        except OSError:
            continue
        if v:
            return v
    return normalise_variant(shell_theme())


def theme_accent(home: Path | None = None,
                 shell_theme: Callable[[], str | None] = _gsettings_theme) -> RGB | None:
    v = theme_variant(home, shell_theme)
    return hex_to_rgb(ACCENTS[v]) if v else None
