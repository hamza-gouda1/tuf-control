#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Generate the shared TUF palette from Bordeaux tokens, without GTK or installation.

    python3 tools/sync_tuf_palette.py --source ../bordeaux-gnome --target . [--check]

Both repository paths are explicit. Only generated palette files in target are written.
"""
from __future__ import annotations

import argparse
import json
import re
import tomllib
from pathlib import Path

BEGIN = "/* BEGIN GENERATED BORDEAUX PALETTE */"
END = "/* END GENERATED BORDEAUX PALETTE */"
ROLE_TOKENS = {
    "accent_bg_color": "accent", "accent_fg_color": "on-accent",
    "accent_color": "accent-text", "gold_color": "gold",
    "window_bg_color": "bg-1", "window_fg_color": "fg-0",
    "view_bg_color": "bg-2", "view_fg_color": "fg-0",
    "headerbar_bg_color": "bg-1", "headerbar_fg_color": "fg-0",
    "headerbar_backdrop_color": "bg-1", "sidebar_bg_color": "sidebar",
    "sidebar_fg_color": "fg-1", "sidebar_backdrop_color": "sidebar",
    "card_bg_color": "bg-2", "card_fg_color": "fg-0",
    "popover_bg_color": "bg-3", "popover_fg_color": "fg-0",
    "dialog_bg_color": "bg-2", "dialog_fg_color": "fg-0",
    "thumbnail_bg_color": "bg-3",
}
TOKENS = sorted(set(ROLE_TOKENS.values()) | {"fg-1"})


def source_palettes(source: Path) -> tuple[dict, dict]:
    data = tomllib.loads((source / "tokens/tokens.toml").read_text())
    palettes = {}
    for scheme, variant in (("dark", "Bordeaux"), ("light", "Merlot-Day")):
        p = data["variant"][variant]["palette"]
        palettes[scheme] = {key: p[key].upper() for key in TOKENS}
    accents = {name.lower(): item["palette"]["accent"].upper()
               for name, item in data["variant"].items()}
    for value in [v for p in palettes.values() for v in p.values()] + list(accents.values()):
        if not re.fullmatch(r"#[0-9A-F]{6}", value):
            raise ValueError(f"shared palette requires opaque hex colours: {value!r}")
    return palettes, accents


def python_palette(palettes: dict, accents: dict) -> str:
    return '''# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Generated from hamza-gouda1/bordeaux-gnome tokens/tokens.toml.

Regenerate with tools/sync_tuf_palette.py; do not edit the shared values here.
This module is display-free and can also be used by keyboard effects.
"""
''' + "\nHEX_PALETTES = " + json.dumps(palettes, indent=4) + \
        "\nACCENTS = " + json.dumps(accents, indent=4) + '''


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
'''


def css_palette(original: str, palette: dict, scheme: str) -> str:
    lines = [BEGIN]
    lines += [f"@define-color {role} {palette[token]};" for role, token in ROLE_TOKENS.items()]
    alpha = "0.06" if scheme == "dark" else "0.07"
    lines += ["@define-color banner_bg_color alpha(@accent_bg_color, 0.12);",
              f"@define-color tile_border alpha({palette['fg-0']}, {alpha});", END]
    block = "\n".join(lines)
    if BEGIN in original:
        start = original.index(BEGIN)
        stop = original.index(END, start) + len(END)
    else:
        definitions = list(re.finditer(r"^@define-color .+;$", original, re.M))
        if not definitions:
            raise ValueError("target stylesheet has no palette block")
        start, stop = definitions[0].start(), definitions[-1].end()
    return original[:start] + block + original[stop:]


def generated_files(source: Path, target: Path) -> dict[Path, str]:
    palettes, accents = source_palettes(source)
    ui = target / "src/tuf_control/ui"
    outputs = {target / "src/tuf_control/brand_palette.py": python_palette(palettes, accents)}
    for scheme, filename in (("light", "style.css"), ("dark", "style-dark.css")):
        path = ui / filename
        outputs[path] = css_palette(path.read_text(), palettes[scheme], scheme)
    return outputs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", required=True, type=Path, help="Bordeaux repository")
    ap.add_argument("--target", required=True, type=Path, help="TUF Control repository")
    ap.add_argument("--check", action="store_true", help="report stale files without writing")
    args = ap.parse_args(argv)
    stale = []
    for path, text in generated_files(args.source, args.target).items():
        if path.exists() and path.read_text() == text:
            continue
        stale.append(path)
        if args.check:
            print(f"stale shared palette: {path}")
        else:
            path.write_text(text)
    return 1 if args.check and stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
