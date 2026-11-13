# Contributing

Open an issue describing the problem and the relevant laptop model, distribution,
kernel and asusd/supergfxd versions. Remove usernames, serials and other private data
from logs. Report security issues privately as described in [SECURITY.md](SECURITY.md).

Keep changes focused. Use Python 3.12, existing style and GPL-3.0-or-later headers with
Copyright (C) Hamza Gouda. Run:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
python3 -m ruff check src tests
bash tests/test_install.sh
shellcheck -S warning install.sh uninstall.sh tests/*.sh
gjs -m tests/test_quick_profiles.js
gjs -m tests/test_gestures.js
```

Use fake hardware (`ASUSIUS_FAKE_ROOT`) for development. Tests must not change
hardware, real desktop settings or install updates. GUI QA instructions and memory
limits are in [docs/UI.md](docs/UI.md). Include a regression test for behavioral fixes,
and fake screenshots for visible changes. Explain what you tested and any limits.
