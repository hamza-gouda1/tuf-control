# Release checks

Reviewed on 2026-11-04. The check uses fake hardware and recorded system-service
transactions; no package updates, firmware flashes or hardware writes were performed.

- Unit suite: 486 tests run, two intentional skips.
- Full UI matrix: all 21 pages, dialogs and transient states across 27 combinations
  (five sizes, three themes, and six stress modes at two sizes). Zero failures and
  warnings; 42 grouped informational notes record shortened labels with tooltips.
  The matrix combines sequential bounded runs and affected-case reruns after fixes.
- Installer: temporary-home round trips, original asset/service restoration, and
  literal paths containing shell-special characters pass.
- Extension logic: 36 profile assertions and 293 gesture assertions pass.
- Focused Shell QA covers reversible desktop travel, app switching, overview handoff,
  reduced motion and media dispatch, using existing window actors.
- Desktop entry and AppStream metadata validate; extension schemas validate.
- ShellCheck and Python syntax checks pass. Ruff is unavailable locally; its CI
  job must pass after publishing before a release is announced.
- Tracked-text scan found no real home paths or obvious credential patterns.

Update results use installed versions read before and after a transaction. Firmware
is reported as pending a restart only with fwupd staging evidence. Dependency
installation rejects held packages, removal plans and a plan changed since
confirmation. Cancellation is checked before submitting package changes, before
firmware flashing, and between firmware devices. An active flash finishes safely.

Real authenticated PackageKit/fwupd installations, physical shortcut mappings,
physical gesture feel and support on other laptop models remain unverified.
The earlier single-machine footprint measurements are documented in PERFORMANCE.md;
they are not a controlled comparison with another application.

## Cutting a release

Before tagging, move shipped changes from `[Unreleased]` to a dated version section in
`CHANGELOG.md` and update the AppStream `<releases>` version/date to the publication date.
Verify version sync (`pyproject.toml`, `src/asusius/__init__.py`, `packaging/arch/PKGBUILD`,
`data/Asusius.metainfo.xml`, `CHANGELOG.md`), desktop/AppStream validation, `python3 -m build`,
unit tests, `ruff`, `shellcheck`, `tests/test_install.sh`, and `gjs` extension tests on the exact
commit to be tagged (and make sure CI passes on GitHub before publishing).

Then create a new annotated tag (`git tag -a vX.Y.Z -m "Asusius vX.Y.Z"`), build the clean
deterministic source archive:

    rm -rf dist
    python3 tools/release_archive.py --tag vX.Y.Z --out dist
    (cd dist && sha256sum -c SHA256SUMS)

Publish the `dist/asusius-X.Y.Z.tar.gz` and `dist/SHA256SUMS` assets via the `Release archives`
workflow (or upload directly), then pin `sha256sums` in `packaging/arch/PKGBUILD` before updating
the AUR package. Keep new development under `[Unreleased]`; published tags and archives remain
immutable.

### GitHub maintenance notes (from `v0.2.0` release audit)

- **`v0.2.0` GitHub Release (`7b0caa3`)**: tagged 4 commits behind `main` (`5241630`) while CI
  (`brand-palette` + `Unit tests`) was failing, and its release notes still reference
  `tuf-control-0.2.0.tar.gz` with a mismatched SHA256 checksum. Update/mark `v0.2.0` superseded
  once `v0.2.1` is published from merged, green `main`.
- **Open GitHub Issue `#3`**: still advises users to run `tuf-control capabilities --json`;
  update the comment/instructions to `asusius capabilities --json`.
