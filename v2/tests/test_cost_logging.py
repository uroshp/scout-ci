"""Cost-logging invariants: every monitor run persists a per-run cost record, and every
propagation decision log carries its propose+judge cost. Spend must be reviewable later."""
import json
import unittest
from datetime import datetime
from unittest import mock

from scout import monitor, propagate, selfserve


class TestRunTotal(unittest.TestCase):
    def test_sums_all_phases_none_safe(self):
        self.assertEqual(
            monitor._run_total({"triage": 0.01, "materiality": 0.02,
                                "propagation": None, "strategy": 0.5}), 0.53)

    def test_empty_and_none(self):
        self.assertEqual(monitor._run_total({}), 0)
        self.assertEqual(monitor._run_total(None), 0)


class TestPropagationLogCarriesCost(unittest.TestCase):
    def test_payload_includes_cost_usd(self):
        captured = {}

        def fake_write(path, text, message):
            captured["path"], captured["text"] = path, text

        with mock.patch.object(selfserve, "write_data", fake_write):
            propagate.log_decisions("acme__vs__bco__x", [{"subject_key": "k"}],
                                    cost={"propose": 0.1, "judge": 0.2})
        payload = json.loads(captured["text"])
        self.assertEqual(payload["cost_usd"], {"propose": 0.1, "judge": 0.2})
        self.assertTrue(captured["path"].startswith("propagation/acme__vs__bco__x/"))


class TestPersistRunCost(unittest.TestCase):
    def test_write_false_is_noop(self):
        with mock.patch.object(selfserve, "write_data") as w:
            monitor._persist_run_cost(datetime(2026, 1, 1, 1, 1, 1),
                                      [{"slug": "a", "total": 0.1}], write=False)
            w.assert_not_called()

    def test_no_rows_is_noop(self):
        with mock.patch.object(selfserve, "write_data") as w:
            monitor._persist_run_cost(datetime(2026, 1, 1, 1, 1, 1), [], write=True)
            w.assert_not_called()

    def test_write_persists_run_total_and_cards(self):
        captured = {}

        def fake_write(path, text, message):
            captured["path"], captured["text"] = path, text

        rows = [{"slug": "a", "total": 0.10, "phases": {"triage": 0.10}},
                {"slug": "b", "total": 0.25, "phases": {"triage": 0.05, "propagation": 0.20}}]
        with mock.patch.object(selfserve, "write_data", fake_write):
            monitor._persist_run_cost(datetime(2026, 6, 28, 17, 5, 50), rows, write=True)
        doc = json.loads(captured["text"])
        self.assertEqual(captured["path"], "costs/20260628T170550.json")
        self.assertEqual(doc["run_total_usd"], 0.35)
        self.assertEqual(len(doc["cards"]), 2)
        self.assertIn("run_ts", doc)
        self.assertIn("mode", doc)


if __name__ == "__main__":
    unittest.main()
