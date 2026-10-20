"""Shadow-eval capture invariants (docs/vnext-roadmap.md §v3.5).

The capture hook is a PURE OBSERVER bolted onto the LIVE generate/monitor write path, so its
invariants are safety ones: OFF by default, writes only to the private 'shadow/' store, and can
NEVER raise into the production run that calls it. Run from v2/:

    python -m unittest discover -s tests
"""
import json
import unittest
from unittest import mock

from scout import shadow


class _FakeGR:
    """Stand-in for grounding.GroundingResult (a dataclass) — attribute access, not a dict."""
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _grounding():
    # One dataclass-style result and one dict-style result, to prove both shapes are read.
    return {
        "results": [
            _FakeGR(claim_id="c_1", subject_key="x", url="https://a.com", status="absent",
                    method=None, best_ratio=0.88, http_status=200, excerpt="excerpt one here"),
            {"claim_id": "c_2", "subject_key": "y", "url": "https://b.com", "status": "grounded",
             "method": "substring", "best_ratio": 1.0, "http_status": 200, "excerpt": "verbatim"},
        ],
        "kept": [], "cut": [],
    }


KEPT = [{"id": "c_2", "claim": "X shipped Y", "claim_type": "fact", "section": "snapshot",
         "zone": None, "source_url": "https://b.com", "source_tier": "primary",
         "evidence_excerpt": "verbatim", "confidence": "high",
         "grounding": {"method": "substring"}}]
CUT = [{"action": "CUT", "claim": "Z happened", "reason": "evidence excerpt not found in source"}]


class FlagGatesCapture(unittest.TestCase):
    def test_noop_when_disabled(self):
        """Default OFF: no write is attempted at all."""
        with mock.patch.object(shadow.config, "SHADOW_EVAL_ENABLED", False), \
             mock.patch.object(shadow.selfserve, "write_data") as w:
            shadow.capture("slug", "generate", kept=KEPT, cut=CUT, grounding=_grounding())
            w.assert_not_called()

    def test_writes_well_formed_record_when_enabled(self):
        with mock.patch.object(shadow.config, "SHADOW_EVAL_ENABLED", True), \
             mock.patch.object(shadow.selfserve, "write_data") as w:
            shadow.capture("anthropic__vs__openai__general", "generate",
                           kept=KEPT, cut=CUT, grounding=_grounding(),
                           competitor="OpenAI", my_company="Anthropic", focus=None)
            w.assert_called_once()
            path, text, _message = w.call_args.args
            # Goes to the private 'shadow/' store, NEVER the public battlecards/ card.
            self.assertTrue(path.startswith("shadow/anthropic__vs__openai__general/"))
            self.assertTrue(path.endswith(".json"))
            self.assertNotIn("battlecards", path)
            rec = json.loads(text)
            self.assertEqual(rec["source"], "generate")
            self.assertEqual(rec["competitor"], "OpenAI")
            self.assertEqual(len(rec["kept"]), 1)
            self.assertEqual(len(rec["cut"]), 1)
            # best_ratio is preserved from BOTH a dataclass-like and a dict result — that band
            # (0.80-0.92) is the whole point: true-but-cut claims the challenger should recover.
            ratios = {r["claim_id"]: r["best_ratio"] for r in rec["grounding_results"]}
            self.assertEqual(ratios["c_1"], 0.88)
            self.assertEqual(ratios["c_2"], 1.0)


class CardFactsRideTheDerivedCapture(unittest.TestCase):
    """2026-10-19: prose is written from several facts; the challenger must see them all."""
    CLAIMS = [
        {"id": "c_fact1", "subject_key": "pricing.sol", "section": "pricing", "status": "active", "as_of": "2026-09-22",
         "claim": "GPT-6 Sol costs $2/$10 per million tokens.", "evidence_excerpt": "Sol is priced at $2 input and $10 output", "source_url": "https://x/sol", "source_tier": "primary"},
        {"id": "c_fact2", "subject_key": "pricing.astra", "section": "pricing", "status": "active",
         "claim": "GPT-6 Astra lists at $10/$50.", "evidence_excerpt": "Astra: $10 / $50", "source_url": "https://x/astra", "source_tier": "primary"},
        {"id": "c_retired", "subject_key": "old", "section": "pricing", "status": "retired",
         "claim": "Old price.", "evidence_excerpt": "old", "source_url": "https://x/old"},
        {"id": "c_prose", "subject_key": "battlecard.price", "section": "battlecard", "status": "active", "derived_from": "c_fact1",
         "claim": "OpenAI now sells Astra at $10/$50 and Sol at $2/$10.", "evidence_excerpt": None, "updated_on": "2026-10-04"},
    ]

    def test_card_facts_are_the_active_sourced_claims_trimmed(self):
        facts = shadow.card_facts(self.CLAIMS)
        self.assertEqual([f["id"] for f in facts], ["c_fact1", "c_fact2"])     # no retired, no prose
        self.assertEqual(facts[0]["source_url"], "https://x/sol")
        long = [dict(self.CLAIMS[0], id=f"c_{i}", claim="x" * 900, evidence_excerpt="y" * 900) for i in range(5)]
        trimmed = shadow.card_facts(long, cap=3)
        self.assertEqual(len(trimmed), 3)
        self.assertEqual((len(trimmed[0]["claim"]), len(trimmed[0]["evidence_excerpt"])), (600, 400))

    def test_the_derived_capture_carries_the_cards_facts(self):
        with mock.patch.object(shadow.config, "SHADOW_EVAL_ENABLED", True), \
             mock.patch.object(shadow.selfserve, "write_data") as w:
            shadow.capture_derived("a__vs__b__x", "monitor", self.CLAIMS, changed_on="2026-10-04", competitor="OpenAI", my_company="Anthropic")
            w.assert_called_once()
            rec = json.loads(w.call_args.args[1])
        self.assertEqual(rec["source"], "monitor-derived")
        self.assertEqual([k["id"] for k in rec["kept"]], ["c_prose"])
        self.assertEqual(rec["kept"][0]["evidence_mode"], "parent_fact")
        self.assertEqual([f["id"] for f in rec["card_facts"]], ["c_fact1", "c_fact2"])
        with mock.patch.object(shadow.config, "SHADOW_EVAL_ENABLED", True), \
             mock.patch.object(shadow.selfserve, "write_data") as w:
            shadow.capture("a__vs__b__x", "monitor", kept=KEPT, cut=CUT, grounding=_grounding())
            rec = json.loads(w.call_args.args[1])
        self.assertNotIn("card_facts", rec)                 # a plain fact capture carries none


class NeverRaises(unittest.TestCase):
    """The hook sits in the live write path — it must swallow EVERYTHING."""

    def test_malformed_inputs_do_not_raise(self):
        with mock.patch.object(shadow.config, "SHADOW_EVAL_ENABLED", True), \
             mock.patch.object(shadow.selfserve, "write_data"):
            shadow.capture("s", "generate", kept=None, cut="nope", grounding=123)
            shadow.capture("s", "monitor", kept=[1, 2], cut=[None], grounding={"results": [None]})

    def test_write_failure_is_swallowed(self):
        with mock.patch.object(shadow.config, "SHADOW_EVAL_ENABLED", True), \
             mock.patch.object(shadow.selfserve, "write_data", side_effect=RuntimeError("boom")):
            shadow.capture("s", "generate", kept=KEPT, cut=CUT, grounding=_grounding())  # no raise


if __name__ == "__main__":
    unittest.main()
