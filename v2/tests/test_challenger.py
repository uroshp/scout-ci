"""Unit tests for the shadow-eval challenger's MODEL-FREE logic: Cohen's kappa, champion-vs-
challenger classification, idempotency hashing, and scorecard aggregation. No model/API calls —
judge_record/_run_challenger (which hit the SDK) are not exercised here."""
import unittest
from unittest import mock

from scout import challenger


class CohensKappa(unittest.TestCase):
    def test_perfect_agreement_two_classes(self):
        k = challenger.cohens_kappa(["keep", "keep", "cut", "cut"], ["keep", "keep", "cut", "cut"])
        self.assertAlmostEqual(k, 1.0)

    def test_chance_agreement_is_zero(self):
        k = challenger.cohens_kappa(["keep", "cut", "keep", "cut"], ["keep", "keep", "cut", "cut"])
        self.assertAlmostEqual(k, 0.0)

    def test_total_disagreement_is_negative(self):
        k = challenger.cohens_kappa(["keep", "cut"], ["cut", "keep"])
        self.assertLess(k, 0)

    def test_degenerate_single_class_is_none(self):
        self.assertIsNone(challenger.cohens_kappa(["keep", "keep"], ["keep", "keep"]))

    def test_empty_is_none(self):
        self.assertIsNone(challenger.cohens_kappa([], []))

    def test_mismatched_length_is_none(self):
        self.assertIsNone(challenger.cohens_kappa(["keep"], ["keep", "cut"]))


def _record():
    return {
        "slug": "a__vs__b__x", "run_ts": "2026-06-21T11:00:00", "source": "backfill",
        "kept": [
            {"id": "c_aaaaaaaaaaaa", "claim": "Kept-and-confirmed.", "evidence_excerpt": "supports it"},
            {"id": "c_bbbbbbbbbbbb", "claim": "Kept-but-challenger-cuts.", "evidence_excerpt": "weak"},
            {"id": "c_cccccccccccc", "claim": "Kept-but-abstained.", "evidence_excerpt": "x"},
        ],
        "cut": [
            {"action": "CUT", "claim": "Cut-and-confirmed.", "reason": "weak aggregator"},
            {"action": "CUT", "claim": "Cut-but-challenger-recovers.", "reason": "403 unreachable"},
        ],
    }


class Compare(unittest.TestCase):
    def setUp(self):
        self.rec = _record()
        # challenger verdicts: agree on c_a + cut:0; slop on c_b; recovery on cut:1; abstain on c_c
        self.judged = {"verdicts": {
            "c_aaaaaaaaaaaa": {"verdict": "keep", "reason": "ok", "confidence": "high"},
            "c_bbbbbbbbbbbb": {"verdict": "cut", "reason": "weak", "confidence": "medium"},
            "cut:0": {"verdict": "cut", "reason": "agree", "confidence": "high"},
            "cut:1": {"verdict": "keep", "reason": "recoverable", "confidence": "low"},
        }, "cost_usd": 0.01, "model": "claude-sonnet-4-6"}

    def test_classification(self):
        cmp = challenger.compare(self.rec, self.judged)
        s = cmp["summary"]
        self.assertEqual(s["judged"], 4)       # 4 verdicts; c_c abstained
        self.assertEqual(s["abstain"], 1)
        self.assertEqual(s["agree"], 2)        # c_a (keep/keep), cut:0 (cut/cut)
        self.assertEqual(s["disagree"], 2)
        self.assertEqual(s["recovery_candidates"], 1)   # cut:1 champ=cut chal=keep
        self.assertEqual(s["slop_candidates"], 1)       # c_b champ=keep chal=cut

    def test_disagreements_carry_delta_id_and_claim(self):
        cmp = challenger.compare(self.rec, self.judged)
        dis = [i for i in cmp["items"] if i["status"] == "disagree"]
        self.assertTrue(all(d.get("delta_id", "").startswith("x_") for d in dis))
        recovery = next(d for d in dis if d["direction"] == "recovery_candidate")
        self.assertEqual(recovery["claim"], "Cut-but-challenger-recovers.")

    def test_delta_id_stable_across_calls(self):
        a = challenger.compare(self.rec, self.judged)
        b = challenger.compare(self.rec, self.judged)
        ids_a = sorted(i["delta_id"] for i in a["items"] if i["status"] == "disagree")
        ids_b = sorted(i["delta_id"] for i in b["items"] if i["status"] == "disagree")
        self.assertEqual(ids_a, ids_b)


class ContentHash(unittest.TestCase):
    def test_deterministic_and_sensitive(self):
        rec = _record()
        h1 = challenger._content_hash(rec)
        self.assertEqual(h1, challenger._content_hash(_record()))   # same content -> same hash
        rec["kept"][0]["claim"] = "changed"
        self.assertNotEqual(h1, challenger._content_hash(rec))      # content change -> new hash


class Scorecard(unittest.TestCase):
    def test_aggregates_without_human_labels(self):
        rec = _record()
        judged = {"verdicts": {
            "c_aaaaaaaaaaaa": {"verdict": "keep"}, "c_bbbbbbbbbbbb": {"verdict": "cut"},
            "cut:0": {"verdict": "cut"}, "cut:1": {"verdict": "keep"},
            "c_cccccccccccc": {"verdict": "keep"},
        }, "cost_usd": 0.02, "model": "claude-sonnet-4-6"}
        result = challenger.result_record(rec, judged, challenger.compare(rec, judged))
        with mock.patch.object(challenger.selfserve, "read_data", return_value=None):
            sc = challenger.scorecard([result])
        self.assertEqual(sc["records"], 1)
        self.assertEqual(sc["disagreements"], 2)
        self.assertEqual(sc["adjudication"]["adjudicated"], 0)
        self.assertIsNone(sc["adjudication"]["kappa_challenger_vs_human"])  # no labels yet
        self.assertEqual(sc["cost_usd"], 0.02)
        # every pending disagreement must carry slug (the print path KeyError'd without this)
        self.assertTrue(all(d.get("slug") for d in sc["pending_disagreements"]))


if __name__ == "__main__":
    unittest.main()
