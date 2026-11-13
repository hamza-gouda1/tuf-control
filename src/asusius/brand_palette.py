# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Generated from hamza-gouda1/bordeaux-gnome tokens/tokens.toml.

Regenerate with tools/sync_tuf_palette.py; do not edit the shared values here.
This module is display-free and can also be used by keyboard effects.
"""

HEX_PALETTES = {
    "dark": {
        "accent": "#8E2A44",
        "accent-text": "#E09AAE",
        "bg-1": "#151112",
        "bg-2": "#1C1718",
        "bg-3": "#262021",
        "fg-0": "#EEE7E4",
        "fg-1": "#C3B8B5",
        "gold": "#C9AE86",
        "on-accent": "#FFF3F1",
        "sidebar": "#120E0F"
    },
    "light": {
        "accent": "#7A1C34",
        "accent-text": "#7A1C34",
        "bg-1": "#F8F4EE",
        "bg-2": "#FDFBF7",
        "bg-3": "#E6DDCF",
        "fg-0": "#2B1A1E",
        "fg-1": "#5A454A",
        "gold": "#A8844C",
        "on-accent": "#FFF7F2",
        "sidebar": "#EFE8DD"
    }
}
ACCENTS = {
    "bordeaux": "#8E2A44",
    "oxblood-noir": "#A3283A",
    "merlot-day": "#7A1C34"
}


def rgb(value: str) -> tuple[float, float, float]:
    return tuple(int(value[i:i + 2], 16) / 255 for i in (1, 3, 5))


BURGUNDY = rgb(ACCENTS["bordeaux"])
GOLD = rgb(HEX_PALETTES["dark"]["gold"])

# Cairo drawing roles use the same token colours as the CSS surfaces.
PALETTE = {
    scheme: {"line": rgb(p["accent-text"]), "fill": rgb(p["accent"]),
             "grid": (*rgb(p["fg-0"]), 0.07 if scheme == "dark" else 0.08),
             "dim": (*rgb(p["fg-1"]), 0.75), "gold": rgb(p["gold"]),
             "point": rgb(p["on-accent"] if scheme == "dark" else p["bg-2"])}
    for scheme, p in HEX_PALETTES.items()
}
