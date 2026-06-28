"""Scheduling + spend-gate invariants: the window-anchored due-gate, the viewer's next-check,
and the self-serve gate (the money guard). Run from v2/:  python -m unittest discover -s tests
"""
import json
import unittest
from datetime import datetime
from unittest import mock

from scout import config, display, selfserve
from scout import monitor


def _at(s):
    return datetime.fromisoformat(s)


class AnchoredDueGate(unittest.TestCase):
    """_is_due must release a card once per daily 7am window and never drift (anchored, not
    relative-elapsed). Assumes the default single anchor 11:00 UTC and Sunday skip."""

    def setUp(self):
        self.assertEqual(config.MONITOR_ANCHORS_UTC, ["11:00"])
        self.assertIn(6, config.MONITOR_SKIP_WEEKDAYS)   # Sunday skipped by default

    def test_due_only_after_an_unserved_anchor(self):
        meta = {"last_checked": "2026-06-05T11:33:00"}                      # served Friday's anchor
        self.assertFalse(monitor._is_due(meta, now=_at("2026-06-05T19:00:00")))  # same day
        self.assertTrue(monitor._is_due(meta, now=_at("2026-06-06T11:05:00")))   # next morning (Sat)

    def test_no_timestamp_is_due(self):
        self.assertTrue(monitor._is_due({}, now=_at("2026-06-05T09:00:00")))   # Friday

    def test_latest_passed_anchor_rolls_to_yesterday_before_first(self):
        self.assertEqual(
            monitor._latest_passed_anchor(_at("2026-06-05T10:00:00")),
            _at("2026-06-04T11:00:00"),                  # single anchor → yesterday's 11:00
        )

    def test_sunday_is_skipped(self):
        meta = {"last_checked": "2026-06-25T11:00:00"}                      # checked days ago
        self.assertFalse(monitor._is_due(meta, now=_at("2026-06-28T11:05:00")))  # 06-28 is Sunday
        self.assertTrue(monitor._is_due(meta, now=_at("2026-06-29T11:05:00")))   # Monday picks it up

    def test_weekly_cadence_holds_then_releases(self):
        meta = {"last_checked": "2026-06-25T11:00:00", "cadence_days": 7}   # Thu 06-25
        # Within the week: past the anchor but not enough days elapsed -> not due.
        self.assertFalse(monitor._is_due(meta, now=_at("2026-06-29T11:05:00")))  # Mon, 4 days
        # A full 7 days later -> due.
        self.assertTrue(monitor._is_due(meta, now=_at("2026-07-02T11:05:00")))   # Thu, 7 days


class NextCheckDisplay(unittest.TestCase):
    """The viewer's next_check must point at the next real anchor (not last+cadence), and
    unmonitored cards must show no countdown."""

    def test_next_check_is_next_anchor(self):
        cp = display.checkpoints({"last_checked": "2026-06-05T18:33:00", "monitored": True})
        self.assertEqual(cp["next_check"], "2026-06-06T11:00:00")

    def test_unmonitored_has_no_next_check(self):
        cp = display.checkpoints({"last_checked": "2026-06-05T18:33:00", "monitored": False})
        self.assertIsNone(cp["next_check"])

    def test_next_check_skips_sunday(self):
        cp = display.checkpoints({"last_checked": "2026-06-27T11:00:00", "monitored": True})  # Sat
        self.assertEqual(cp["next_check"], "2026-06-29T11:00:00")                              # -> Mon

    def test_next_check_honors_weekly_cadence(self):
        cp = display.checkpoints({"last_checked": "2026-06-25T11:00:00", "monitored": True,
                                  "cadence_days": 7})                                          # Thu
        self.assertEqual(cp["next_check"], "2026-07-02T11:00:00")                              # +7 days


class SpendGate(unittest.TestCase):
    """gate() is the money guard: it must close on the free-window count AND independently on the
    dollar ceiling, reserving one generation's headroom so spend can never cross the cap."""

    def _state(self, used=0, spend=0.0):
        s = selfserve.default_state()
        s["used"], s["spend_usd"] = used, spend
        return s

    def test_open_when_room_on_both(self):
        self.assertTrue(selfserve.gate(self._state())["open"])

    def test_closes_when_free_window_exhausted(self):
        g = selfserve.gate(self._state(used=config.SELFSERVE_FREE_LIMIT))
        self.assertFalse(g["open"])
        self.assertEqual(g["reason"], "window_closed")

    def test_ceiling_reserves_headroom(self):
        ceiling = config.SELFSERVE_SPEND_CEILING_USD
        headroom = config.GEN_MAX_BUDGET_USD
        # One generation's headroom below the ceiling -> still open; cross it -> closed.
        self.assertTrue(selfserve.gate(self._state(spend=ceiling - headroom - 1))["open"])
        g = selfserve.gate(self._state(spend=ceiling - headroom + 1))
        self.assertFalse(g["open"])
        self.assertEqual(g["reason"], "spend_ceiling")


class WatermarkHold(unittest.TestCase):
    """A SUBSTANTIAL development that survives nothing (no material captured) must NOT let the
    detection window advance past it — else it's lost forever (the original bug). Instead the
    window is HELD at `since` across up to MONITOR_MAX_UNRESOLVED_RETRIES checks, while
    last_checked still advances (due-gate stays honest). After the bound, give up but SURFACE it.
    No API: triage + materiality are stubbed; the store is in-memory."""

    def _check(self, state, *, substantial=True):
        cands = ([{"signal": "Acme files S-1 (2026-06-08)", "subject_key": "NEW",
                   "substantial": True, "why_new": "n", "source_hint": "reuters.com"}]
                 if substantial else [])
        triage = {"text": "```json\n" + json.dumps(
            {"has_candidates": bool(cands), "candidates": cands}) + "\n```", "cost_usd": 0.0}
        mat = {"text": "```json\n{\"material\": [], \"immaterial\": []}\n```", "cost_usd": 0.0}

        async def fake_triage(*a, **k):
            return triage

        async def fake_mat(*a, **k):
            return mat

        store_stub = mock.Mock()
        store_stub.load_meta.side_effect = lambda slug: dict(state["meta"])
        store_stub.load_claims.side_effect = lambda slug: list(state["claims"])

        def _wb(slug, claims, meta, md):
            state["meta"] = dict(meta)

        store_stub.write_baseline.side_effect = _wb
        with mock.patch.object(monitor, "_run_triage", fake_triage), \
             mock.patch.object(monitor, "_run_materiality", fake_mat), \
             mock.patch.object(monitor, "store", store_stub), \
             mock.patch.object(monitor, "_current_md", lambda slug: ""):
            return monitor.check("test-slug", write=True)

    def test_window_held_then_abandoned_but_never_silently_lost(self):
        state = {"meta": {"last_checked": "2026-06-08T17:00:00", "baseline_date": "2026-06-04"},
                 "claims": []}
        # Checks 1..(N-1): window HELD at the original date, last_checked still advances.
        for attempt in range(1, config.MONITOR_MAX_UNRESOLVED_RETRIES):
            res = self._check(state)
            self.assertEqual(res["since"], "2026-06-08")                 # window stays anchored
            self.assertEqual(state["meta"]["unresolved_attempts"], attempt)
            self.assertEqual(state["meta"]["unresolved_since"], "2026-06-08")
            self.assertNotEqual(state["meta"]["last_checked"], "2026-06-08T17:00:00")  # advanced
            self.assertIn("unresolved_held", res)
        # Final check: bound hit -> give up scanning, clear the hold, but SURFACE the abandonment.
        res = self._check(state)
        self.assertIn("abandoned_substantial", res)
        self.assertNotIn("unresolved_since", state["meta"])
        self.assertNotIn("unresolved_attempts", state["meta"])

    def test_quiet_window_clears_a_held_window(self):
        state = {"meta": {"last_checked": "2026-06-08T17:00:00", "baseline_date": "2026-06-04",
                          "unresolved_since": "2026-06-08", "unresolved_attempts": 1},
                 "claims": []}
        res = self._check(state, substantial=False)                      # nothing substantial now
        self.assertTrue(res["no_change"])
        self.assertNotIn("unresolved_since", state["meta"])
        self.assertNotIn("unresolved_attempts", state["meta"])


if __name__ == "__main__":
    unittest.main()
