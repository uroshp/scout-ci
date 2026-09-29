"""A spend ledger with a hard daily ceiling (WS2, 2026-09-28), the self-serve gate's idea made
reusable: refuse to START a run that could cross the ceiling, so total spend can never exceed it.

State lives in the private store (`ask/state.json`, under the RC prefix on RC) and is updated with
optimistic concurrency (selfserve.update_data), so two engine instances cannot both take the last
dollar. `start()` reserves the per-question cap; `settle()` replaces the reservation with the
actual cost. The day rolls over on the UTC date.
"""
from __future__ import annotations

import json
from datetime import date

from scout import selfserve


class Ledger:
    def __init__(self, path: str, ceiling_usd: float, reserve_usd: float):
        self.path, self.ceiling, self.reserve = path, float(ceiling_usd), float(reserve_usd)

    @staticmethod
    def _today() -> str:
        return date.today().isoformat()

    def _fresh(self, cur: dict | None) -> dict:
        s = dict(cur or {})
        if s.get("day") != self._today():
            s = {"day": self._today(), "spend_usd": 0.0, "in_flight_usd": 0.0, "questions": 0, "refused": 0}
        s.setdefault("spend_usd", 0.0); s.setdefault("in_flight_usd", 0.0); s.setdefault("questions", 0); s.setdefault("refused", 0)
        s["ceiling_usd"] = self.ceiling
        return s

    def read(self) -> dict:
        try:
            raw = selfserve.read_data(self.path)
            return self._fresh(json.loads(raw) if raw else None)
        except Exception:
            return self._fresh(None)

    def room(self, state: dict | None = None) -> float:
        s = state or self.read()
        return round(self.ceiling - s["spend_usd"] - s["in_flight_usd"], 4)

    def start(self, reserve: float | None = None) -> tuple[bool, dict]:
        """Reserve one question's cap (the default, or a smaller one for a cheaper path). (True,
        state) if it fits under the ceiling, else (False, state)."""
        out: dict = {}
        reserve = self.reserve if reserve is None else float(reserve)

        def tx(cur_text):
            s = self._fresh(json.loads(cur_text) if cur_text else None)
            if s["spend_usd"] + s["in_flight_usd"] + reserve > self.ceiling:
                s["refused"] += 1
                out["ok"] = False
            else:
                s["in_flight_usd"] = round(s["in_flight_usd"] + reserve, 4)
                s["questions"] += 1
                out["ok"] = True
            out["state"] = s
            return json.dumps(s, indent=1)

        try:
            selfserve.update_data(self.path, tx, "ask: ledger start")
        except Exception:
            # the store is unreachable: fail CLOSED (no spend without a ledger)
            return False, self._fresh(None)
        return bool(out.get("ok")), out.get("state", {})

    def settle(self, actual_usd: float, reserve: float | None = None) -> None:
        """Replace this question's reservation with what it actually cost. Never raises."""
        reserve = self.reserve if reserve is None else float(reserve)

        def tx(cur_text):
            s = self._fresh(json.loads(cur_text) if cur_text else None)
            s["in_flight_usd"] = round(max(0.0, s["in_flight_usd"] - reserve), 4)
            s["spend_usd"] = round(s["spend_usd"] + float(actual_usd or 0.0), 4)
            return json.dumps(s, indent=1)
        try:
            selfserve.update_data(self.path, tx, "ask: ledger settle")
        except Exception:
            pass
