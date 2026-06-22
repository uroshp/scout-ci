"""Propagation APPLY + RETIRE-CASCADE invariants (spec §17 step 5).

Once the judge has confirmed an op, the control line takes over: deterministic, model-free code
materializes it into a real claim object, re-validates, and only then writes. These tests pin the
mutation semantics — add appends a derived interpretation, revise edits in place and re-anchors,
retire is a status flip that never deletes — and the retire-cascade that walks `derived_from` when
a fact is falsified. Pure functions, no model, no network. Run from v2/:

    python -m unittest discover -s tests
"""
import unittest

from scout.propagate import apply_ops, retire_cascade
from scout.schema import claim_id, validation_errors
from scout.render import claims_to_markdown

SLUG = "anthropic-vs-openai"
TODAY = "2026-06-14"
FACT_ID = claim_id(SLUG, "openai|api-outage|2026-06")
FACT_ID2 = claim_id(SLUG, "anthropic|fable-5-access|current")

# The grounded facts propagation draws from (live in the card, e.g. recent_moves).
FACTS = [
    {"id": FACT_ID, "subject_key": "openai|api-outage|2026-06", "as_of": "2026-06-11",
     "source_url": "https://news.test/openai-outage"},
    {"id": FACT_ID2, "subject_key": "anthropic|fable-5-access|current", "as_of": "2026-06-12",
     "source_url": "https://news.test/fable5"},
]


def _play(sk, claim="**Edge**\n\nWhy.\n\n**Soundbite:** *\"x\"*"):
    """A normal generated battlecard play, with its own grounded source."""
    return {
        "id": claim_id(SLUG, sk), "subject_key": sk, "claim": claim,
        "claim_type": "interpretation", "section": "battlecard", "zone": "where_we_win",
        "order": 0, "source_url": "https://own.test/a", "source_tier": "reputable_secondary",
        "evidence_excerpt": "z" * 45, "as_of": "2026-01-01", "verified": True,
        "confidence": "high", "persona": "technical_evaluator",
        "grounding": {"checked": True, "match": True, "method": "substring",
                      "fetched_at": "2026-01-01"},
    }


class ApplyAdd(unittest.TestCase):
    def test_add_appends_valid_derived_interpretation(self):
        claims = [_play("anthropic|reliability|current")]
        op = {"operation": "add", "section": "objection_handling", "zone": None,
              "subject_key": "openai|outage-objection|current",
              "claim": "**\"What about reliability?\"**\n\nOpenAI was down 3 days.\n\n**So what:** cite it.",
              "claim_type": "interpretation", "derived_from": FACT_ID, "persona": "economic_buyer"}
        res = apply_ops(claims, [op], FACTS, SLUG, TODAY)
        self.assertEqual(res["skipped"], [])
        self.assertEqual(len(res["claims"]), 2)
        new = res["claims"][-1]
        self.assertEqual(new["claim_type"], "interpretation")
        self.assertEqual(new["derived_from"], FACT_ID)
        self.assertNotIn("source_url", new)            # no own source — inherits via derived_from
        self.assertEqual(new["as_of"], "2026-06-11")   # parent fact's as_of
        self.assertEqual(validation_errors(new), [])

    def test_add_orders_after_existing_peers(self):
        claims = [_play("a|b|c"), _play("d|e|f")]
        claims[0]["order"], claims[1]["order"] = 0, 1
        op = {"operation": "add", "section": "battlecard", "zone": "where_we_win",
              "subject_key": "g|h|i", "claim": "**New**\n\nx.\n\n**Soundbite:** *\"y\"*",
              "claim_type": "interpretation", "derived_from": FACT_ID, "persona": "technical_evaluator"}
        res = apply_ops(claims, [op], FACTS, SLUG, TODAY)
        self.assertEqual(res["claims"][-1]["order"], 2)


class ApplyRevise(unittest.TestCase):
    def test_revise_edits_in_place_and_reanchors(self):
        claims = [_play("anthropic|reliability|current")]
        original_id = claims[0]["id"]
        op = {"operation": "revise", "section": "battlecard", "zone": "where_we_win",
              "target_subject_key": "anthropic|reliability|current",
              "subject_key": "anthropic|reliability|current",
              "claim": "**We win on uptime**\n\nOpenAI down 3 days June 2026.\n\n**Soundbite:** *\"x\"*",
              "claim_type": "interpretation", "derived_from": FACT_ID}
        res = apply_ops(claims, [op], FACTS, SLUG, TODAY)
        self.assertEqual(res["skipped"], [])
        self.assertEqual(len(res["claims"]), 1)         # in place, not appended
        c = res["claims"][0]
        self.assertEqual(c["id"], original_id)          # identity preserved
        self.assertIn("OpenAI down 3 days", c["claim"])
        self.assertEqual(c["derived_from"], FACT_ID)
        self.assertNotIn("source_url", c)               # own source stripped, re-anchored to the fact
        self.assertNotIn("grounding", c)
        self.assertEqual(c["as_of"], "2026-06-11")
        self.assertEqual(validation_errors(c), [])

    def test_revise_missing_target_is_skipped_not_crashed(self):
        res = apply_ops([_play("x|y|z")], [{
            "operation": "revise", "target_subject_key": "ghost|none|x",
            "subject_key": "ghost|none|x", "claim": "new", "claim_type": "interpretation",
            "section": "battlecard", "zone": "where_we_win", "derived_from": FACT_ID}],
            FACTS, SLUG, TODAY)
        self.assertEqual(res["applied"], [])
        self.assertEqual(len(res["skipped"]), 1)


class ApplyRetire(unittest.TestCase):
    def test_retire_flips_status_keeps_claim(self):
        claims = [_play("anthropic|open-access|current")]
        original_text = claims[0]["claim"]
        op = {"operation": "retire", "section": "battlecard", "zone": "where_we_win",
              "target_subject_key": "anthropic|open-access|current",
              "subject_key": "anthropic|open-access|current", "claim": None,
              "claim_type": "interpretation", "derived_from": FACT_ID2,
              "retired_reason": "invalidated: access pulled"}
        res = apply_ops(claims, [op], FACTS, SLUG, TODAY)
        self.assertEqual(res["skipped"], [])
        c = res["claims"][0]
        self.assertEqual(c["status"], "retired")
        self.assertEqual(c["retired_on"], TODAY)
        self.assertEqual(c["retired_reason"], "invalidated: access pulled")
        self.assertEqual(c["derived_from"], FACT_ID2)   # the killing fact
        self.assertEqual(c["claim"], original_text)     # text kept for lineage, never deleted
        self.assertEqual(validation_errors(c), [])

    def test_input_claims_never_mutated(self):
        claims = [_play("anthropic|open-access|current")]
        apply_ops(claims, [{
            "operation": "retire", "section": "battlecard", "zone": "where_we_win",
            "target_subject_key": "anthropic|open-access|current",
            "subject_key": "anthropic|open-access|current", "claim": None,
            "claim_type": "interpretation", "derived_from": FACT_ID2,
            "retired_reason": "neutralized: wash"}], FACTS, SLUG, TODAY)
        self.assertNotIn("status", claims[0])           # the caller's list is untouched


class RetireCascade(unittest.TestCase):
    def test_falsified_fact_retires_its_dependents_only(self):
        # Two propagated interpretations: one derives from the falsified fact, one does not.
        dep = {"id": claim_id(SLUG, "dep|play|current"), "subject_key": "dep|play|current",
               "claim": "**P**\n\nx.\n\n**Soundbite:** *\"y\"*", "claim_type": "interpretation",
               "section": "battlecard", "zone": "where_we_win", "order": 0, "verified": True,
               "confidence": "medium", "as_of": "2026-06-11", "derived_from": FACT_ID,
               "persona": "technical_evaluator"}
        indep = {"id": claim_id(SLUG, "indep|play|current"), "subject_key": "indep|play|current",
                 "claim": "**Q**\n\nx.\n\n**Soundbite:** *\"y\"*", "claim_type": "interpretation",
                 "section": "battlecard", "zone": "where_we_win", "order": 1, "verified": True,
                 "confidence": "medium", "as_of": "2026-06-11", "derived_from": FACT_ID2,
                 "persona": "technical_evaluator"}
        res = retire_cascade([dep, indep], {FACT_ID}, TODAY)
        self.assertEqual([x["id"] for x in res["cascaded"]], [dep["id"]])
        self.assertEqual(res["claims"][0]["status"], "retired")
        self.assertEqual(res["claims"][0]["retired_on"], TODAY)
        self.assertNotIn("status", res["claims"][1])    # the independent claim is untouched


class RenderLifecycle(unittest.TestCase):
    def test_retired_claims_excluded_from_active_card(self):
        active = _play("anthropic|live-win|current",
                       "**Live win**\n\nReal.\n\n**Soundbite:** *\"a\"*")
        retired = _play("anthropic|dead-play|current",
                        "**Dead play**\n\nGone.\n\n**Soundbite:** *\"b\"*")
        retired.update(status="retired", retired_on=TODAY, retired_reason="invalidated: x",
                       derived_from=FACT_ID2)
        md = claims_to_markdown([active, retired], "# Competitive Intelligence Brief: A vs B",
                                my_company="A", competitor="B")
        self.assertIn("Live win", md)
        self.assertNotIn("Dead play", md)               # retired -> off the active card

    def test_propagated_claim_links_through_to_parent_source(self):
        parent = {"id": FACT_ID, "subject_key": "openai|api-outage|2026-06",
                  "claim": "OpenAI API outage.", "claim_type": "fact", "section": "recent_moves",
                  "zone": None, "order": 0, "source_url": "https://news.test/openai-outage",
                  "source_tier": "reputable_secondary", "evidence_excerpt": "z" * 45,
                  "as_of": "2026-06-11", "verified": True, "confidence": "high",
                  "grounding": {"checked": True, "match": True, "method": "substring",
                                "fetched_at": "2026-06-11"}}
        prop = {"id": claim_id(SLUG, "o|obj|current"), "subject_key": "o|obj|current",
                "claim": "**\"Reliability?\"**\n\nThey were down.\n\n**So what:** cite it.",
                "claim_type": "interpretation", "section": "objection_handling", "zone": None,
                "order": 0, "as_of": "2026-06-11", "verified": True, "confidence": "medium",
                "derived_from": FACT_ID}
        md = claims_to_markdown([parent, prop], "# Competitive Intelligence Brief: A vs B",
                                my_company="A", competitor="B")
        self.assertIn("news.test", md)                  # the inherited source link rendered


if __name__ == "__main__":
    unittest.main()
