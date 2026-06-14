"""Propagation FLOOR invariants (spec §17 thesis-governance (b)).

Propagation is the first user-facing content gated by a model with no model-free authority behind
it. The deterministic floor IS that authority: a model-free grader that rejects structurally unsound
ops BEFORE the adversarial Opus judge ever sees them, so nothing the judge confirms can reach a card
unless the structure is sound. These tests pin the invariants the floor must enforce — no model call,
no network, pure functions. Run from v2/:

    python -m unittest discover -s tests
"""
import unittest

from scout.propagate import floor_check, _active_targets, _decision_records


# Two active claims to target: a play (battlecard) and an objection (objection_handling).
CLAIMS = [
    {"subject_key": "openai|flagship-model|current", "section": "battlecard",
     "zone": "where_they_win", "status": "active", "claim": "They lead on the frontier model."},
    {"subject_key": "anthropic|vendor-access|current", "section": "objection_handling",
     "zone": None, "status": "active", "claim": "Is access stable?"},
    # A retired claim must NOT be a valid revise/retire target (it is off the active card).
    {"subject_key": "openai|old-play|current", "section": "battlecard", "zone": "where_we_win",
     "status": "retired", "claim": "An old, retired play."},
]
FACT_ID = "c_aaaaaaaaaaaa"          # the one surviving grounded fact every op anchors to
FACTS = {FACT_ID}
ACTIVE = _active_targets(CLAIMS)


def _add(**over):
    op = {"operation": "add", "section": "objection_handling", "zone": None, "valence": "back_foot",
          "target_subject_key": None, "subject_key": "anthropic|gov-restriction|current",
          "claim": "Buyer raises the restriction. Answer: the stated scope is X.",
          "claim_type": "interpretation", "derived_from": FACT_ID}
    op.update(over)
    return op


def _revise(**over):
    op = {"operation": "revise", "section": "battlecard", "zone": "where_they_win",
          "valence": "front_foot", "target_subject_key": "openai|flagship-model|current",
          "subject_key": "openai|flagship-model|current", "claim": "Their lead is narrower now.",
          "claim_type": "interpretation", "derived_from": FACT_ID}
    op.update(over)
    return op


def _retire(**over):
    op = {"operation": "retire", "section": "battlecard", "zone": "where_they_win",
          "valence": "front_foot", "target_subject_key": "openai|flagship-model|current",
          "subject_key": "openai|flagship-model|current", "claim": None,
          "claim_type": "interpretation", "derived_from": FACT_ID,
          "retired_reason": "invalidated: the model was pulled"}
    op.update(over)
    return op


class FloorAccepts(unittest.TestCase):
    def test_valid_add(self):
        self.assertEqual(floor_check(_add(), FACTS, ACTIVE), [])

    def test_valid_revise(self):
        self.assertEqual(floor_check(_revise(), FACTS, ACTIVE), [])

    def test_valid_retire_neutralized(self):
        op = _retire(retired_reason="neutralized: the gap is now a wash")
        self.assertEqual(floor_check(op, FACTS, ACTIVE), [])


class FloorRejects(unittest.TestCase):
    """Each invariant from the thesis-governance floor gets one failing case."""

    def test_no_model_minted_facts(self):
        # The cardinal rule: propagation only ever authors interpretations.
        self.assertTrue(floor_check(_add(claim_type="fact"), FACTS, ACTIVE))

    def test_derived_from_must_resolve(self):
        # Provenance anchor must point at a surviving grounded fact.
        self.assertTrue(floor_check(_add(derived_from="c_bbbbbbbbbbbb"), FACTS, ACTIVE))

    def test_derived_from_malformed(self):
        self.assertTrue(floor_check(_add(derived_from="not-an-id"), FACTS, ACTIVE))

    def test_revise_target_must_be_active(self):
        self.assertTrue(floor_check(_revise(target_subject_key="ghost|x|y",
                                            subject_key="ghost|x|y"), FACTS, ACTIVE))

    def test_retire_target_cannot_be_already_retired(self):
        # A retired claim is off the active card — it is not a valid target.
        op = _retire(target_subject_key="openai|old-play|current",
                     subject_key="openai|old-play|current")
        self.assertTrue(floor_check(op, FACTS, ACTIVE))

    def test_add_cannot_overwrite_active_claim(self):
        # An add that collides with an existing active subject_key should be a revise/retire.
        self.assertTrue(floor_check(_add(subject_key="openai|flagship-model|current"),
                                    FACTS, ACTIVE))

    def test_revise_must_reuse_subject_key_in_place(self):
        self.assertTrue(floor_check(_revise(subject_key="openai|flagship-model|2026"),
                                    FACTS, ACTIVE))

    def test_retire_must_clear_prose(self):
        self.assertTrue(floor_check(_retire(claim="still here"), FACTS, ACTIVE))

    def test_retire_reason_must_be_tagged(self):
        self.assertTrue(floor_check(_retire(retired_reason="because"), FACTS, ACTIVE))

    def test_objection_must_have_null_zone(self):
        self.assertTrue(floor_check(_add(zone="where_we_win"), FACTS, ACTIVE))

    def test_battlecard_needs_a_zone(self):
        self.assertTrue(floor_check(_revise(zone=None), FACTS, ACTIVE))

    def test_unknown_operation(self):
        self.assertTrue(floor_check(_add(operation="delete"), FACTS, ACTIVE))

    def test_add_must_carry_prose(self):
        self.assertTrue(floor_check(_add(claim="  "), FACTS, ACTIVE))


class DecisionRecords(unittest.TestCase):
    """The decision log is the audit trail AND the judge's training corpus — its shape is load-bearing."""

    def test_floored_op_is_logged_as_floor_reject_uncommitted(self):
        ops = [_add(claim_type="fact")]                       # floored
        floor_results = [floor_check(ops[0], FACTS, ACTIVE)]
        recs = _decision_records(ops, floor_results, {}, {FACT_ID: {"source_url": "https://x"}}, ACTIVE)
        self.assertEqual(recs[0]["judge_verdict"], "floor_reject")
        self.assertFalse(recs[0]["committed"])
        self.assertTrue(recs[0]["floor_violations"])

    def test_confirmed_op_carries_old_text_and_commits(self):
        ops = [_revise()]                                    # floor-clean
        recs = _decision_records(
            ops, [[]], {0: {"verdict": "confirm", "reason": "ok"}},
            {FACT_ID: {"source_url": "https://x"}}, ACTIVE)
        self.assertEqual(recs[0]["judge_verdict"], "confirm")
        self.assertTrue(recs[0]["committed"])
        # old_text is pulled from the targeted active claim; new_text is the op's prose.
        self.assertEqual(recs[0]["old_text"], "They lead on the frontier model.")
        self.assertEqual(recs[0]["new_text"], "Their lead is narrower now.")

    def test_missing_verdict_fails_closed_to_reject(self):
        # A floor-clean op the judge returned no verdict for must NOT commit.
        recs = _decision_records(
            [_revise()], [[]], {}, {FACT_ID: {"source_url": "https://x"}}, ACTIVE)
        self.assertEqual(recs[0]["judge_verdict"], "reject")
        self.assertFalse(recs[0]["committed"])


if __name__ == "__main__":
    unittest.main()
