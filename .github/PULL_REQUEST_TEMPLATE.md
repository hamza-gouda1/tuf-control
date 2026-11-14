## What changed

## Checked
- [ ] `PYTHONPATH=src python3 -m unittest discover -s tests -v` passes
- [ ] `python3 -m ruff check src tests` passes
- [ ] `shellcheck -S warning install.sh uninstall.sh tests/test_install.sh && bash tests/test_install.sh` passes
- [ ] `gjs -m tests/test_quick_profiles.js && gjs -m tests/test_gestures.js` passes
- [ ] `desktop-file-validate data/Asusius.desktop` and `appstreamcli validate --no-net data/Asusius.metainfo.xml` pass
- [ ] No test writes to real hardware (fake tree or fake transport only)
- [ ] Screenshots attached for UI changes
