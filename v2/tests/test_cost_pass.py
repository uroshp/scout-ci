"""Cost-pass invariants (2026-07-02, ~$8/day runs): the judge's slimmed targets digest, the
my_company 24h scan window (own-side cutoff = last_checked, never a held competitor window), and
the run-level by_role token ledger. Pure, no model/network.

    python -m unittest discover -s tests
"""
import json
import unittest
from unittest import mock

from scout import generate, monitor
from scout.propagate import _targets_digest


CLAIMS = [
    {"subject_key": "openai|flagship|current", "section": "battlecard", "zone": "where_they_win",
     "status": "active", "claim": "T " * 200},                       # long, targeted
    {"subject_key": "openai|other-play|current", "section": "battlecard", "zone": "where_we_win",
     "status": "active", "claim": "N " * 200},                       # long, NOT targeted
    {"subject_key": "openai|short|current", "section": "objection_handling", "zone": None,
     "status": "active", "claim": "short claim"},                    # short, NOT targeted
]


class TargetsDigestSlim(unittest.TestCase):
    def test_default_full_for_none_is_untouched_full_text(self):
        out = _targets_digest(CLAIMS)                                # the author path
        self.assertTrue(all("truncated" not in r["claim"] for r in out))
        self.assertEqual(out[0]["claim"], "T " * 200)

    def test_targeted_full_others_truncated(self):
        out = _targets_digest(CLAIMS, full_for={"openai|flagship|current"})
        by_key = {r["subject_key"]: r["claim"] for r in out}
        self.assertEqual(by_key["openai|flagship|current"], "T " * 200)      # target: full
        self.assertIn("truncated", by_key["openai|other-play|current"])      # long non-target: cut
        self.assertLessEqual(len(by_key["openai|other-play|current"]), 200)
        self.assertEqual(by_key["openai|short|current"], "short claim")      # short: left alone


class MyCompanyCutoff(unittest.TestCase):
    """The own-side scan window is the last successful check (~24h in daily ops) — NEVER the held
    unresolved_since window, which exists for competitor grounding retries (the John Jumper story
    was re-grounded on consecutive days through a held 6/27 window)."""

    def _capture_cutoffs(self, meta):
        captured = {}

        async def fake_triage(m, since, claims, my_since=None):
            captured["since"], captured["my_since"] = since, my_since
            return {"text": '```json\n{"has_candidates": false, "candidates": []}\n```',
                    "cost_usd": 0.0}

        store_stub = mock.MagicMock()
        store_stub.load_meta.return_value = meta
        store_stub.load_claims.return_value = []
        with mock.patch.object(monitor, "_run_triage", fake_triage), \
             mock.patch.object(monitor, "store", store_stub), \
             mock.patch.object(monitor, "_current_md", lambda slug: ""):
            monitor.check("test-slug", write=False)
        return captured

    def test_held_window_widens_competitor_side_only(self):
        got = self._capture_cutoffs({"last_checked": "2026-07-01T11:00:34",
                                     "unresolved_since": "2026-06-27",
                                     "unresolved_attempts": 1,
                                     "my_company": "Anthropic", "competitor": "OpenAI"})
        self.assertEqual(got["since"], "2026-06-27")       # competitor: held window honored
        self.assertEqual(got["my_since"], "2026-07-01")    # own side: last check only (~24h)

    def test_no_window_both_sides_use_last_checked(self):
        got = self._capture_cutoffs({"last_checked": "2026-07-01T11:00:34",
                                     "my_company": "Anthropic", "competitor": "OpenAI"})
        self.assertEqual(got["since"], "2026-07-01")
        self.assertEqual(got["my_since"], "2026-07-01")


class RoleTotalsLedger(unittest.TestCase):
    def setUp(self):
        generate.reset_role_totals()

    def tearDown(self):
        generate.reset_role_totals()

    def test_merge_and_reset(self):
        generate._merge_role_totals({"judge": {"input": 100, "output": 10, "cache_read": 5,
                                               "cache_creation": 50, "messages": 1}})
        generate._merge_role_totals({"judge": {"input": 30, "output": 3, "cache_read": 0,
                                               "cache_creation": 0, "messages": 1}})
        self.assertEqual(generate.ROLE_TOTALS["judge"]["input"], 130)
        self.assertEqual(generate.ROLE_TOTALS["judge"]["messages"], 2)
        generate.reset_role_totals()
        self.assertEqual(generate.ROLE_TOTALS, {})

    def test_cost_record_carries_by_role(self):
        generate._merge_role_totals({"triage": {"input": 7, "output": 1, "cache_read": 0,
                                                "cache_creation": 0, "messages": 1}})
        captured = {}
        from datetime import datetime
        with mock.patch.object(monitor.selfserve, "write_data",
                               side_effect=lambda p, body, m: captured.update(json.loads(body))):
            monitor._persist_run_cost(datetime(2026, 7, 2, 11, 0, 0),
                                      [{"slug": "s", "total": 1.0, "phases": {}}], write=True)
        self.assertEqual(captured["by_role"]["triage"]["input"], 7)


class UsdPerClaim(unittest.TestCase):
    """$/claim (2026-07-08, schema v2): the run record carries claims + usd_per_claim at both
    levels, and both emails surface the run's cost_note — spend-per-output is never ledger-only."""

    def _persist(self, rows):
        captured = {}
        from datetime import datetime
        with mock.patch.object(monitor.selfserve, "write_data",
                               side_effect=lambda p, body, m: captured.update(json.loads(body))):
            monitor._persist_run_cost(datetime(2026, 7, 8, 11, 0, 0), rows, write=True)
        return captured

    def test_run_level_usd_per_claim(self):
        doc = self._persist([{"slug": "a", "total": 3.0, "claims": 2, "phases": {}},
                             {"slug": "b", "total": 1.5, "claims": 1, "phases": {}}])
        self.assertEqual(doc["schema_version"], 2)
        self.assertEqual(doc["run_claims"], 3)
        self.assertEqual(doc["run_usd_per_claim"], 1.5)

    def test_zero_claims_is_none_not_division_crash(self):
        doc = self._persist([{"slug": "a", "total": 0.2, "claims": 0, "phases": {}}])
        self.assertEqual(doc["run_claims"], 0)
        self.assertIsNone(doc["run_usd_per_claim"])

    def test_rows_missing_claims_key_tolerated(self):
        # Old-shape rows (pre-metric) must not crash the ledger write.
        doc = self._persist([{"slug": "a", "total": 1.0, "phases": {}}])
        self.assertEqual(doc["run_claims"], 0)

    def test_digest_carries_cost_note(self):
        from scout import notify
        _, body = notify.render_digest(
            {"competitor": "OpenAI", "my_company": "Mistral"},
            [{"headline": "x", "severity": "act"}],
            cost_note="Run cost: $3.24 — 4 claims, $0.81/claim")
        self.assertIn("$0.81/claim", body)

    def test_proposals_email_carries_cost_note(self):
        from scout import notify
        _, body = notify.render_propagation_proposals(
            "slug", {"competitor": "OpenAI", "my_company": "Mistral"},
            [{"operation": "revise", "section": "snapshot", "subject_key": "k",
              "new_text": "n", "judge_verdict": "confirm"}],
            cost_note="Run cost: $3.24 — 4 claims, $0.81/claim")
        self.assertIn("$0.81/claim", body)


if __name__ == "__main__":
    unittest.main()
