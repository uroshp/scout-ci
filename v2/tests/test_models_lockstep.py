"""Models and the Agent SDK move in lockstep (Uroš, 2026-10-03: "latest models and latest SDK,
updated in lockstep"). The first rehearsal caught every Opus call failing with "Claude Code 2.1.179
does not support this model; version 2.1.280 or newer is required": the model id had moved, the
SDK pin (which bundles the binary) had not. Three checks, no network, no spend."""
import os
import re
import unittest

from scout import config, sdkcheck

V2 = config.APP_ROOT


def _pin(fname):
    txt = open(os.path.join(V2, fname)).read()
    m = re.search(r"^claude-agent-sdk==([0-9.]+)", txt, re.M)
    return m.group(1) if m else None


class Lockstep(unittest.TestCase):
    def test_both_requirement_files_pin_the_same_sdk(self):
        a, b = _pin("requirements.txt"), _pin("requirements-engine.txt")
        self.assertTrue(a and b, (a, b))
        self.assertEqual(a, b, "the monitor's and the engine's SDK pins must move together")

    def test_installed_sdk_is_the_pinned_one(self):
        """The environment running the tests (the mini's venv, a runner) is the pinned SDK, so a
        stale `pip install` cannot hide behind a green suite."""
        try:
            from importlib.metadata import version
            installed = version("claude-agent-sdk")
        except Exception:
            self.skipTest("claude-agent-sdk not installed here")
        self.assertEqual(installed, _pin("requirements.txt"))

    def test_bundled_binary_supports_the_configured_models(self):
        v = sdkcheck.cli_version()
        if not v:
            self.skipTest("no bundled Claude Code binary in this environment")
        self.assertIsNone(sdkcheck.too_old(v), f"bundled Claude Code {v} < MODEL_MIN_CLI {config.MODEL_MIN_CLI}")

    def test_too_old_reason_and_preflight_hook(self):
        from scout import monitor
        self.assertIn("2.1.280 or newer", sdkcheck.too_old("2.1.179", "2.1.280"))
        self.assertIsNone(sdkcheck.too_old("2.1.286", "2.1.280"))
        self.assertIsNone(sdkcheck.too_old("3.0.0", "2.1.280"))
        self.assertIsNone(sdkcheck.too_old(None, "2.1.280"))            # no binary: the SDK reports that itself
        # preflight refuses on a runner with an old binary, and never shells out in a plain unit test
        from unittest import mock
        with mock.patch.object(sdkcheck, "cli_version", return_value="2.1.179"):
            self.assertIn("does not support the configured models", monitor.preflight({"GITHUB_REF_NAME": "main"}))
            self.assertIsNone(monitor.preflight({}))
        with mock.patch.object(sdkcheck, "cli_version", return_value="2.1.286"):
            self.assertIsNone(monitor.preflight({"GITHUB_REF_NAME": "main"}))
            self.assertIsNone(monitor.preflight({"GITHUB_REF_NAME": "main", "SCOUT_REHEARSAL": "1",
                                                 "SCOUT_SELFSERVE_DATA_PREFIX": "rehearsal", "SCOUT_STORE_ROOT": "/tmp/x"}))


if __name__ == "__main__":
    unittest.main()
