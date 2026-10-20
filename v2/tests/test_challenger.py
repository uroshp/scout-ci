"""Unit tests for the shadow-eval challenger's MODEL-FREE logic: Cohen's kappa, champion-vs-
challenger classification, idempotency hashing, and scorecard aggregation. No model/API calls —
judge_record/_run_challenger (which hit the SDK) are not exercised here."""
import asyncio
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


class ItemsScope(unittest.TestCase):
    def test_kept_claim_without_excerpt_is_skipped(self):
        # the capture-bug fix: an interpretation (no evidence excerpt) is out of the excerpt-
        # groundedness judge's scope, so it isn't shown to the challenger or counted in the comparison
        rec = {"slug": "a__vs__b__x", "run_ts": "t", "source": "backfill", "cut": [],
               "kept": [
                   {"id": "c_fact00000000", "claim": "A fact.", "evidence_excerpt": "the evidence"},
                   {"id": "c_interp000000", "claim": "**An objection?**\n\nb.\n\n**So what:** m.",
                    "evidence_excerpt": None},
               ]}
        items, champion = challenger._items(rec)
        ids = {i["item_id"] for i in items}
        self.assertIn("c_fact00000000", ids)            # excerpt-bearing fact is judged
        self.assertNotIn("c_interp000000", ids)         # no-excerpt interpretation skipped
        self.assertNotIn("c_interp000000", champion)    # and not counted as a disagreement


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


class CardFactsReachTheChallenger(unittest.TestCase):
    """2026-10-19: a derived item is judged against the card's facts, not one parent."""
    FACTS = [
        {"id": "c_sol", "claim": "GPT-6 Sol costs $2/$10 per million tokens.", "evidence_excerpt": "Sol is priced at $2 input and $10 output", "source_url": "https://x/sol"},
        {"id": "c_astra", "claim": "GPT-6 Astra lists at $10/$50.", "evidence_excerpt": "Astra: $10 / $50", "source_url": "https://x/astra"},
        {"id": "c_luna", "claim": "GPT-6 Luna costs $0.10/$0.50.", "evidence_excerpt": "Luna at $0.10 and $0.50", "source_url": "https://x/luna"},
        {"id": "c_hr", "claim": "OpenAI hired a new CFO.", "evidence_excerpt": "named chief financial officer", "source_url": "https://x/cfo"},
        {"id": "c_office", "claim": "OpenAI opened a Tokyo office.", "evidence_excerpt": "Tokyo office opens", "source_url": "https://x/tokyo"},
    ]
    DERIVED = {"item_id": "c_prose", "evidence_mode": "parent_fact", "supporting_fact": "GPT-6.1 Sol launched September 29.",
               "claim": "OpenAI now sells a GPT-6 lineup: Astra at $10/$50, Sol at $2/$10 and Luna at $0.10/$0.50.", "evidence": "x", "source_url": "https://x/61"}

    def test_relevant_facts_share_a_number_or_a_name_with_the_prose(self):
        rel = challenger._relevant_facts(self.FACTS, [self.DERIVED])
        self.assertEqual([f["id"] for f in rel], ["c_sol", "c_astra", "c_luna"])
        self.assertEqual(challenger._relevant_facts(self.FACTS, [{"item_id": "f", "evidence_mode": "own_excerpt", "claim": "x"}]), [])
        self.assertEqual(challenger._relevant_facts([], [self.DERIVED]), [])
        few = challenger._relevant_facts(self.FACTS[3:], [self.DERIVED])        # under three hits: everything, capped
        self.assertEqual([f["id"] for f in few], ["c_hr", "c_office"])
        self.assertEqual(len(challenger._relevant_facts(self.FACTS * 20, [self.DERIVED], cap=7)), 7)

    def test_the_request_carries_the_facts_only_when_given(self):
        seen = {}

        async def fake_drive(prompt, options, role):
            seen["prompt"] = prompt
            return {"text": '{"verdicts": []}', "cost_usd": 0.0}
        with mock.patch.object(challenger, "_drive", fake_drive):
            asyncio.run(challenger._run_challenger("a__vs__b__x", [self.DERIVED], "sys", card_facts=self.FACTS[:3]))
            self.assertIn("CARD FACTS", seen["prompt"])
            self.assertIn("Luna at $0.10 and $0.50", seen["prompt"])
            asyncio.run(challenger._run_challenger("a__vs__b__x", [self.DERIVED], "sys"))
            self.assertNotIn("CARD FACTS", seen["prompt"])

    def test_judge_record_uses_the_captured_facts_or_reads_the_card(self):
        calls = []

        async def fake_run(slug, items, system, card_facts=None):
            calls.append(card_facts)
            return {"text": '{"verdicts": [{"item_id": "c_prose", "verdict": "keep", "reason": "licensed", "confidence": "high"}]}', "cost_usd": 0.01}
        kept = [{"id": "c_prose", "claim": self.DERIVED["claim"], "evidence_excerpt": None,
                 "parent": {"claim": "GPT-6.1 Sol launched September 29.", "evidence_excerpt": "x", "source_url": "https://x/61"}}]
        with mock.patch.object(challenger, "_run_challenger", fake_run):
            out = challenger.judge_record({"slug": "a__vs__b__x", "run_ts": "t", "kept": kept, "cut": [], "card_facts": self.FACTS})
            self.assertEqual(out["verdicts"]["c_prose"]["verdict"], "keep")
            self.assertEqual([f["id"] for f in calls[-1]], ["c_sol", "c_astra", "c_luna"])
            with mock.patch("scout.store.load_claims", return_value=[dict(f, status="active") for f in self.FACTS]):
                challenger.judge_record({"slug": "a__vs__b__x", "run_ts": "t", "kept": kept, "cut": []})   # an older record
            self.assertEqual([f["id"] for f in calls[-1]], ["c_sol", "c_astra", "c_luna"])
            with mock.patch("scout.store.load_claims", side_effect=FileNotFoundError("no card")):
                challenger.judge_record({"slug": "gone", "run_ts": "t", "kept": kept, "cut": []})
            self.assertEqual(calls[-1], [])                                           # unreadable card: judged as before



if __name__ == "__main__":
    unittest.main()