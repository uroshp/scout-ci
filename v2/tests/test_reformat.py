"""The no-drop guarantee (scout.reformat.repair_or_hold): a confirmed material update is published
(as-is or after a format/persona repair) or HELD + flagged — it is NEVER cut and NEVER silently
dropped. The model reformat + persona-classify calls are injected so these are deterministic, no-API."""
import unittest
from unittest import mock

from scout import reformat

# well-formed: So-what block AND a persona (the buyer badge)
GOOD = {"section": "objection_handling", "zone": None, "persona": "economic_buyer",
        "claim": '**"Q?"**\n\nbody.\n\n**So what:** the move.'}
# malformed: no So-what block, no persona
BAD = {"section": "objection_handling", "zone": None, "subject_key": "x|obj|current",
       "claim": '**"Q?"**\n\nbody only, no marker.'}

_PERSONA = lambda *a: "economic_buyer"          # stub classifier


def _boom(*a):
    raise AssertionError("repair helper should not be called when nothing of its kind is missing")


class RepairOrHold(unittest.TestCase):
    def test_wellformed_passes_untouched(self):
        status, c = reformat.repair_or_hold("s", GOOD, reformatter=_boom, persona_classifier=_boom)
        self.assertEqual(status, "ok")
        self.assertEqual(c, GOOD)

    def test_missing_block_and_persona_both_repaired(self):
        fixed = '**"Q?"**\n\nbody only, no marker.\n\n**So what:** do the thing.'
        status, c = reformat.repair_or_hold("s", BAD, reformatter=lambda *a: fixed, persona_classifier=_PERSONA)
        self.assertEqual(status, "repaired")
        self.assertEqual(c["claim"], fixed)
        self.assertEqual(c["persona"], "economic_buyer")
        self.assertEqual(reformat.schema.render_structure_errors(c), [])

    def test_missing_persona_only_is_classified(self):
        no_persona = {"section": "objection_handling", "zone": None,
                      "claim": '**"Q?"**\n\nbody.\n\n**So what:** m.'}   # block present, persona absent
        status, c = reformat.repair_or_hold("s", no_persona, reformatter=_boom,
                                            persona_classifier=lambda *a: "exec_top_down")
        self.assertEqual(status, "repaired")
        self.assertEqual(c["persona"], "exec_top_down")

    def test_unrepairable_block_is_held_and_flagged_never_cut(self):
        with mock.patch.object(reformat.selfserve, "write_data") as w, \
             mock.patch.object(reformat, "_alert_human") as alert:
            status, _ = reformat.repair_or_hold("s", BAD, reformatter=lambda *a: None, persona_classifier=_PERSONA)
        self.assertEqual(status, "held")
        self.assertTrue(w.called, "held update must be persisted (pending_publish), not dropped")
        self.assertTrue(alert.called, "held update must be flagged to the human")

    def test_reformatter_returning_still_malformed_is_held(self):
        with mock.patch.object(reformat.selfserve, "write_data"), mock.patch.object(reformat, "_alert_human"):
            status, _ = reformat.repair_or_hold("s", BAD, reformatter=lambda *a: "still no marker",
                                                persona_classifier=_PERSONA)
        self.assertEqual(status, "held")

    def test_only_three_outcomes_never_a_drop(self):
        for rf, pc in ((lambda *a: GOOD["claim"], _PERSONA), (lambda *a: None, _PERSONA),
                       (lambda *a: None, lambda *a: None)):
            with mock.patch.object(reformat.selfserve, "write_data"), mock.patch.object(reformat, "_alert_human"):
                status, _ = reformat.repair_or_hold("s", BAD, reformatter=rf, persona_classifier=pc)
            self.assertIn(status, ("ok", "repaired", "held"))


if __name__ == "__main__":
    unittest.main()
