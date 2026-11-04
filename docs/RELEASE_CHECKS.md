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
- Extension logic: 36 profile assertions and 160 gesture assertions pass.
- Focused Shell animation QA passes desktop hide/restore, rapid reversal, overview
  handoff and reduced motion, with no additional window actors during the fade.
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
Keep new development under `[Unreleased]`. Run the complete gates on the exact commit to
be tagged, then use a new annotated tag; published tags and archives remain immutable.
