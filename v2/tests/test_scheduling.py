"""Scheduling + spend-gate invariants: the window-anchored due-gate, the viewer's next-check,
and the self-serve gate (the money guard). Run from v2/:  python -m unittest discover -s tests
"""
import unittest
from datetime import datetime

from scout import config, display, selfserve
from scout import monitor


def _at(s):
    return datetime.fromisoformat(s)


class AnchoredDueGate(unittest.TestCase):
    """_is_due must release a card once per 7am/1pm window and never drift (anchored, not
    relative-elapsed). Assumes the default anchors 11:00/17:00 UTC."""

    def setUp(self):
        self.assertEqual(config.MONITOR_ANCHORS_UTC, ["11:00", "17:00"])

    def test_due_only_after_an_unserved_anchor(self):
        meta = {"last_checked": "2026-06-05T18:33:00"}                      # served the 1pm window
        self.assertFalse(monitor._is_due(meta, now=_at("2026-06-05T19:00:00")))  # same window
        self.assertTrue(monitor._is_due(meta, now=_at("2026-06-06T11:05:00")))   # next morning anchor

    def test_no_timestamp_is_due(self):
        self.assertTrue(monitor._is_due({}, now=_at("2026-06-05T09:00:00")))

    def test_latest_passed_anchor_rolls_to_yesterday_before_first(self):
        self.assertEqual(
            monitor._latest_passed_anchor(_at("2026-06-05T10:00:00")),
            _at("2026-06-04T17:00:00"),
        )


class NextCheckDisplay(unittest.TestCase):
    """The viewer's next_check must point at the next real anchor (not last+cadence), and
    unmonitored cards must show no countdown."""

    def test_next_check_is_next_anchor(self):
        cp = display.checkpoints({"last_checked": "2026-06-05T18:33:00", "monitored": True})
        self.assertEqual(cp["next_check"], "2026-06-06T11:00:00")

    def test_unmonitored_has_no_next_check(self):
        cp = display.checkpoints({"last_checked": "2026-06-05T18:33:00", "monitored": False})
        self.assertIsNone(cp["next_check"])


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


if __name__ == "__main__":
    unittest.main()
