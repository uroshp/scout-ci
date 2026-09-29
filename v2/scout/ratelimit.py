"""Soft rate limits for the viewer's paid endpoints (WS2, 2026-09-29): a per-minute burst limit
per client IP and a per-day quota per visitor id, in memory, per instance. They shape traffic and
stop a drive-by from spending the day's Ask ceiling before breakfast; the ledger in the engine
(a hard daily ceiling in the private store) remains the real bound, so a second viewer instance or
a restart can only ever let a few more questions through, never unbounded spend.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from datetime import date


class Limiter:
    def __init__(self, per_minute: int | None = None, per_day: int | None = None):
        self.per_minute, self.per_day = per_minute, per_day
        self._minute: dict[str, deque] = {}
        self._day: dict[tuple[str, str], int] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _today() -> str:
        return date.today().isoformat()

    def check(self, key: str, now: float | None = None) -> tuple[bool, str]:
        """Would one more request for `key` be allowed? (True, "") or (False, "minute" | "day").
        Counts nothing."""
        now = time.time() if now is None else now
        key = str(key or "?")
        with self._lock:
            return self._check(key, now)

    def _check(self, key: str, now: float) -> tuple[bool, str]:
        if self.per_minute is not None:
            q = self._minute.setdefault(key, deque())
            while q and now - q[0] >= 60:
                q.popleft()
            if len(q) >= self.per_minute:
                return False, "minute"
        if self.per_day is not None and self._day.get((key, self._today()), 0) >= self.per_day:
            return False, "day"
        return True, ""

    def hit(self, key: str, now: float | None = None) -> tuple[bool, str]:
        """Count one request for `key`. (True, "") when allowed; (False, "minute" | "day") when not.
        A refused request is not counted."""
        now = time.time() if now is None else now
        key = str(key or "?")
        with self._lock:
            ok, why = self._check(key, now)
            if not ok:
                return ok, why
            if self.per_minute is not None:
                self._minute[key].append(now)
            if self.per_day is not None:
                dk = (key, self._today())
                self._day[dk] = self._day.get(dk, 0) + 1
            if len(self._day) > 5000 or len(self._minute) > 5000:
                self._prune(now)
            return True, ""

    def _prune(self, now: float) -> None:
        today = self._today()
        for dk in [k for k in self._day if k[1] != today]:
            del self._day[dk]
        for k in [k for k, q in self._minute.items() if not q or now - q[-1] >= 60]:
            del self._minute[k]
