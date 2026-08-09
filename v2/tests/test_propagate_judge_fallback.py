"""Judge-outage invariants (the 2026-07-01 Opus incident: two unparseable judge responses silently
killed 4 material drafts). The judge must degrade, never drop: empty parse -> primary retry ->
FALLBACK MODEL (different family) -> and if all fail, ops become judge_unavailable — preserved for
human review, never rejected, never silent. All model calls mocked. Run from v2/:

    python -m unittest discover -s tests
"""
import json
import unittest
from unittest import mock

from scout import config, notify, propagate, review
from scout.propagate import _decision_records, log_decisions


OPS = [(0, {"operation": "revise", "section": "pricing", "zone": None, "claim": "draft prose",
            "subject_key": "anthropic|api-list-price|current", "derived_from": "c_aaaaaaaaaaaa"})]
GOOD = ('```json\n{"verdicts":[{"op_index":0,"verdict":"confirm","reason":"ok",'
        '"rewritable":false}]}\n```')
GARBAGE = "I apologize, but"


class JudgeFallback(unittest.TestCase):
    def _judge(self, texts):
        """Run judge() with a stateful _run_judge fake; returns (result, calls[(model, text)...])."""
        calls = []

        async def fake(meta, facts, claims, indexed_ops, model=None):
            calls.append(model)
            return {"text": texts[len(calls) - 1], "cost_usd": 0.1}

        with mock.patch.object(propagate, "_run_judge", fake):
            res = propagate.judge({"competitor": "X"}, [], [], OPS)
        return res, calls

    def test_healthy_single_call_no_fallback(self):
        res, calls = self._judge([GOOD])
        self.assertEqual(calls, [config.ORCHESTRATOR_MODEL])
        self.assertEqual(res["verdicts"][0]["verdict"], "confirm")
        self.assertEqual(res["verdicts"][0]["judged_by"], config.ORCHESTRATOR_MODEL)
        self.assertEqual(res["raw_failures"], [])

    def test_two_garbage_then_fallback_judges(self):
        res, calls = self._judge([GARBAGE, GARBAGE, GOOD])
        self.assertEqual(calls, [config.ORCHESTRATOR_MODEL, config.ORCHESTRATOR_MODEL,
                                 config.JUDGE_FALLBACK_MODEL])
        self.assertEqual(res["verdicts"][0]["verdict"], "confirm")
        self.assertEqual(res["verdicts"][0]["judged_by"],
                         f"fallback:{config.JUDGE_FALLBACK_MODEL}")
        self.assertEqual([f["model"] for f in res["raw_failures"]],
                         [config.ORCHESTRATOR_MODEL, config.ORCHESTRATOR_MODEL])

    def test_all_garbage_becomes_judge_unavailable_not_reject(self):
        res, calls = self._judge([GARBAGE, GARBAGE, GARBAGE])
        self.assertEqual(len(calls), 3)
        v = res["verdicts"][0]
        self.assertEqual(v["verdict"], "judge_unavailable")
        self.assertFalse(v["rewritable"])
        self.assertIsNone(v["judged_by"])
        # adjudicate keys per-op hiccups on this exact string; an outage must not match it
        self.assertNotIn("fail-closed", v["reason"])
        self.assertEqual(len(res["raw_failures"]), 3)

    def test_raw_failure_text_truncated(self):
        res, _ = self._judge(["x" * 9000, GOOD])
        self.assertEqual(len(res["raw_failures"][0]["text"]), 4000)

    def test_empty_knob_disables_fallback(self):
        saved = config.JUDGE_FALLBACK_MODEL
        config.JUDGE_FALLBACK_MODEL = ""
        try:
            res, calls = self._judge([GARBAGE, GARBAGE])
            self.assertEqual(len(calls), 2)               # primary + retry only, no third call
            self.assertEqual(res["verdicts"][0]["verdict"], "judge_unavailable")
        finally:
            config.JUDGE_FALLBACK_MODEL = saved


class UnavailableEndToEnd(unittest.TestCase):
    """propagate() with a dead judge: nothing confirms, nothing rewrites, nothing is silent."""

    def test_unavailable_flows_to_records_without_rewrite(self):
        from tests.test_propagate_rewrite import (CLAIMS, FACT, SURFACE_OP, _authored, _fake,
                                                  _route_fake)
        rewrite = mock.MagicMock()

        async def dead_judge(meta, facts, claims, indexed_ops, model=None):
            return {"text": GARBAGE, "cost_usd": 0.1}

        with mock.patch.object(propagate, "route", _route_fake([dict(SURFACE_OP)])), \
             mock.patch.object(propagate, "_run_author", _fake(_authored("fine prose"))), \
             mock.patch.object(propagate, "_run_judge", dead_judge), \
             mock.patch.object(propagate, "_run_rewrite", rewrite):
            res = propagate.propagate({"competitor": "OpenAI", "my_company": "Anthropic"},
                                      [{"fact": FACT, "alert": {"severity": "act"}}], [],
                                      CLAIMS, slug=None, persist=False)
        rewrite.assert_not_called()                       # unavailable is not 'reject': no loop
        self.assertEqual(res["confirmed"], [])
        rec = res["decisions"][0]
        self.assertEqual(rec["judge_verdict"], "judge_unavailable")
        self.assertFalse(rec["rewrite_exhausted"])        # disjoint: unverified, not exhausted
        self.assertFalse(rec["committed"])
        self.assertEqual(rec["new_text"], "fine prose")   # the draft is preserved in the record

    def test_log_payload_carries_raw_failures_and_schema_v3(self):
        captured = {}
        with mock.patch.object(propagate.selfserve, "write_data",
                               side_effect=lambda path, body, msg: captured.update(
                                   json.loads(body))):
            log_decisions("slug", [], judge_raw_failures=[{"model": "m", "text": "t"}])
        self.assertEqual(captured["schema_version"], 7)
        self.assertEqual(captured["judge_raw_failures"], [{"model": "m", "text": "t"}])

    def test_records_carry_judged_by(self):
        ops = [{"operation": "revise", "section": "pricing", "zone": None, "claim": "x",
                "subject_key": "s", "derived_from": "c_aaaaaaaaaaaa", "feed_note": None,
                "target_subject_key": None, "valence": None, "change_kind": "update"}]
        recs = _decision_records(ops, [[]], {0: {"verdict": "confirm", "reason": "ok",
                                                 "judged_by": "fallback:claude-sonnet-4-6"}},
                                 {}, {})
        self.assertEqual(recs[0]["judged_by"], "fallback:claude-sonnet-4-6")


class UnjudgedEmail(unittest.TestCase):
    META = {"my_company": "Anthropic", "competitor": "OpenAI"}
    UNJUDGED = [{"operation": "revise", "section": "pricing", "zone": None, "change_kind": "update",
                 "subject_key": "anthropic|api-list-price|current",
                 "trigger_source_url": "https://example.com/launch",
                 "old_text": "old pricing", "new_text": "drafted new pricing",
                 "judge_verdict": "judge_unavailable"}]

    def test_unjudged_only_email_passes_guard(self):
        out = notify.send_propagation_proposals("slug", self.META, [], dry_run=True,
                                                unjudged=self.UNJUDGED)
        self.assertNotEqual(out.get("reason"), "no proposals")
        self.assertIn("(+1 unverified)", out["subject"])

    def test_render_unjudged_section(self):
        _, body = notify.render_propagation_proposals("slug", self.META, [],
                                                      unjudged=self.UNJUDGED)
        self.assertIn("DRAFTED BUT UNVERIFIED", body)
        self.assertIn("drafted new pricing", body)        # the full draft, readable
        self.assertIn("allow_unjudged", body)             # the manual-approval path named
        self.assertIn("could NOT be verified", body)      # zero-confirmed opener


class ReviewUnjudged(unittest.TestCase):
    P = {"run_ts": "2026-07-02T03:09:00",
         "confirmed": [{"subject_key": "a", "judge_verdict": "confirm"}],
         "unjudged": [{"subject_key": "b", "judge_verdict": "judge_unavailable"}],
         "facts": []}

    def test_approve_default_excludes_unjudged(self):
        with mock.patch.object(review, "pending", return_value=dict(self.P)), \
             mock.patch.object(review, "apply") as ap:
            review.approve("slug")
        self.assertEqual([o["subject_key"] for o in ap.call_args.args[1]], ["a"])

    def test_allow_unjudged_includes_them(self):
        with mock.patch.object(review, "pending", return_value=dict(self.P)), \
             mock.patch.object(review, "apply") as ap:
            review.approve("slug", allow_unjudged=True)
        self.assertEqual([o["subject_key"] for o in ap.call_args.args[1]], ["a", "b"])


class AdjudicateExclusions(unittest.TestCase):
    DELTAS = [
        {"delta_id": "a_1", "judge_verdict": "confirm", "judged_by": "claude-opus-4-8"},
        {"delta_id": "a_2", "judge_verdict": "confirm", "judged_by": "fallback:claude-sonnet-4-6"},
        {"delta_id": "a_3", "judge_verdict": "judge_unavailable", "judged_by": None},
    ]

    def test_gate_excludes_unavailable_and_fallback(self):
        from scout import adjudicate
        with mock.patch.object(adjudicate, "load_deltas", return_value=list(self.DELTAS)), \
             mock.patch.object(adjudicate, "load_labels", return_value={}):
            d = adjudicate.digest()
        self.assertEqual([x["delta_id"] for x in d["pending"]], ["a_1"])   # gate pool: Opus only
        self.assertEqual(d["by_verdict"]["judge_unavailable"], 1)
        self.assertEqual(d["fallback_judged"], 1)
        self.assertEqual([x["delta_id"] for x in d["judge_unavailable"]], ["a_3"])


if __name__ == "__main__":
    unittest.main()
