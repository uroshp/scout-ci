"""The model boundary normalizes REPRESENTATION in code (2026-10-04).

Sonnet 5.5 writes `confidence` as 0.85 / 0.9 where Sonnet 5 wrote "high". The prompts never spell
the enum out (it lives in scout/schema.py), so with the 10/3 model change EVERY own-side fact failed
the pre-grounding schema check and was dropped without a word: the rehearsal of the OpenAI vs
Anthropic card found the DevDay collaboration launches, sourced them with verbatim excerpts, and the
step row read "0 anchor fact(s) grounded of 4 candidate(s)" as if grounding had failed. These tests
pin the fix: numbers and loose strings map onto the enum before any schema check, in both monitor
arms and in generation, and a fact the schema still rejects is NAMED in the step row (and fails the
step when nothing survived)."""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scout import monitor, schema  # noqa: E402


class NormalizeConfidence(unittest.TestCase):
    def test_numbers_map_onto_the_enum(self):
        self.assertEqual(schema.normalize_confidence(0.9), "high")
        self.assertEqual(schema.normalize_confidence(0.85), "high")
        self.assertEqual(schema.normalize_confidence(0.75), "high")
        self.assertEqual(schema.normalize_confidence(0.6), "medium")
        self.assertEqual(schema.normalize_confidence(0.3), "low")
        self.assertEqual(schema.normalize_confidence(1), "high")
        self.assertEqual(schema.normalize_confidence(90), "high")      # a 0-100 scale
        self.assertEqual(schema.normalize_confidence(50), "medium")

    def test_strings_are_trimmed_and_lowercased(self):
        self.assertEqual(schema.normalize_confidence(" High "), "high")
        self.assertEqual(schema.normalize_confidence("MEDIUM"), "medium")
        self.assertEqual(schema.normalize_confidence("very high"), "high")
        self.assertEqual(schema.normalize_confidence("0.9"), "high")
        self.assertEqual(schema.normalize_confidence("certain"), "certain")   # still named by the schema

    def test_normalize_claim_touches_representation_only(self):
        c = {"subject_key": "x", "claim": "Text.", "confidence": 0.9, "source_tier": "Primary ", "claim_type": "Fact"}
        out = schema.normalize_claim(c)
        self.assertIs(out, c)
        self.assertEqual(c["confidence"], "high")
        self.assertEqual(c["source_tier"], "primary")
        self.assertEqual(c["claim_type"], "fact")
        self.assertEqual(c["claim"], "Text.")


def _sonnet55_fact(subject_key: str, conf) -> dict:
    """The shape the own-company step emitted on 2026-10-04 (verbatim keys, numeric confidence)."""
    return {"claim": {"subject_key": subject_key,
                      "claim": "At DevDay on September 29 OpenAI made @ChatGPT mentionable in Slack and Microsoft Teams channels.",
                      "claim_type": "fact", "section": "tracked_facts", "zone": None, "order": 1,
                      "source_url": "https://thenextweb.com/news/openai-devday", "source_tier": "reputable_secondary",
                      "evidence_excerpt": "People can now mention @ChatGPT in Slack and Microsoft Teams channels, threads and direct messages.",
                      "as_of": "2026-09-29", "confidence": conf,
                      "candidate_sources": [{"source_url": "https://thenextweb.com/news/openai-devday",
                                             "source_tier": "reputable_secondary",
                                             "evidence_excerpt": "People can now mention @ChatGPT in Slack and Microsoft Teams channels, threads and direct messages."}]},
            "alert": {"old_value": "Workspace Agents", "new_value": "@ChatGPT in Slack and Teams", "headline": "h", "so_what": "s", "severity": "act"}}


class OwnCompanyArmBoundary(unittest.TestCase):
    """_my_company_facts with the model faked to Sonnet 5.5's output and grounding faked to keep
    whatever reaches it: the facts must REACH grounding."""

    def _run(self, facts):
        text = json.dumps({"facts": facts, "immaterial": []})
        kept_ids = []

        def fake_ground_best(claims):
            # what the real grounder returns: candidate_sources stripped, the grounding record attached
            kept_ids.extend(c["subject_key"] for c in claims)
            kept = []
            for c in claims:
                k = {kk: v for kk, v in c.items() if kk != "candidate_sources"}
                k["grounding"] = {"checked": True, "match": True, "method": "substring", "fetched_at": "2026-10-04", "detail": None}
                kept.append(k)
            return {"kept": kept, "failed": [], "cut": []}

        with mock.patch.object(monitor, "_run_my_facts", new=mock.AsyncMock(return_value={"text": text, "cost_usd": 0.2})), \
             mock.patch.object(monitor, "_ground_best", side_effect=fake_ground_best):
            out = monitor._my_company_facts("openai__vs__anthropic__x", {"competitor": "Anthropic", "my_company": "OpenAI"},
                                            "2026-09-15", [{"signal": "s", "about": "my_company"}], [])
        return out, kept_ids

    def test_numeric_confidence_reaches_grounding_and_lands(self):
        out, kept = self._run([_sonnet55_fact("openai|flagship-product|collaboration-agent", 0.9),
                               _sonnet55_fact("openai|chatgpt-space", 0.88)])
        self.assertEqual(kept, ["openai|flagship-product|collaboration-agent", "openai|chatgpt-space"])
        self.assertEqual(len(out["grounded"]), 2)
        self.assertEqual(out["schema_rejected"], [])
        self.assertEqual(out["emitted"], 2)
        self.assertEqual(out["grounded"][0][0]["confidence"], "high")

    def test_a_fact_the_schema_still_rejects_is_named(self):
        bad = _sonnet55_fact("openai|bad", 0.9)
        bad["claim"]["source_tier"] = "blog"                   # not a tier the schema knows
        del bad["claim"]["candidate_sources"]
        out, kept = self._run([bad, _sonnet55_fact("openai|good", "High")])
        self.assertEqual(kept, ["openai|good"])
        self.assertEqual(len(out["schema_rejected"]), 1)
        self.assertTrue(out["schema_rejected"][0].startswith("openai|bad: "))


class StepRowNamesSchemaRejects(unittest.TestCase):
    def test_everything_rejected_fails_the_step(self):
        status, detail = monitor._arm_status(0, 4, "anchor fact(s) grounded", 4,
                                             ["a: confidence: 0.9 is not one of [...]"] * 4)
        self.assertEqual(status, "failed")
        self.assertIn("4 of 4 emitted fact(s) rejected by the schema before grounding", detail)
        self.assertIn("0 anchor fact(s) grounded of 4 candidate(s)", detail)

    def test_partial_rejects_are_named_but_the_step_ran(self):
        status, detail = monitor._arm_status(1, 3, "grounded", 2, ["x: as_of: 'soon' is not a date"])
        self.assertEqual(status, "ran")
        self.assertIn("1 of 2 emitted fact(s) rejected", detail)

    def test_clean_row_is_unchanged(self):
        self.assertEqual(monitor._arm_status(2, 3, "grounded", 2, []), ("ran", "2 grounded of 3 candidate(s)"))


if __name__ == "__main__":
    unittest.main()
