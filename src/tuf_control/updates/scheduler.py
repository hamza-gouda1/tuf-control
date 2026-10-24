"""Background update checks for tuf-controld: at most every `interval_h` hours, one worker,
skipped on a metered connection, a notification only for updates not announced before."""
from __future__ import annotations

import threading
import time

from . import notify as notify_mod
from .check import check_in_child, load_cache, update_cache
from .model import Report

RETRY_S = 3600.0         # after a failed check
METERED_RETRY_S = 1800.0
FIRST_DELAY_S = 120.0    # let the session settle after login before the first check


def is_metered() -> bool:
    """NetworkManager's Metered property: 1 yes, 3 guess-yes. Unknown counts as not."""
    try:
        from .. import dbusutil
        v = dbusutil.get_property("system", "org.freedesktop.NetworkManager",
                                  "/org/freedesktop/NetworkManager",
                                  "org.freedesktop.NetworkManager", "Metered")
        return int(v) in (1, 3)
    except Exception:
        return False


class UpdateScheduler:
    def __init__(self, *, clock=time.time, runner=check_in_child, notifier=None,
                 metered=is_metered, first_delay_s: float = FIRST_DELAY_S) -> None:
        self.clock, self.runner, self.metered = clock, runner, metered
        self.notifier = notifier or self._notify
        self.settings: dict = {}
        self.worker: threading.Thread | None = None
        self.last_error: str | None = None
        self.skipped: str | None = None
        self.started_at = clock()
        self.first_delay_s = first_delay_s
        self.next_try = 0.0
        self.last_ts: float | None = None
        self._notify_id = 0
        self._lock = threading.Lock()

    def configure(self, settings: dict) -> None:
        self.settings = dict(settings or {})
        self._status = None

    @property
    def checking(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def _last_checked(self) -> float:
        """Newest of our own last check and the cache (the GUI may have checked)."""
        rep = load_cache().get("report") or {}
        return max(self.last_ts or 0.0, float(rep.get("checked_ts") or 0.0))

    def due(self, now: float | None = None) -> bool:
        now = self.clock() if now is None else now
        if not self.settings.get("enabled", True) or self.checking:
            return False
        if now < self.next_try or now - self.started_at < self.first_delay_s:
            return False
        interval = float(self.settings.get("interval_h", 6.0)) * 3600.0
        return now - self._last_checked() >= interval

    def tick(self, now: float | None = None) -> bool:
        """Start a check in the background when one is due. Returns True if started."""
        now = self.clock() if now is None else now
        if not self.due(now):
            return False
        if self.settings.get("respect_metered", True) and self.metered():
            self.skipped = "metered connection"
            self.next_try = now + METERED_RETRY_S
            return False
        self.skipped = None
        self.worker = threading.Thread(target=self._run, args=(now,), name="tuf-updates",
                                       daemon=True)
        self.worker.start()
        return True

    def _run(self, now: float) -> None:
        try:
            rep = self.runner()
        except Exception as exc:
            self.last_error = str(exc)
            self.next_try = now + RETRY_S
            return
        self.last_error = None
        self.last_ts = rep.checked_ts or now
        self.after_check(rep)
        self.status(refresh=True)

    def after_check(self, rep: Report) -> str | None:
        """Notification decision (shared with checks the GUI started). Returns the text
        shown, if any."""
        if not self.settings.get("notify", True):
            # still remember what was offered, so turning notifications on later does
            # not announce a backlog
            def mark(d):
                _t, d["notify"] = notify_mod.decide(rep, d.get("notify"))
            update_cache(mark)
            return None
        shown: dict = {}

        def upd(d):
            text, d["notify"] = notify_mod.decide(rep, d.get("notify"))
            shown["text"] = text
        with self._lock:
            update_cache(upd)
            if shown.get("text"):
                self.notifier(shown["text"], notify_mod.body_text(rep))
        return shown.get("text")

    def _notify(self, summary: str, body: str) -> None:
        self._notify_id = notify_mod.send(summary, body, self._notify_id)

    def status(self, refresh: bool = False) -> dict:
        """For the daemon's status file; the cache is re-read only when asked (each tick)."""
        if refresh or getattr(self, "_status", None) is None:
            rep = load_cache().get("report") or {}
            self._status = {"last_checked": rep.get("checked_ts"), "total": rep.get("total"),
                            "summary": rep.get("summary")}
        return {**self._status, "enabled": self.settings.get("enabled", True),
                "checking": self.checking, "error": self.last_error, "skipped": self.skipped}


def mark_seen(rep: Report) -> None:
    """A check the owner started and is looking at: remember its updates as announced so
    the daemon does not notify about them later."""
    def upd(d):
        _t, d["notify"] = notify_mod.decide(rep, d.get("notify"))
    update_cache(upd)
