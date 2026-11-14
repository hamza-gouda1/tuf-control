# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Lightweight persisted UI preferences (appearance scheme, temperature unit)."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

VALID_SCHEMES = ("system", "dark", "light")
VALID_TEMP_UNITS = ("C", "F")
_DEFAULTS = {"scheme": "system", "temp_unit": "C", "dismissed_setup": False}
_cache: dict | None = None
_cache_path: Path | None = None


def prefs_path() -> Path:
    cfg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(cfg) / "asusius" / "ui.json"


def load_prefs() -> dict:
    global _cache, _cache_path
    path = prefs_path()
    if _cache is not None and _cache_path == path:
        return dict(_cache)
    data = dict(_DEFAULTS)
    try:
        raw = json.loads(path.read_text())
        if isinstance(raw, dict):
            if raw.get("scheme") in VALID_SCHEMES:
                data["scheme"] = raw["scheme"]
            if raw.get("temp_unit") in VALID_TEMP_UNITS:
                data["temp_unit"] = raw["temp_unit"]
            if isinstance(raw.get("dismissed_setup"), bool):
                data["dismissed_setup"] = raw["dismissed_setup"]
    except (OSError, ValueError):
        pass
    _cache = data
    _cache_path = path
    return dict(data)


def save_prefs(**updates) -> dict:
    global _cache, _cache_path
    data = load_prefs()
    for key, val in updates.items():
        if key == "scheme" and val in VALID_SCHEMES:
            data[key] = val
        elif key == "temp_unit" and val in VALID_TEMP_UNITS:
            data[key] = val
        elif key == "dismissed_setup":
            data[key] = bool(val)
    path = prefs_path()
    _cache = dict(data)
    _cache_path = path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".ui-", suffix=".json", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh, indent=2)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError:
        pass
    return dict(data)


def get_temp_unit() -> str:
    return load_prefs()["temp_unit"]


def get_scheme() -> str:
    return load_prefs()["scheme"]
