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


if __name__ == "__main__":
    unittest.main()
