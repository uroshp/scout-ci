"""Detection-gate invariants born from the 2026-07-01 Fable-lift miss: the deterministic
escalation floor (a tracked-subject candidate escalates regardless of the cheap triage grade),
my_company about-tag normalization (company-name tags must not slip through the competitor arm),
and window-hold semantics (an unrelated alert or one empty re-scan must never erase a held
window; abandonment is bounded and loud). Run from v2/:  python -m unittest discover -s tests
"""
import unittest
from unittest import mock

from scout import config, monitor


class EscalationFloor(unittest.TestCase):
    CLAIMS = [{"subject_key": "objection | model-govt-disable-fable-mythos | current"},
              {"subject_key": "openai | ipo-filing | 2026"}]

    def test_minor_grade_on_tracked_subject_is_forced_substantial(self):
        c = [{"subject_key": "objection | model-govt-disable-fable-mythos | current",
              "substantial": False}]
        monitor._escalation_floor(c, self.CLAIMS)
        self.assertTrue(c[0]["substantial"])
        self.assertEqual(c[0]["escalated_by"], "tracked_subject_floor")

    def test_new_and_missing_subject_keys_keep_their_grade(self):
        c = [{"subject_key": "NEW", "substantial": False},
             {"substantial": False}]                      # the 7/1 candidate omitted the field
        monitor._escalation_floor(c, self.CLAIMS)
        self.assertFalse(c[0]["substantial"])
        self.assertFalse(c[1]["substantial"])
        self.assertNotIn("escalated_by", c[0])

    def test_already_substantial_is_untouched(self):
        c = [{"subject_key": "openai | ipo-filing | 2026", "substantial": True}]
        monitor._escalation_floor(c, self.CLAIMS)
        self.assertTrue(c[0]["substantial"])
        self.assertNotIn("escalated_by", c[0])


class MyCompanyTag(unittest.TestCase):
    def test_literal_my_company_tag(self):
        self.assertTrue(monitor._is_mine({"about": "my_company"}, "Anthropic"))

    def test_company_name_tag_counts_as_mine(self):
        # 7/1: the same Anthropic story was tagged "anthropic" one run and "my_company" the next;
        # the name-tagged one routed through the competitor arm.
        self.assertTrue(monitor._is_mine({"about": "Anthropic"}, "Anthropic"))
        self.assertTrue(monitor._is_mine({"about": " anthropic "}, "Anthropic"))

    def test_competitor_tag_is_not_mine(self):
        self.assertFalse(monitor._is_mine({"about": "competitor"}, "Anthropic"))
        self.assertFalse(monitor._is_mine({"about": "OpenAI"}, "Anthropic"))

    def test_no_my_company_configured(self):
        self.assertFalse(monitor._is_mine({"about": "Anthropic"}, None))
        self.assertTrue(monitor._is_mine({"about": "my_company"}, None))


class WindowHold(unittest.TestCase):
    """unresolved_since must survive unrelated alerts and empty re-scans, resolve on a matching
    alert, and abandon loudly (never silently) at MONITOR_MAX_UNRESOLVED_RETRIES."""

    def setUp(self):
        self.assertGreaterEqual(config.MONITOR_MAX_UNRESOLVED_RETRIES, 2)

    def _held_meta(self, attempts=1, subjects=("anthropic | government-foreign-access-ban",)):
        return {"unresolved_since": "2026-06-27", "unresolved_attempts": attempts,
                "unresolved_subjects": list(subjects)}

    def test_unrelated_alert_keeps_the_window(self):
        meta, result = self._held_meta(), {}
        monitor._resolve_or_hold(meta, [{"subject_key": "github-copilot | billing-backlash"}], result)
        self.assertEqual(meta["unresolved_since"], "2026-06-27")
        self.assertEqual(meta["unresolved_attempts"], 2)
        self.assertIn("unresolved_held", result)

    def test_matching_alert_resolves_the_window(self):
        meta, result = self._held_meta(), {}
        monitor._resolve_or_hold(meta, [{"subject_key": "anthropic | government-foreign-access-ban"}], result)
        self.assertNotIn("unresolved_since", meta)
        self.assertNotIn("unresolved_attempts", meta)
        self.assertNotIn("unresolved_subjects", meta)
        self.assertIn("unresolved_resolved", result)

    def test_legacy_window_without_subjects_is_kept(self):
        # Pre-fix holds stored no subjects; nothing can match, so they persist to the bound.
        meta, result = {"unresolved_since": "2026-06-27", "unresolved_attempts": 1}, {}
        monitor._resolve_or_hold(meta, [{"subject_key": "github-copilot | billing-backlash"}], result)
        self.assertEqual(meta["unresolved_since"], "2026-06-27")

    def test_no_window_is_a_noop(self):
        meta, result = {"last_checked": "2026-07-01T11:00:00"}, {}
        monitor._resolve_or_hold(meta, [{"subject_key": "x"}], result)
        self.assertEqual(meta, {"last_checked": "2026-07-01T11:00:00"})
        self.assertEqual(result, {})

    def test_bound_abandons_loudly_and_clears(self):
        meta, result = self._held_meta(attempts=config.MONITOR_MAX_UNRESOLVED_RETRIES - 1), {}
        monitor._hold_window(meta, "2026-06-27", result)
        self.assertNotIn("unresolved_since", meta)
        self.assertNotIn("unresolved_subjects", meta)
        self.assertIn("abandoned_window", result)
        self.assertEqual(result["abandoned_window"]["subjects"],
                         ["anthropic | government-foreign-access-ban"])

    def test_hold_below_bound_increments(self):
        meta, result = self._held_meta(attempts=0), {}
        monitor._hold_window(meta, "2026-06-27", result)
        self.assertEqual(meta["unresolved_attempts"], 1)
        self.assertEqual(result["unresolved_held"], {"since": "2026-06-27", "attempt": 1})


if __name__ == "__main__":
    unittest.main()


class RunControls(unittest.TestCase):
    """SCOUT_MONITOR_SLUGS / force (2026-09-28): a dry test run or a signal-triggered run checks only
    the named cards; the scheduled run (no slugs) is unchanged."""

    def test_slug_filter_checks_only_the_named_cards(self):
        from scout import monitor
        seen = []
        def fake_check(slug, write=False):
            seen.append(slug)
            return {"slug": slug, "alerts": [], "material": [], "cost": {"triage": 0.1, "materiality": 0.0},
                    "decisions": [], "no_change": True, "my_company_error": None}
        with mock.patch("scout.display.list_battlecards", return_value=["a", "b", "c"]), \
             mock.patch.object(monitor, "check", side_effect=fake_check), \
             mock.patch.object(monitor.store, "load_meta", return_value={"monitored": True}), \
             mock.patch.object(monitor, "_persist_run_cost"), \
             mock.patch("scout.conseq.maybe_notify_ready"), \
             mock.patch("scout.notify.send_digest"):
            out = monitor._run_all_impl(write=False, send=False, email_dry_run=True, force=True, slugs=["b"])
        self.assertEqual(seen, ["b"])
        self.assertEqual([r.get("skipped") for r in out if r["slug"] != "b"], ["not selected", "not selected"])

    def test_no_slugs_means_every_due_card(self):
        from scout import monitor
        seen = []
        def fake_check(slug, write=False):
            seen.append(slug)
            return {"slug": slug, "alerts": [], "material": [], "cost": {"triage": 0.1, "materiality": 0.0},
                    "decisions": [], "no_change": True, "my_company_error": None}
        with mock.patch("scout.display.list_battlecards", return_value=["a", "b"]), \
             mock.patch.object(monitor, "check", side_effect=fake_check), \
             mock.patch.object(monitor.store, "load_meta", return_value={"monitored": True}), \
             mock.patch.object(monitor, "_persist_run_cost"), \
             mock.patch("scout.conseq.maybe_notify_ready"), \
             mock.patch("scout.notify.send_digest"):
            monitor._run_all_impl(write=False, send=False, email_dry_run=True, force=True)
        self.assertEqual(seen, ["a", "b"])


class LiveMode(unittest.TestCase):
    """The authorship judge goes LIVE (decided 2026-09-28, wired 2026-09-29): confirmed ops apply
    through the review path's protections (anchor refresh + provenance gate), and the run sends ONE
    cumulative FYI plus ONE "needs you" email only when something needs him. Nothing per card."""

    @staticmethod
    def _res(slug, alerts=(), decisions=(), applied=(), provenance=(), health=None, gated=None):
        return {"slug": slug, "alerts": list(alerts), "material": [], "cost": {"triage": 0.1, "materiality": 0.2},
                "no_change": not alerts, "my_company_error": None, "pipeline_health": health, "last_checked": "t",
                "propagation": {"decisions": list(decisions), "applied": list(applied), "held": [], "skipped": [],
                                "provenance_issues": list(provenance), "gated": gated, "run_verdict": None}}

    def test_one_fyi_and_one_issues_email_per_run(self):
        from scout import monitor, config
        ok_op = {"subject_key": "x | y", "operation": "revise", "judge_verdict": "confirm", "new_text": "n", "old_text": "o", "feed_note": "moved"}
        held_op = {"subject_key": "h | h", "operation": "add", "judge_verdict": "confirm", "held_for_format": True, "new_text": "long", "held_reason": "over cap"}
        results = {
            "a": self._res("a", alerts=[{"headline": "price", "old_value": "1", "new_value": "2"}], decisions=[ok_op, held_op], applied=[{"subject_key": "x | y", "operation": "revise"}]),
            "b": self._res("b", decisions=[{"subject_key": "q | q", "operation": "revise", "judge_verdict": "gated_routine"}], gated="routine"),
            "c": self._res("c", provenance=["citation mismatch on z"], health="judge unavailable on c"),
            "d": self._res("d"),
        }
        sent = {}
        with mock.patch("scout.display.list_battlecards", return_value=list(results)), \
             mock.patch.object(monitor, "check", side_effect=lambda slug, write=False: results[slug]), \
             mock.patch.object(monitor.store, "load_meta", return_value={"monitored": True, "competitor": "X", "my_company": "Y"}), \
             mock.patch.object(monitor, "_persist_run_cost"), mock.patch("scout.conseq.maybe_notify_ready"), \
             mock.patch.object(config, "PROPAGATE_MODE", "live"), mock.patch.object(config, "CONSEQUENTIAL_FILTER", "off"), \
             mock.patch("scout.notify.send_digest") as digest, mock.patch("scout.notify.send_propagation_proposals") as props, \
             mock.patch("scout.notify.send_lead_election_fyi") as lead, mock.patch("scout.notify._dispatch") as disp, \
             mock.patch("scout.notify.send_run_fyi", side_effect=lambda cards, cost, dry_run=True: sent.__setitem__("fyi", (cards, cost)) or {"sent": True}) as fyi, \
             mock.patch("scout.notify.send_run_issues", side_effect=lambda cards, dry_run=True: sent.__setitem__("issues", cards) or {"sent": True}) as iss:
            monitor._run_all_impl(write=False, send=True, email_dry_run=True, force=True)
        digest.assert_not_called(); props.assert_not_called(); lead.assert_not_called(); disp.assert_not_called()
        fyi.assert_called_once(); iss.assert_called_once()
        cards, cost = sent["fyi"]
        self.assertEqual([c["slug"] for c in cards], ["a", "b"])                       # d was quiet, c had only issues
        self.assertEqual([d["subject_key"] for d in cards[0]["applied"]], ["x | y"])   # the held op is NOT in "applied"
        self.assertEqual(cards[1]["deferred_n"], 1); self.assertAlmostEqual(cost, 1.2)
        issues = sent["issues"]
        self.assertEqual([c["slug"] for c in issues], ["a", "c"])
        self.assertEqual([d["subject_key"] for d in issues[0]["held"]], ["h | h"])
        self.assertEqual(issues[1]["provenance_issues"], ["citation mismatch on z"]); self.assertIn("judge unavailable", issues[1]["pipeline_health"])

    def test_review_mode_emails_are_unchanged(self):
        from scout import monitor, config
        res = self._res("a", alerts=[{"headline": "price"}], decisions=[{"subject_key": "x | y", "operation": "revise", "judge_verdict": "confirm", "new_text": "n"}])
        with mock.patch("scout.display.list_battlecards", return_value=["a"]), \
             mock.patch.object(monitor, "check", side_effect=lambda slug, write=False: res), \
             mock.patch.object(monitor.store, "load_meta", return_value={"monitored": True}), \
             mock.patch.object(monitor, "_persist_run_cost"), mock.patch("scout.conseq.maybe_notify_ready"), \
             mock.patch.object(config, "PROPAGATE_MODE", "review"), mock.patch.object(config, "CONSEQUENTIAL_FILTER", "off"), \
             mock.patch("scout.notify.send_digest") as digest, mock.patch("scout.notify.send_propagation_proposals") as props, \
             mock.patch("scout.notify.send_run_fyi") as fyi, mock.patch("scout.notify.send_run_issues") as iss:
            monitor._run_all_impl(write=False, send=True, email_dry_run=True, force=True)
        digest.assert_called_once(); props.assert_called_once(); fyi.assert_not_called(); iss.assert_not_called()


class LiveApplyGate(unittest.TestCase):
    def test_provenance_failure_writes_nothing_and_is_reported(self):
        from scout import monitor
        src = __import__("inspect").getsource(monitor)
        i = src.find('config.PROPAGATE_MODE == "live" and prop["confirmed"]')
        block = src[i:i + 2600]
        self.assertIn("refresh_anchor_facts(", block); self.assertIn("provenance_issues(", block)
        self.assertIn("new_claims = pre_claims", block)                      # gate failed: card keeps its state
        self.assertIn('result["propagation"]["provenance_issues"] = issues', block)
