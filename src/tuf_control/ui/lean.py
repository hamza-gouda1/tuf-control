# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Process-wide memory discipline for the GUI.

Renderer: GTK's GL renderers load the whole Mesa stack (libgallium + libLLVM) and, through
GLVND, NVIDIA's EGL libraries: about 110 MB of resident memory in this process for an app
that redraws a few small charts once or twice a second. The Cairo renderer draws the same
widgets in software with none of that, so it is the default here. Set GSK_RENDERER
yourself (or TUF_CONTROL_RENDERER=gl) to choose another; docs/PERFORMANCE.md has numbers.
"""
from __future__ import annotations

import ctypes
import os

RENDERER_ENV = "TUF_CONTROL_RENDERER"


def choose_renderer() -> str:
    """Call before GTK opens the display. Returns the renderer in effect."""
    if os.environ.get("GSK_RENDERER"):
        return os.environ["GSK_RENDERER"]
    want = os.environ.get(RENDERER_ENV, "cairo").strip().lower()
    if want in ("", "default", "gl", "ngl", "vulkan"):
        if want in ("ngl", "vulkan"):
            os.environ["GSK_RENDERER"] = want
        return want or "default"
    os.environ["GSK_RENDERER"] = "cairo"
    return "cairo"


_libc = None


def trim() -> None:
    """Give freed heap pages back to the kernel (glibc keeps them otherwise)."""
    global _libc
    try:
        if _libc is None:
            _libc = ctypes.CDLL("libc.so.6")
        _libc.malloc_trim(0)
    except (OSError, AttributeError):
        pass
