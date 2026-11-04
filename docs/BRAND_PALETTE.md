# Shared brand palette

Bordeaux GNOME's `tokens/tokens.toml` defines the shared colours. TUF Control generates
its CSS colour roles, Cairo palette and keyboard-effect variant accents from that file.
Both repositories carry the same display-free `tools/sync_tuf_palette.py` tool.

With both working copies available, run the tool from TUF Control with explicit paths:

```sh
python3 tools/sync_tuf_palette.py --source ../bordeaux-gnome --target .
python3 tools/sync_tuf_palette.py --source ../bordeaux-gnome --target . --check
```

Commit the generated `src/tuf_control/brand_palette.py` and the palette blocks in
`src/tuf_control/ui/style.css` and `style-dark.css` together. Surface roles map directly
to Bordeaux (dark) and Merlot-Day (light): windows use `bg-1`, cards use `bg-2`, raised
surfaces use `bg-3`, and accents, foregrounds and gold use their named tokens. Cairo
uses the same colours with drawing-specific opacity. TUF controls retain their own
layout and typography rules outside the generated blocks.

TUF Control CI checks these outputs against the owner's Bordeaux GNOME `main` branch
and checks that the two generator copies match. When changing shared tokens, update
both repositories and publish the Bordeaux source before TUF Control's generated copy.
No sibling checkout is needed at runtime or to run the fixture-based unit tests.
