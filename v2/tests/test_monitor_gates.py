"""Detection-gate invariants born from the 2026-07-01 Fable-lift miss: the deterministic
escalation floor (a tracked-subject candidate escalates regardless of the cheap triage grade),
my_company about-tag normalization (company-name tags must not slip through the competitor arm),
and window-hold semantics (an unrelated alert or one empty re-scan must never erase a held
window; abandonment is bounded and loud). Run from v2/:  python -m unittest discover -s tests
"""
import unittest

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
