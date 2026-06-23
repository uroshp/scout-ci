"""my_company STANDING-STRENGTH facts (scout.strengths) + the floor's strength-exclusion
(propagate._trigger_fact_ids). The catch-22 fix: a back-foot rebuttal's pivot needs a GROUNDED strength
in the admissible facts pool; these facts supply it, but they are pivot evidence ONLY and never a
trigger for an op. Deterministic, model-free, no network. Run from v2/:

    python -m unittest discover -s tests
"""
import unittest

from scout import strengths
from scout.propagate import _trigger_fact_ids, floor_check
from scout.schema import ANCHOR_SECTION, claim_id

SLUG = "anthropic-vs-openai"

_WIN_CLAIM = ("**We win on uptime**\n\nClaude is GA on Vertex AI and Bedrock with enterprise SLAs.\n\n"
              "**Soundbite:** *\"Run it where your SLA already lives.\"*")


def _win_play(sk, claim=_WIN_CLAIM, **kw):
    base = {"id": claim_id(SLUG, sk), "subject_key": sk, "claim": claim,
            "claim_type": "interpretation", "section": "battlecard", "zone": "where_we_win",
            "order": 0, "source_url": "https://anthropic.com/cloud", "source_tier": "primary",
            "evidence_excerpt": "Claude is generally available on Amazon Bedrock and Google Vertex AI.",
            "as_of": "2026-06-01", "verified": True, "confidence": "high",
            "persona": "technical_evaluator"}
    base.update(kw)
    return base


class BuildFromClaims(unittest.TestCase):
    def test_own_grounded_win_play_becomes_strength_fact(self):
        facts = strengths.build_from_claims(SLUG, [_win_play("anthropic|multicloud|current")])
        self.assertEqual(len(facts), 1)
        f = facts[0]
        self.assertTrue(f["standing_strength"])
        self.assertEqual(f["about"], "my_company")
        self.assertEqual(f["valence"], "front_foot")
        self.assertEqual(f["claim_type"], "fact")
        self.assertEqual(f["source_url"], "https://anthropic.com/cloud")
        self.assertNotIn("Soundbite", f["claim"])        # the pitch block stripped, assertion kept
        self.assertIn("Vertex", f["claim"])

    def test_derived_play_grounds_through_parent(self):
        parent = {"id": claim_id(SLUG, "anthropic|ga|current"), "subject_key": "anthropic|ga|current",
                  "claim": "Claude is GA on Bedrock.", "claim_type": "fact", "section": ANCHOR_SECTION,
                  "zone": None, "source_url": "https://news.test/ga",
                  "source_tier": "reputable_secondary",
                  "evidence_excerpt": "Claude is generally available on Bedrock.", "as_of": "2026-05-01",
                  "about": "my_company"}
        derived = _win_play("anthropic|ga-play|current", derived_from=parent["id"])
        for k in ("source_url", "source_tier", "evidence_excerpt"):   # derived play has no own source
            derived.pop(k, None)
        facts = strengths.build_from_claims(SLUG, [parent, derived])
        play_fact = next(f for f in facts if f["subject_key"] == "anthropic|ga-play|current")
        self.assertEqual(play_fact["source_url"], "https://news.test/ga")   # inherited from the parent

    def test_ungroundable_strength_is_skipped(self):
        play = _win_play("anthropic|vibes|current")
        for k in ("source_url", "source_tier", "evidence_excerpt"):
            play.pop(k, None)                            # no own source, no derived_from -> can't ground
        self.assertEqual(strengths.build_from_claims(SLUG, [play]), [])

    def test_only_our_strengths_qualify(self):
        lose = _win_play("openai|edge|current", zone="where_they_win")   # THEIR strength, not ours
        obj = {"id": claim_id(SLUG, "o|obj|c"), "subject_key": "o|obj|c",
               "section": "objection_handling", "zone": None, "claim": "x",
               "source_url": "u", "evidence_excerpt": "e", "status": "active"}
        self.assertEqual(strengths.build_from_claims(SLUG, [lose, obj]), [])

    def test_retired_strength_skipped(self):
        play = _win_play("anthropic|old|current", status="retired", retired_on="2026-06-01")
        self.assertEqual(strengths.build_from_claims(SLUG, [play]), [])


class FloorExcludesStrengths(unittest.TestCase):
    def test_strength_is_pivot_evidence_not_a_trigger(self):
        trigger = {"id": claim_id(SLUG, "openai|outage|2026-06"),
                   "subject_key": "openai|outage|2026-06"}
        strength = {"id": claim_id(SLUG, "strength::anthropic|multicloud|current"),
                    "subject_key": "anthropic|multicloud|current", "standing_strength": True}
        triggers = _trigger_fact_ids([trigger, strength])
        self.assertIn(trigger["id"], triggers)
        self.assertNotIn(strength["id"], triggers)        # strength excluded from derivable triggers
        # an op deriving FROM the strength fails the floor's provenance check
        op = {"operation": "add", "section": "objection_handling", "zone": None,
              "subject_key": "x|y|z", "claim": "**\"Q?\"**\n\nbody.\n\n**So what:** m.",
              "claim_type": "interpretation", "derived_from": strength["id"]}
        v = floor_check(op, triggers, {})
        self.assertTrue(any("does not resolve to a surviving grounded fact" in e for e in v))
        # the SAME op deriving from the real trigger has no provenance violation
        op["derived_from"] = trigger["id"]
        v2 = floor_check(op, triggers, {})
        self.assertFalse(any("derived_from" in e for e in v2))


if __name__ == "__main__":
    unittest.main()
