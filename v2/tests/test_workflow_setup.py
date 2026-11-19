"""The morning workflow's optional setup can never stop the run (2026-11-19: Ubuntu's package
servers were down at 4 AM, the headless browser install failed and the monitor was skipped)."""
import os
import re
import unittest

_WF = os.path.join(os.path.dirname(__file__), "..", "..", ".github", "workflows", "monitor.yml")


class OptionalBrowserInstall(unittest.TestCase):
    def test_browser_install_cannot_fail_the_run(self):
        text = open(_WF, encoding="utf-8").read()
        steps = re.split(r"\n      - ", text)
        installs = [s for s in steps if "playwright install" in s]
        self.assertEqual(len(installs), 1)
        step = installs[0]
        self.assertIn("continue-on-error: true", step)
        self.assertIn("timeout-minutes:", step)

    def test_the_monitor_step_does_not_depend_on_the_install(self):
        text = open(_WF, encoding="utf-8").read()
        steps = re.split(r"\n      - ", text)
        live = [s for s in steps if s.startswith("name: Run monitoring checks (live)")]
        self.assertEqual(len(live), 1)
        self.assertNotIn("steps.", live[0].split("run:")[0].split("env:")[0])


if __name__ == "__main__":
    unittest.main()
