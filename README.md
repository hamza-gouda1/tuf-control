# tuf-control

Control app for ASUS TUF/ROG laptops on Linux. This repository currently holds the
non-UI core (models, backends, CLI); a GTK4 + Libadwaita interface follows.

## Goals

- Safe by construction: every write is validated against the firmware's own limits.
- Never block a UI thread: D-Bus calls carry timeouts and have a thread-pool helper.
- Testable without hardware through a fake sysfs tree.

## Features

| Feature | Status |
| --- | --- |
| Platform profile | placeholder |
| Power limits | placeholder |
| Battery charge limit and health | placeholder |
| GPU mode | placeholder |
| Fan curves | placeholder |
| Keyboard backlight | placeholder |

## Install

Placeholder.

## Development

    PYTHONPATH=src python -m unittest discover -s tests   # or: pytest
    PYTHONPATH=src python -m tuf_control status

Set `TUF_CONTROL_FAKE_ROOT` to a fake tree (see `tuf_control.backends.fake`) to run
without touching hardware.

## License

GPL-3.0-or-later. Copyright Hamza Gouda.
