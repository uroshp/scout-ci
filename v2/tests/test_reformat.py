"""The no-drop guarantee (scout.reformat.repair_or_hold): a confirmed material update is published
(as-is or after a format repair) or HELD + flagged — it is NEVER cut and NEVER silently dropped.
The model reformat call is injected so these are deterministic, no-API tests."""
import unittest
from unittest import mock

from scout import reformat

GOOD = {"section": "objection_handling", "zone": None,
        "claim": '**"Q?"**\n\nbody.\n\n**So what:** the move.'}
BAD = {"section": "objection_handling", "zone": None, "subject_key": "x|obj|current",
       "claim": '**"Q?"**\n\nbody only, no marker.'}


def _boom(*a):
    raise AssertionError("reformatter should not be called for a well-formed claim")


class RepairOrHold(unittest.TestCase):
    def test_wellformed_passes_untouched(self):
        status, c = reformat.repair_or_hold("s", GOOD, reformatter=_boom)
        self.assertEqual(status, "ok")
        self.assertEqual(c, GOOD)

    def test_malformed_repaired_when_reformatter_succeeds(self):
        fixed = '**"Q?"**\n\nbody only, no marker.\n\n**So what:** do the thing.'
        status, c = reformat.repair_or_hold("s", BAD, reformatter=lambda *a: fixed)
        self.assertEqual(status, "repaired")
        self.assertEqual(c["claim"], fixed)
        self.assertEqual(reformat.schema.render_structure_errors(c), [])   # actually renders now

    def test_unrepairable_is_held_and_flagged_never_cut(self):
        with mock.patch.object(reformat.selfserve, "write_data") as w, \
             mock.patch.object(reformat, "_alert_human") as alert:
            status, c = reformat.repair_or_hold("s", BAD, reformatter=lambda *a: None)
        self.assertEqual(status, "held")
        self.assertTrue(w.called, "held update must be persisted (pending_publish), not dropped")
        self.assertTrue(alert.called, "held update must be flagged to the human")

    def test_reformatter_returning_still_malformed_is_held_not_published(self):
        # defense in depth: even a buggy reformatter that returns markerless text must NOT publish
        with mock.patch.object(reformat.selfserve, "write_data"), \
             mock.patch.object(reformat, "_alert_human"):
            status, _ = reformat.repair_or_hold("s", BAD, reformatter=lambda *a: "still no marker")
        self.assertEqual(status, "held")

    def test_only_three_outcomes_never_a_drop(self):
        for rf in (lambda *a: GOOD["claim"], lambda *a: None, _boom if False else (lambda *a: None)):
            with mock.patch.object(reformat.selfserve, "write_data"), mock.patch.object(reformat, "_alert_human"):
                status, _ = reformat.repair_or_hold("s", BAD, reformatter=rf)
            self.assertIn(status, ("ok", "repaired", "held"))


if __name__ == "__main__":
    unittest.main()
