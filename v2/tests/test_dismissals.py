"""Dismissal capture: records what a run surfaced-but-didn't-alert (triage candidates + materiality
immaterial verdicts + own-co signals); skips a truly quiet run; no-op when shadow-eval is off."""
import json
import unittest
from unittest import mock

from scout import shadow, selfserve, config


class DismissalCapture(unittest.TestCase):
    def _capture(self, *, candidates=None, immaterial=None, my_substantial=None, enabled=True):
        cap = {}

        def fake_write(path, text, message):
            cap["path"], cap["text"] = path, text

        with mock.patch.object(config, "SHADOW_EVAL_ENABLED", enabled), \
             mock.patch.object(selfserve, "write_data", fake_write):
            shadow.dismissal_capture(
                "acme__vs__bco__x", run_ts="2026-06-29T11:00:00",
                candidates=candidates or [], immaterial=immaterial or [],
                became_material=[], alerts=[], my_substantial=my_substantial or [],
                competitor="Bco", my_company="Acme")
        return cap

    def test_records_surfaced_and_immaterial(self):
        cap = self._capture(
            candidates=[{"about": "competitor", "substantial": True, "signal": "Bco launches X",
                         "why_new": "n", "source_hint": "reuters.com"}],
            immaterial=[{"signal": "minor blog post", "why_not": "not a real change"}])
        rec = json.loads(cap["text"])
        self.assertTrue(cap["path"].startswith(f"{shadow.DISMISSAL_DIR}/acme__vs__bco__x/"))
        self.assertEqual(rec["surfaced"][0]["signal"], "Bco launches X")
        self.assertEqual(rec["materiality_immaterial"][0]["why_not"], "not a real change")

    def test_records_own_company_signals(self):
        rec = json.loads(self._capture(
            my_substantial=[{"signal": "Acme ships Y", "why_new": "n", "source_hint": "x.com"}])["text"])
        self.assertEqual(rec["my_company_substantial"][0]["signal"], "Acme ships Y")

    def test_quiet_run_writes_nothing(self):
        with mock.patch.object(config, "SHADOW_EVAL_ENABLED", True), \
             mock.patch.object(selfserve, "write_data") as w:
            shadow.dismissal_capture("s", run_ts="t", candidates=[], immaterial=[],
                                     became_material=[], alerts=[], my_substantial=[])
            w.assert_not_called()

    def test_noop_when_disabled(self):
        with mock.patch.object(config, "SHADOW_EVAL_ENABLED", False), \
             mock.patch.object(selfserve, "write_data") as w:
            shadow.dismissal_capture("s", run_ts="t", candidates=[{"signal": "x"}], immaterial=[],
                                     became_material=[], alerts=[], my_substantial=[])
            w.assert_not_called()


if __name__ == "__main__":
    unittest.main()
