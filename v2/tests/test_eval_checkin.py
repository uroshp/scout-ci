"""The 14-day check-in's pre-registered go/no-go rule (docs/eval-exit-criteria.md), the promotion
instrument for the v3.5 takeover judges. Pure-function tests of the verdict logic incl. the kill branch."""
import unittest
from unittest import mock
import sys
sys.path.insert(0, "scripts")
import eval_checkin as ec


def cur(adjudicated, precision, pending=0):
    return {"adjudicated": adjudicated, "right": 0, "wrong": 0, "precision": precision, "pending": pending}


class Verdict(unittest.TestCase):
    K = "verification"   # MIN_ADJUDICATED = 15, bar 0.80

    def test_accumulate_when_too_few(self):
        self.assertEqual(ec.verdict(self.K, cur(5, 1.0), None)["status"], "ACCUMULATE")

    def test_baseline_first_time_on_bar(self):
        self.assertEqual(ec.verdict(self.K, cur(20, 0.85), None)["status"], "BASELINE")

    def test_eligible_sustained(self):
        self.assertEqual(ec.verdict(self.K, cur(20, 0.90), {"precision": 0.85})["status"], "ELIGIBLE")

    def test_watch_above_bar_but_down(self):
        self.assertEqual(ec.verdict(self.K, cur(20, 0.82), {"precision": 0.90})["status"], "WATCH")

    def test_diagnose_below_bar_improving(self):
        v = ec.verdict(self.K, cur(20, 0.60), {"precision": 0.50})
        self.assertEqual(v["status"], "DIAGNOSE")
        self.assertEqual(v["no_improve_streak"], 0)

    def test_streak_increments_then_kills(self):
        # below bar, not improving, streak builds to KILL_STREAK
        v1 = ec.verdict(self.K, cur(20, 0.50), {"precision": 0.60, "no_improve_streak": 0})
        self.assertEqual((v1["status"], v1["no_improve_streak"]), ("DIAGNOSE", 1))
        v3 = ec.verdict(self.K, cur(20, 0.50), {"precision": 0.60, "no_improve_streak": 2})
        self.assertEqual((v3["status"], v3["no_improve_streak"]), ("KILL?", 3))


if __name__ == "__main__":
    unittest.main()


class Brief(unittest.TestCase):
    """The check-in opens with the executive brief (2026-10-01); detail is the appendix."""

    def test_named_periods_and_model_slices(self):
        # 2026-11-02: the challenger moved to Sonnet 5.5; rows are sliced by named period (stamp) and
        # by the model that judged them, and the legacy pre/post slices still print
        from scout import challenger as ch
        self.assertEqual(ch.period_of("2026-09-01T00:00:00"), "pre_fix")
        self.assertEqual(ch.period_of("2026-10-01T00:00:00"), "post_fix")
        self.assertEqual(ch.period_of("2026-10-20T00:00:00"), "card_facts")
        self.assertEqual(ch.period_of("2026-11-03T04:00:00"), "sonnet_5_5")
        rows = [{"judged_at": "2026-11-03T04:00:00", "challenger_model": "claude-sonnet-5-5",
                 "items": [{"status": "disagree", "delta_id": "d1"}]},
                {"judged_at": "2026-10-20T04:00:00", "challenger_model": "claude-sonnet-4-6",
                 "items": [{"status": "disagree", "delta_id": "d2"}, {"status": "agree", "delta_id": "d3"}]}]
        with mock.patch.object(ch, "load_labels", return_value={"d1": "agree"}):
            sl = ch.scorecard_slices(rows)
        self.assertEqual(sl["current"], "sonnet_5_5")
        self.assertEqual(sl["periods"]["sonnet_5_5"]["adjudicated"], 1); self.assertEqual(sl["periods"]["card_facts"]["disagreements"], 1)
        self.assertEqual(sl["by_model"]["claude-sonnet-5-5"]["precision"], 1.0)
        self.assertEqual(sl["period"]["post_fix"]["disagreements"], 2)
        txt = ec._slices_block(sl)
        self.assertIn("NAMED PERIODS (current: sonnet_5_5", txt); self.assertIn("claude-sonnet-4-6", txt)

    def test_brief_leads_with_number_meaning_next_step(self):
        import importlib.util, os
        spec = importlib.util.spec_from_file_location("eval_checkin", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "eval_checkin.py"))
        ec = importlib.util.module_from_spec(spec); spec.loader.exec_module(ec)
        ver = {"precision": 0.417, "adjudicated": 12, "pending": 1, "right": 5, "wrong": 7,
               "slices": {"fix_stamp": "2026-09-28T00:00:00", "period": {"pre_fix": {"precision": 0.417, "adjudicated": 12, "disagreements": 12},
                                                                       "post_fix": {"precision": None, "adjudicated": 0, "disagreements": 1}}}}
        auth = {"precision": 0.991, "adjudicated": 230, "pending": 59, "right": 228, "wrong": 2}
        model = {"brief": {"results": 268, "labels": 0, "common_n": 31, "arms": [
            {"backend": "ollama_nemotron", "vendor": "NVIDIA", "tag": "nemotron-3-nano:4b-q8_0", "results": 89, "today": 12,
             "exact_coverage": 1.0, "judge_agree": 0.739, "judge_n": 11, "judge_p50_ms": 68586, "warming_up": False}]}}
        text = ec.brief({}, ver, auth, {"status": "ACCUMULATE"}, {"status": "ELIGIBLE"}, model)
        self.assertTrue(text.startswith("# Scout evals: the brief"))
        for needle in ("## 1.", "## 2.", "## 3.", "Next step", "What it means", "0 human labels", "NVIDIA nemotron-3-nano:4b-q8_0", "74%", "68 s", "LIVE"):
            self.assertIn(needle, text)
        for noise in ("|---|", "kappa=None", "tool_protocol"):
            self.assertNotIn(noise, text)

    def test_html_brief_is_cards_not_tables_of_none(self):
        import importlib.util, os
        from datetime import datetime
        spec = importlib.util.spec_from_file_location("eval_checkin", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "eval_checkin.py"))
        ec = importlib.util.module_from_spec(spec); spec.loader.exec_module(ec)
        ver = {"precision": 0.417, "adjudicated": 12, "pending": 1, "right": 5, "wrong": 7, "slices": {"fix_stamp": "2026-09-28", "period": {
            "pre_fix": {"precision": 0.417, "adjudicated": 12, "disagreements": 12}, "post_fix": {"precision": None, "adjudicated": 0, "disagreements": 1}}}}
        auth = {"precision": 0.991, "adjudicated": 230, "pending": 59, "right": 228, "wrong": 2}
        model = {"brief": {"results": 268, "labels": 0, "common_n": 31, "arms": [
            {"backend": "ollama_gemma4", "vendor": "Google", "tag": "gemma4:26b-a4b-it-qat", "results": 116, "today": 39,
             "exact_coverage": 1.0, "judge_agree": 0.739, "judge_n": 11, "judge_p50_ms": 48314, "warming_up": False, "eligible": True},
            {"backend": "apple_ondevice", "vendor": "Apple", "tag": "SystemLanguageModel", "results": 35, "today": 0,
             "exact_coverage": 0.38, "judge_agree": None, "judge_n": 4, "judge_p50_ms": None, "warming_up": True, "eligible": False}], "eligible_coverage": 0.9}}
        html = ec.brief_html(datetime(2026, 10, 1), ver, auth, {"status": "ACCUMULATE"}, {"status": "ELIGIBLE"}, model)
        for needle in ("Scout evals, 1 October 2026", "ACCUMULATE", "ELIGIBLE", "Google</b> gemma4:26b-a4b-it-qat", "74%", "48 s", "Next step", "What it means", "Comparable", ">yes<", "warming up"):
            self.assertIn(needle, html)
        self.assertNotIn("None", html.replace("none", ""))      # no raw None cells
