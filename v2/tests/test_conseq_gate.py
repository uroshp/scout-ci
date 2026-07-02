"""Consequentiality-gate invariants (piping shipped 2026-07-02, dormant until the monitor.yml env
flip): in gate mode an EXPLICIT routine verdict from the router defers the paid authoring stages —
ops recorded as gated_routine, never silent; fail-OPEN on a missing verdict; shadow mode is
byte-identical to today. All model calls mocked. Run from v2/:

    python -m unittest discover -s tests
"""
import unittest
from unittest import mock

from scout import config, notify, propagate
from tests.test_propagate_rewrite import CLAIMS, FACT, SURFACE_OP, _authored, _fake, _verdict


def _route_fake(surface_ops, run_verdict):
    return lambda meta, fwa, claims: {"surface_ops": surface_ops, "no_surface": [],
                                      "run_verdict": run_verdict, "cost_usd": 0.11, "raw": ""}


def _run(run_verdict, mode):
    saved = config.CONSEQUENTIAL_FILTER
    config.CONSEQUENTIAL_FILTER = mode
    try:
        with mock.patch.object(propagate, "route", _route_fake([dict(SURFACE_OP)], run_verdict)), \
             mock.patch.object(propagate, "_run_author", _fake(_authored("prose"))), \
             mock.patch.object(propagate, "_run_judge", _fake(_verdict("confirm", "ok"))):
            return propagate.propagate({"competitor": "OpenAI", "my_company": "Anthropic"},
                                       [{"fact": FACT, "alert": {"severity": "act"}}], [],
                                       CLAIMS, slug=None, persist=False)
    finally:
        config.CONSEQUENTIAL_FILTER = saved


ROUTINE = {"consequential": False, "consequence_rationale": "tense refresh only",
           "headline": None}


class GateBranch(unittest.TestCase):
    def test_routine_in_gate_mode_defers_without_paid_stages(self):
        saved = config.CONSEQUENTIAL_FILTER
        config.CONSEQUENTIAL_FILTER = "gate"
        author, judge = mock.MagicMock(), mock.MagicMock()
        try:
            with mock.patch.object(propagate, "route", _route_fake([dict(SURFACE_OP)], ROUTINE)), \
                 mock.patch.object(propagate, "_run_author", author), \
                 mock.patch.object(propagate, "_run_judge", judge):
                res = propagate.propagate({"competitor": "OpenAI", "my_company": "Anthropic"},
                                          [{"fact": FACT, "alert": {"severity": "act"}}], [],
                                          CLAIMS, slug=None, persist=False)
        finally:
            config.CONSEQUENTIAL_FILTER = saved
        author.assert_not_called()
        judge.assert_not_called()
        self.assertEqual(res["gated"], "routine")
        self.assertEqual(res["confirmed"], [])
        self.assertEqual(res["ops"], [])
        # the full caller-facing shape survives (monitor bracket-indexes these)
        for k in ("surface_ops", "no_surface", "no_change", "run_verdict", "floor_results",
                  "verdicts", "decisions", "cost_usd"):
            self.assertIn(k, res)
        self.assertEqual(list(res["cost_usd"]), ["route"])          # only the route was paid
        rec = res["decisions"][0]
        self.assertEqual(rec["judge_verdict"], "gated_routine")
        self.assertEqual(rec["judge_reason"], "tense refresh only")  # the router's rationale
        self.assertIsNone(rec["new_text"])                           # nothing was authored
        self.assertFalse(rec["committed"])
        self.assertFalse(rec["rewrite_exhausted"])
        self.assertEqual(rec["feed_note"], SURFACE_OP["feed_note"])  # audit keeps the routing

    def test_fail_open_on_missing_or_empty_verdict(self):
        for rv in ({}, {"consequence_rationale": "x"}, {"consequential": None}):
            res = _run(rv, "gate")
            self.assertNotIn("gated", res, f"verdict {rv!r} must fail OPEN (consequential)")
            self.assertEqual(len(res["confirmed"]), 1)               # full pipeline ran

    def test_shadow_mode_unchanged_even_on_routine(self):
        res = _run(ROUTINE, "shadow")
        self.assertNotIn("gated", res)
        self.assertEqual(len(res["confirmed"]), 1)                   # authored + judged as today
        self.assertEqual(res["run_verdict"], ROUTINE)                # verdict still surfaces

    def test_consequential_in_gate_mode_runs_fully(self):
        res = _run({"consequential": True, "consequence_rationale": "changes the play",
                    "headline": "h"}, "gate")
        self.assertNotIn("gated", res)
        self.assertEqual(len(res["confirmed"]), 1)


class DeferredNoteEmail(unittest.TestCase):
    META = {"my_company": "Anthropic", "competitor": "OpenAI"}
    ALERT = {"severity": "watch", "headline": "h", "old_value": None, "new_value": "n",
             "so_what": "s", "subject_key": "k", "date": "2026-07-02"}

    def test_digest_carries_the_deferred_note(self):
        _, body = notify.render_digest(self.META, [dict(self.ALERT)],
                                       deferred_note="Routine run: 3 routed update(s) deferred "
                                                     "by the consequentiality gate.")
        self.assertIn("3 routed update(s) deferred", body)
        self.assertLess(body.index("deferred"), body.index("— Scout"))   # before the sign-off

    def test_digest_without_note_is_unchanged(self):
        _, body = notify.render_digest(self.META, [dict(self.ALERT)])
        self.assertNotIn("deferred", body)


if __name__ == "__main__":
    unittest.main()
