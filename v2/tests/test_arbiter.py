"""The research layer: context parsing, anonymisation, grade mapping, aggregates (no model calls)."""
import json
import unittest
from unittest import mock

from scout import arbiter, research

USER = ("Competitor: OpenAI   We are: Anthropic\n\n"
        "GROUNDED FACTS (the ONLY admissible evidence; judge every op strictly against these):\n"
        + json.dumps([{"id": "c_1", "claim": "OpenAI canceled Astra.", "source_url": "https://x", "as_of": "2026-09-29"}])
        + "\n\nCURRENT ACTIVE PLAYS + OBJECTIONS (full prose for the ops' targets — the old_text a revise/retire would change; "
          "other claims truncated, enough to catch a duplicate add):\n"
        + json.dumps([{"subject_key": "k1", "claim": "old text"}])
        + "\n\nPROPOSED OPS TO JUDGE (confirm or reject each by op_index):\n"
        + json.dumps([{"op_index": 0, "operation": "revise", "section": "battlecard", "target_subject_key": "k1", "claim": "new text", "derived_from": "c_1"}]))
RECORD = {"call_id": "c_call", "role": "judge", "run_ts": "2026-10-01T04:00:00", "model": "claude-opus-4-8",
          "system": {"kind": "string", "text": "sys"}, "user": USER,
          "result": {"text": json.dumps({"verdicts": [{"op_index": 0, "verdict": "confirm", "reason": "live reason"}]})}}
RESULTS = [{"call_id": "c_call", "backend": "ollama", "backend_model": "magistral:24b", "mode": "exact", "rep": 0, "status": "ok",
            "text": json.dumps({"verdicts": [{"op_index": 0, "verdict": "reject", "reason": "local reason"}]})}]
ROW = {"delta_id": "m_abc", "call_id": "c_call", "role": "judge", "slug": "s", "item_id": "0", "reference": "confirm", "candidates": {"ollama": "reject"}}


class Context(unittest.TestCase):
    def test_judge_context_parses_facts_current_and_op(self):
        ctx = arbiter.judge_context(RECORD, "0")
        self.assertEqual(ctx["facts"][0]["id"], "c_1"); self.assertEqual(ctx["current"]["claim"], "old text")
        self.assertEqual(ctx["op"]["claim"], "new text"); self.assertEqual(ctx["competitor"], "OpenAI"); self.assertEqual(ctx["my_company"], "Anthropic")

    def test_arm_texts_and_anonymise_hide_identity_but_keep_the_key(self):
        arms = arbiter.arm_texts(ROW, RECORD, RESULTS)
        self.assertEqual([a["who"] for a in arms], ["live_judge", "ollama"])
        labelled, key = arbiter.anonymise(arms, "m_abc")
        self.assertEqual(sorted(a["label"] for a in labelled), ["A", "B"])
        self.assertEqual(sorted(key.values()), ["live_judge", "ollama"])
        for a in labelled:
            self.assertNotIn("who", a)
        self.assertEqual(arbiter.anonymise(arms, "m_abc")[1], key)      # deterministic per item


class Arbitrate(unittest.TestCase):
    def test_grades_map_back_to_arms_and_record_is_persisted(self):
        writes = {}
        def fake_call(prompt, searches=3):
            self.assertIn("[A]", prompt); self.assertNotIn("live_judge", prompt); self.assertNotIn("magistral", prompt)
            return {"parsed": {"verdict": "reject", "decisive_facts": ["c_1"], "resolution": "r", "searched": "",
                               "best": "none", "best_why": "", "confidence": 0.7,
                               "grades": {"A": {"right": False, "modes": ["overreach", "bogus"], "diagnosis": "d"},
                                          "B": {"right": True, "modes": [], "diagnosis": "ok"}}},
                    "text": "{}", "cost_usd": 0.1, "duration_ms": 1000, "searches_used": 0, "model": "claude-opus-5-5"}
        with mock.patch.object(arbiter, "call_arbiter", side_effect=fake_call), \
             mock.patch.object(arbiter.selfserve, "write_data", side_effect=lambda p, t, m: writes.__setitem__(p, t)):
            rec = arbiter.arbitrate(ROW, RECORD, RESULTS)
        self.assertEqual(rec["verdict"], "reject"); self.assertIsNone(rec["best"])
        self.assertEqual(set(rec["grades"]), {"live_judge", "ollama"})
        for g in rec["grades"].values():
            self.assertTrue(set(g["modes"]) <= set(arbiter.FAILURE_MODES))     # unknown modes dropped
        self.assertEqual(rec["content"]["facts"][0]["id"], "c_1"); self.assertEqual(rec["review"]["status"], "pending")
        self.assertIn("research/arbiter/2026-10/m_abc.json", writes)
        self.assertGreater(rec["prompt_chars"], 0)

    def test_ratify_agree_and_overrule_write_the_label(self):
        rec = {"delta_id": "m_abc", "verdict": "reject", "arbiter_model": "claude-opus-5-5", "run_ts": "2026-10-01T04:00:00", "review": {}}
        labels = []
        with mock.patch.object(arbiter.adjudicate_models, "label", side_effect=lambda d, t, n: labels.append((d, t)) or {}), \
             mock.patch.object(arbiter.selfserve, "write_data"):
            arbiter.ratify(dict(rec), None)
            arbiter.ratify(dict(rec), "confirm", "the arbiter missed the materiality")
        self.assertEqual(labels, [("m_abc", "reject"), ("m_abc", "confirm")])


class Research(unittest.TestCase):
    def test_aggregates(self):
        recs = [{"run_ts": "2026-10-01", "cost_usd": 0.1, "duration_ms": 30000, "searches_used": 0, "prompt_chars": 40000,
                 "review": {"status": "agreed"},
                 "grades": {"live_judge": {"right": True, "modes": []}, "ollama": {"right": False, "modes": ["overreach"]}}},
                {"run_ts": "2026-10-01", "cost_usd": 0.1, "duration_ms": 30000, "searches_used": 1, "prompt_chars": 10000,
                 "review": {"status": "overruled"},
                 "grades": {"live_judge": {"right": False, "modes": ["misread_rule"]}, "ollama": {"right": True, "modes": ["right_for_wrong_reason"]}}}]
        results = [{"backend": "ollama", "role": "judge", "mode": "exact", "rep": 0, "status": "ok",
                    "comparison": {"items": [{"status": "agree"}, {"status": "disagree"}, {"status": "abstain"}]}}]
        rep = research.report(recs, results)
        self.assertEqual(rep["variance"]["judge"]["rate"], 0.5)
        self.assertEqual(rep["outcomes"]["ollama"]["precision"], 0.5); self.assertEqual(rep["outcomes"]["ollama"]["right_for_wrong_reason"], 1)
        self.assertEqual(rep["causes"]["overall"]["overreach"], 1)
        self.assertEqual(rep["review"]["overrule_rate"], 0.5)
        self.assertIn("30-45k", rep["by_size"]); self.assertIn("<15k chars", rep["by_size"])
        self.assertEqual(rep["cost"]["items_with_search"], 1)
        text = research.render(rep)
        self.assertIn("Variance", text); self.assertIn("overreach 1", text)


if __name__ == "__main__":
    unittest.main()
