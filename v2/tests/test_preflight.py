"""The unattended lanes refuse to run on a wrong precondition instead of exiting green
(2026-09-30: a production run wrote under rc/; the replay scored a backlog)."""
import unittest

from scout import monitor


class Preflight(unittest.TestCase):
    def test_main_with_prefix_refuses(self):
        self.assertIn("prefix 'rc' on main", monitor.preflight({"GITHUB_REF_NAME": "main", "SCOUT_SELFSERVE_DATA_PREFIX": "rc"}))

    def test_main_without_prefix_runs(self):
        self.assertIsNone(monitor.preflight({"GITHUB_REF_NAME": "main", "SCOUT_SELFSERVE_DATA_PREFIX": ""}))
        self.assertIsNone(monitor.preflight({"GITHUB_REF_NAME": "main"}))

    def test_rc_needs_rc_prefix(self):
        self.assertIsNone(monitor.preflight({"GITHUB_REF_NAME": "rc", "SCOUT_SELFSERVE_DATA_PREFIX": "rc"}))
        self.assertIn("on rc", monitor.preflight({"GITHUB_REF_NAME": "rc", "SCOUT_SELFSERVE_DATA_PREFIX": ""}))

    def test_outside_actions_has_no_opinion(self):
        self.assertIsNone(monitor.preflight({"SCOUT_SELFSERVE_DATA_PREFIX": "rc"}))

    def test_rehearsal_is_legal_on_any_ref_with_its_prefix_and_store_root(self):
        """2026-10-03: the real write path on a retired card, isolated by prefix AND store root."""
        ok = {"SCOUT_REHEARSAL": "1", "SCOUT_SELFSERVE_DATA_PREFIX": "rehearsal", "SCOUT_STORE_ROOT": "/tmp/x/battlecards"}
        for ref in ("main", "rc", ""):
            self.assertIsNone(monitor.preflight({**ok, "GITHUB_REF_NAME": ref}), ref)

    def test_rehearsal_refuses_without_prefix_or_store_root(self):
        self.assertIn("rehearsals write under rehearsal/", monitor.preflight(
            {"GITHUB_REF_NAME": "main", "SCOUT_REHEARSAL": "1", "SCOUT_SELFSERVE_DATA_PREFIX": "", "SCOUT_STORE_ROOT": "/tmp/x"}))
        self.assertIn("rehearsals write under rehearsal/", monitor.preflight(
            {"GITHUB_REF_NAME": "rc", "SCOUT_REHEARSAL": "1", "SCOUT_SELFSERVE_DATA_PREFIX": "rc", "SCOUT_STORE_ROOT": "/tmp/x"}))
        self.assertIn("without SCOUT_STORE_ROOT", monitor.preflight(
            {"GITHUB_REF_NAME": "main", "SCOUT_REHEARSAL": "1", "SCOUT_SELFSERVE_DATA_PREFIX": "rehearsal"}))

    def test_rehearsal_prefix_without_the_flag_refuses(self):
        self.assertIn("without SCOUT_REHEARSAL", monitor.preflight({"GITHUB_REF_NAME": "main", "SCOUT_SELFSERVE_DATA_PREFIX": "rehearsal"}))
        # and the production rules are untouched
        self.assertIn("prefix 'rc' on main", monitor.preflight({"GITHUB_REF_NAME": "main", "SCOUT_SELFSERVE_DATA_PREFIX": "rc", "SCOUT_REHEARSAL": "0"}))


if __name__ == "__main__":
    unittest.main()
