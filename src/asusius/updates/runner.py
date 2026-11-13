# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""One update run end to end: apply through the system services with live state,
re-check what is actually installed, verify item by item and write the history entry.

Used by the Updates page and `asusius updates apply`; never claims success a backend
did not prove: every item ends as installed / on-restart / still pending (with apt's
reason) / failed (with the backend's error).
"""
from __future__ import annotations

import time

from . import actions, verify
from .model import Report, UpdateItem
from .progress import RunState


def _watch(state: RunState, methods: list[str]) -> dict:
    return {m: state.watcher(m) for m in methods}


def _fresh_run() -> None:
    """A fresh run forgets what a cancelled earlier fake run skipped."""
    import os
    from .fake import FAKE_SKIP_ENV
    os.environ.pop(FAKE_SKIP_ENV, None)


def apply_run(items: list[UpdateItem], state: RunState | None = None,
              trigger: str = "manual", check_fn=None) -> dict:
    """Apply `items`, then re-check and verify. Returns {"outcomes", "report", "results",
    "summary", "run"}; the run dict is also appended to the history."""
    from . import history
    state = state or RunState()
    _fresh_run()
    t0 = time.time()
    methods = list(actions.plan(items))
    watch = _watch(state, methods + ["packagekit-deps"])
    for m in methods:
        state.update(m, phase="Waiting", percent=None, package="", done=False)
    before = verify.before_versions(items)
    outcomes = actions.apply(items, watch=watch, cancel=state.cancel)
    rep = _recheck(check_fn)
    after = verify.installed_versions(items)
    results = verify.verify(items, before, after, outcomes, rep,
                            cancelled=state.cancel.is_set(), staged=verify.staged_firmware(items))
    summary = verify.summary(results)
    run = _run_dict(items, results, t0, trigger, summary, rep, state.cancel.is_set())
    history_error = _save_history(history, run, state)
    if history_error:
        summary += "; history could not be saved"
    state.finish(summary)
    return {"outcomes": outcomes, "report": rep, "results": results, "summary": summary,
            "run": run, "history_error": history_error}


def deps_run(item: UpdateItem, state: RunState | None = None, trigger: str = "manual",
             check_fn=None) -> dict:
    """The "Install with dependencies" path for one kept-back item."""
    from . import history
    state = state or RunState()
    _fresh_run()
    t0 = time.time()
    state.update("packagekit-deps", phase="Waiting", percent=None, package="", done=False)
    watch = _watch(state, ["packagekit-deps"])
    before = verify.before_versions([item])
    outcomes = actions.apply_deps(item, watch=watch, cancel=state.cancel)
    rep = _recheck(check_fn)
    after = verify.installed_versions([item])
    results = verify.verify([item], before, after, outcomes, rep,
                            cancelled=state.cancel.is_set(), staged=verify.staged_firmware([item]))
    summary = verify.summary(results)
    run = _run_dict([item], results, t0, trigger, summary, rep, state.cancel.is_set())
    history_error = _save_history(history, run, state)
    if history_error:
        summary += "; history could not be saved"
    state.finish(summary)
    return {"outcomes": outcomes, "report": rep, "results": results, "summary": summary,
            "run": run, "history_error": history_error}


def _recheck(check_fn):
    if check_fn is not None:
        try:
            return check_fn()
        except Exception:
            return None
    from .check import run_check
    try:
        return run_check()
    except Exception:
        return None


def _run_dict(items: list[UpdateItem], results: list[dict], t0: float, trigger: str,
              summary: str, rep: Report | None, cancelled: bool) -> dict:
    return {"start": t0, "end": time.time(), "trigger": trigger,
            "duration_s": round(time.time() - t0, 1),
            "sources": sorted({i.source for i in items}), "cancelled": cancelled,
            "reboot": bool(rep.reboot_required if rep else False)
            or any(r["result"] == "restart" for r in results),
            "summary": summary, "items": results}


def _save_history(history, run: dict, state: RunState) -> str:
    try:
        history.append(run)
    except (OSError, ValueError) as exc:
        error = str(exc)[:300]
        state.line("history", "Could not save update history: " + error)
        return error
    return ""
