"""The no-drop guarantee (scout.reformat.repair_or_hold): a confirmed material update is published
(as-is or after a format/persona repair) or HELD + flagged — it is NEVER cut and NEVER silently
dropped. The model reformat + persona-classify calls are injected so these are deterministic, no-API."""
import json
import unittest
from unittest import mock

from scout import reformat

# well-formed: So-what block AND a persona (the buyer badge)
GOOD = {"section": "objection_handling", "zone": None, "persona": "economic_buyer",
        "claim": '**"Q?"**\n\nbody.\n\n**So what:** the move.'}
# malformed: no So-what block, no persona
BAD = {"section": "objection_handling", "zone": None, "subject_key": "x|obj|current",
       "claim": '**"Q?"**\n\nbody only, no marker.'}

_PERSONA = lambda *a, **k: "economic_buyer"          # stub classifier


def _boom(*a, **k):
    raise AssertionError("repair helper should not be called when nothing of its kind is missing")


class RepairOrHold(unittest.TestCase):
    def test_wellformed_passes_untouched(self):
        status, c = reformat.repair_or_hold("s", GOOD, reformatter=_boom, persona_classifier=_boom)
        self.assertEqual(status, "ok")
        self.assertEqual(c, GOOD)

    def test_missing_block_and_persona_both_repaired(self):
        fixed = '**"Q?"**\n\nbody only, no marker.\n\n**So what:** do the thing.'
        status, c = reformat.repair_or_hold("s", BAD, reformatter=lambda *a, **k: fixed, persona_classifier=_PERSONA)
        self.assertEqual(status, "repaired")
        self.assertEqual(c["claim"], fixed)
        self.assertEqual(c["persona"], "economic_buyer")
        self.assertEqual(reformat.schema.render_structure_errors(c), [])

    def test_missing_persona_only_is_classified(self):
        no_persona = {"section": "objection_handling", "zone": None,
                      "claim": '**"Q?"**\n\nbody.\n\n**So what:** m.'}   # block present, persona absent
        status, c = reformat.repair_or_hold("s", no_persona, reformatter=_boom,
                                            persona_classifier=lambda *a, **k: "exec_top_down")
        self.assertEqual(status, "repaired")
        self.assertEqual(c["persona"], "exec_top_down")

    def test_unrepairable_block_is_held_and_flagged_never_cut(self):
        with mock.patch.object(reformat.selfserve, "write_data") as w, \
             mock.patch.object(reformat, "_alert_human") as alert:
            status, _ = reformat.repair_or_hold("s", BAD, reformatter=lambda *a, **k: None, persona_classifier=_PERSONA)
        self.assertEqual(status, "held")
        self.assertTrue(w.called, "held update must be persisted (pending_publish), not dropped")
        self.assertTrue(alert.called, "held update must be flagged to the human")

    def test_reformatter_returning_still_malformed_is_held(self):
        with mock.patch.object(reformat.selfserve, "write_data"), mock.patch.object(reformat, "_alert_human"):
            status, _ = reformat.repair_or_hold("s", BAD, reformatter=lambda *a, **k: "still no marker",
                                                persona_classifier=_PERSONA)
        self.assertEqual(status, "held")

    def test_only_three_outcomes_never_a_drop(self):
        for rf, pc in ((lambda *a, **k: GOOD["claim"], _PERSONA), (lambda *a, **k: None, _PERSONA),
                       (lambda *a, **k: None, lambda *a, **k: None)):
            with mock.patch.object(reformat.selfserve, "write_data"), mock.patch.object(reformat, "_alert_human"):
                status, _ = reformat.repair_or_hold("s", BAD, reformatter=rf, persona_classifier=pc)
            self.assertIn(status, ("ok", "repaired", "held"))


# over the cap: 200+ words, block present — the 2026-07-25 hold class (length-only violation)
LONG = {"section": "objection_handling", "zone": None, "persona": "economic_buyer",
        "subject_key": "x|obj|current",
        "claim": '**"Q?"**\n\n' + ("beat " * 200) + '\n\n**So what:** the move.'}
SHORT_OK = '**"Q?"**\n\nCurrent state, numbers kept.\n\n**So what:** the move.'


class CondenseAndVerify(unittest.TestCase):
    """2026-07-25 'cure, don't hold' + 're-judge everywhere': a length-only violation now triggers
    the condenser (it used to go straight to hold with zero repair attempts), and any condense that
    changed judge-confirmed prose must pass the fidelity judge before it can publish."""

    def test_length_only_violation_triggers_the_condenser(self):
        calls = []
        def rf(text, section, zone, **kw):
            calls.append(kw)
            return SHORT_OK
        status, c = reformat.repair_or_hold("s", LONG, reformatter=rf,
                                            persona_classifier=_boom,
                                            condense_verifier=lambda *a, **k: (True, "faithful"))
        self.assertEqual(status, "repaired")
        self.assertEqual(c["claim"], SHORT_OK)
        self.assertEqual(len(calls), 1)
        self.assertTrue(any("render cap" in e for e in calls[0]["errors"]))

    def test_condense_that_fails_fidelity_verification_is_held(self):
        with mock.patch.object(reformat.selfserve, "write_data") as w, \
             mock.patch.object(reformat, "_alert_human"):
            status, _ = reformat.repair_or_hold(
                "s", LONG, reformatter=lambda *a, **k: SHORT_OK, persona_classifier=_boom,
                condense_verifier=lambda *a, **k: (False, "dropped the $1.5B figure"))
        self.assertEqual(status, "held")
        self.assertTrue(w.called)
        rec = json.loads(w.call_args[0][1])
        self.assertIn("condense failed re-verification", rec["reason"])
        self.assertIn("dropped the $1.5B figure", rec["reason"])

    def test_missing_block_repair_skips_the_fidelity_judge(self):
        def no_verify(*a, **k):
            raise AssertionError("a block repair must not spend a fidelity-judge call")
        fixed = '**"Q?"**\n\nbody only, no marker.\n\n**So what:** do the thing.'
        status, c = reformat.repair_or_hold("s", BAD, reformatter=lambda *a, **k: fixed,
                                            persona_classifier=_PERSONA,
                                            condense_verifier=no_verify)
        self.assertEqual(status, "repaired")

    def test_verifier_gets_original_and_condensed(self):
        seen = {}
        def verifier(original, condensed, section=None, zone=None, cost=None):
            seen.update(original=original, condensed=condensed)
            return (True, "ok")
        reformat.repair_or_hold("s", LONG, reformatter=lambda *a, **k: SHORT_OK,
                                persona_classifier=_boom, condense_verifier=verifier)
        self.assertEqual(seen["original"], LONG["claim"])
        self.assertEqual(seen["condensed"], SHORT_OK)

    def test_cost_accumulator_reaches_the_reformatter(self):
        acc = {}
        def rf(text, section, zone, errors=None, cost=None, **kw):
            if cost is not None:
                cost["reformat"] = cost.get("reformat", 0.0) + 0.05
            return SHORT_OK
        reformat.repair_or_hold("s", LONG, reformatter=rf, persona_classifier=_boom,
                                condense_verifier=lambda *a, **k: (True, "ok"), cost=acc)
        self.assertAlmostEqual(acc["reformat"], 0.05)


if __name__ == "__main__":
    unittest.main()
