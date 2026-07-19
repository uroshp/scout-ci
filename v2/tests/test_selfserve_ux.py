"""The 2026-07-19 create-your-own pass: honest failure handling + the creating/payoff UX.

Pins: a failed run requeues once then shows HUMAN copy (raw internals never reach a visitor's
browser — the 7/18 incident); every failure emails the owner and charges an estimated spend to
the ledger; progress helpers are fail-soft; the briefing box is parameterizable for user cards
while the live viewer's default header stays byte-identical. Pure, no model/network.

    python -m unittest discover -s tests
"""
import importlib
import json
import unittest
from unittest import mock

from scout import config, generate as gen, page, selfserve

run_selfserve = importlib.import_module("scripts.run_selfserve")


class FailureHandling(unittest.TestCase):
    def _fail(self, attempts):
        calls = {}
        state = {"used": 0, "free_limit": 5, "spend_usd": 0.0, "spend_ceiling_usd": 500.0}
        with mock.patch.object(selfserve, "update_request",
                               side_effect=lambda j, **f: calls.setdefault("update", f)), \
             mock.patch.object(selfserve, "load_state", return_value=dict(state)), \
             mock.patch.object(selfserve, "save_state",
                               side_effect=lambda s: calls.setdefault("state", s)), \
             mock.patch.object(run_selfserve.notify, "_dispatch",
                               side_effect=lambda subj, body, **k: calls.setdefault(
                                   "mail", {"subj": subj, "body": body})), \
             mock.patch.object(selfserve, "save_result",
                               side_effect=lambda j, rec, **k: calls.setdefault("result", rec)):
            run_selfserve._handle_failure({"job_id": "j1"}, "j1", attempts,
                                          RuntimeError("Reached maximum budget ($100)"), "now")
        return calls

    def test_first_failure_requeues_and_alerts_owner(self):
        calls = self._fail(attempts=1)
        self.assertNotIn("result", calls)                       # visitor keeps the creating page
        self.assertEqual(calls["update"], {"attempts": 1})      # attempt recorded on the request
        self.assertIn("FAILED", calls["mail"]["subj"])
        self.assertIn("Reached maximum budget", calls["mail"]["body"])   # owner gets the internals
        est = config.SELFSERVE_FAILED_RUN_SPEND_EST
        self.assertAlmostEqual(calls["state"]["spend_usd"], est)         # ledger charged

    def test_second_failure_shows_honest_copy_without_internals(self):
        calls = self._fail(attempts=2)
        rec = calls["result"]
        self.assertEqual(rec["status"], "error")
        self.assertNotIn("Claude", rec["message"])              # no tool names
        self.assertNotIn("$", rec["message"])                   # no budget figures
        self.assertIn("flagged to the owner", rec["message"])
        self.assertIn("Reached maximum budget", rec["detail_internal"])  # owner-only field


class ProgressHelpers(unittest.TestCase):
    def test_write_progress_never_raises(self):
        with mock.patch.object(selfserve, "_write", side_effect=RuntimeError("api down")):
            selfserve.write_progress("j1", "researching")       # must not raise

    def test_read_progress_swallows_garbage(self):
        with mock.patch.object(selfserve, "_read", return_value="{not json"):
            self.assertIsNone(selfserve.read_progress("j1"))

    def test_update_request_merges(self):
        store = {"selfserve/requests/j1.json": json.dumps({"job_id": "j1", "status": "queued"})}
        with mock.patch.object(selfserve, "_read", side_effect=lambda p: store.get(p)), \
             mock.patch.object(selfserve, "_write",
                               side_effect=lambda p, t, m: store.__setitem__(p, t)):
            out = selfserve.update_request("j1", attempts=1)
        self.assertEqual(out["attempts"], 1)
        self.assertEqual(json.loads(store["selfserve/requests/j1.json"])["status"], "queued")


class StageHook(unittest.TestCase):
    def test_emit_stage_is_fail_soft(self):
        with mock.patch.object(gen, "_ON_STAGE", lambda s: (_ for _ in ()).throw(RuntimeError())):
            gen._emit_stage("researching")                      # must not raise

    def test_stage_tables_agree(self):
        """Every anchor stage has a message bucket and vice versa — the JS and Streamlit UIs
        consume both tables in lockstep."""
        self.assertEqual(set(page.STAGE_ANCHORS), set(page.STAGE_BUCKETS))
        self.assertTrue(set(page.STAGE_BUCKETS.values()) <= set(page.PROGRESS_MESSAGES))


class BriefingHeader(unittest.TestCase):
    CLAIMS = [{"section": "executive_summary", "order": 0, "status": "active",
               "claim": "**Lead.**\n\nBody.\n\n**So what:** move.", "claim_type": "interpretation",
               "subject_key": "a|lead|c", "source_url": "https://s/x"}]

    def test_user_card_label_renders(self):
        html_out = page.static_brief_html(self.CLAIMS, "", briefing=True,
                                          briefing_label="Your 2-minute brief",
                                          briefing_tag="the fast read first")
        self.assertIn("Your 2-minute brief", html_out)
        self.assertIn("the fast read first", html_out)
        self.assertNotIn("Your Daily Briefing", html_out)

    def test_viewer_default_is_untouched(self):
        html_out = page.static_brief_html(self.CLAIMS, "", briefing=True)
        self.assertIn("Your Daily Briefing", html_out)
        self.assertIn("refreshed today", html_out)

    def test_no_briefing_by_default(self):
        html_out = page.static_brief_html(self.CLAIMS, "")
        self.assertNotIn('class="briefing"', html_out)


if __name__ == "__main__":
    unittest.main()
