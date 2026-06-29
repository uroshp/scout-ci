"""Consequentiality-filter capture (the gate's decisions), shadow-first. The verdict must be logged
for the longitudinal eval, in its OWN store (separate from the v3.5 grounding capture, so it can
never alter what that eval sees — docs/consequential-filter-spec.md, Fold A)."""
import json
import unittest
from unittest import mock

from scout import shadow, config, selfserve


class FilterCapture(unittest.TestCase):
    def _capture(self, verdict, enabled=True):
        captured = {}

        def fake_write(path, text, message):
            captured["path"], captured["text"] = path, text

        with mock.patch.object(config, "SHADOW_EVAL_ENABLED", enabled), \
             mock.patch.object(selfserve, "write_data", fake_write):
            shadow.filter_capture(
                "acme__vs__bco__x", run_ts="2026-06-28T11:00:00", verdict=verdict,
                act_subject_keys=["recent_moves | k | 2026"], competitor="Bco",
                my_company="Acme", mode="shadow")
        return captured

    def test_records_verdict_in_its_own_store(self):
        c = self._capture({"consequential": True, "consequence_rationale": "shifts the thesis",
                           "headline": "X"})
        rec = json.loads(c["text"])
        self.assertTrue(c["path"].startswith(f"{shadow.FILTER_DIR}/acme__vs__bco__x/"))
        self.assertNotEqual(shadow.FILTER_DIR, shadow.SHADOW_DIR)   # separate from grounding capture
        self.assertIs(rec["consequential"], True)
        self.assertEqual(rec["consequence_rationale"], "shifts the thesis")
        self.assertEqual(rec["mode"], "shadow")
        self.assertEqual(rec["act_subject_keys"], ["recent_moves | k | 2026"])

    def test_routine_verdict(self):
        rec = json.loads(self._capture({"consequential": False})["text"])
        self.assertIs(rec["consequential"], False)

    def test_missing_consequential_is_none_not_crash(self):
        rec = json.loads(self._capture({"headline": "no verdict field"})["text"])
        self.assertIsNone(rec["consequential"])

    def test_noop_when_shadow_eval_disabled(self):
        with mock.patch.object(config, "SHADOW_EVAL_ENABLED", False), \
             mock.patch.object(selfserve, "write_data") as w:
            shadow.filter_capture("s", run_ts="t", verdict={"consequential": True},
                                  act_subject_keys=[])
            w.assert_not_called()

    def test_never_raises_on_bad_verdict(self):
        # verdict not a dict -> swallowed, no write, no crash (live write path contract)
        with mock.patch.object(config, "SHADOW_EVAL_ENABLED", True), \
             mock.patch.object(selfserve, "write_data") as w:
            shadow.filter_capture("s", run_ts="t", verdict="oops", act_subject_keys=[])
            rec = json.loads(w.call_args[0][1])
            self.assertIsNone(rec["consequential"])


if __name__ == "__main__":
    unittest.main()
