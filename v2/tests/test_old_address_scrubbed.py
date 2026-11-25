"""The old viewer address is written nowhere in this repo (2026-11-24: the GitHub About link and
nine files still carried it, and a visitor kept arriving through it). The forwarding page still
works; its address lives in the SCOUT_LEGACY_URL repo variable and the canary's own env.

    python -m unittest discover -s tests
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OLD = re.compile(r"streamlit\s*\.\s*app", re.I)
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
             ".claude"}     # local, untracked tool settings: not part of the repo
TEXT = (".py", ".md", ".yml", ".yaml", ".html", ".txt", ".toml", ".json", ".mjs", ".js", ".css",
        ".example", ".cfg", ".sh")


class OldAddressScrubbed(unittest.TestCase):
    def test_no_file_names_the_old_address(self):
        hits = []
        for base, dirs, files in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for name in files:
                if not name.endswith(TEXT):
                    continue
                path = os.path.join(base, name)
                try:
                    with open(path, encoding="utf-8", errors="ignore") as fh:
                        for n, line in enumerate(fh, 1):
                            if OLD.search(line):
                                hits.append(f"{os.path.relpath(path, ROOT)}:{n}")
                except OSError:
                    continue
        self.assertEqual(hits, [], "the old address is written in: " + ", ".join(hits[:10]))

    def test_the_forwarding_page_is_probed_only_when_its_address_is_configured(self):
        import importlib
        import sys
        import types
        from unittest import mock
        fake = types.ModuleType("playwright.sync_api")
        fake.sync_playwright = lambda: None
        mods = {"playwright": types.ModuleType("playwright"), "playwright.sync_api": fake}
        sys.path.insert(0, os.path.join(ROOT, "v2", "scripts"))
        try:
            with mock.patch.dict(sys.modules, mods):
                with mock.patch.dict(os.environ, {}, clear=False):
                    os.environ.pop("SCOUT_LEGACY_URL", None)
                    sys.modules.pop("probe_hosts", None)
                    bare = importlib.import_module("probe_hosts")
                    self.assertEqual(list(bare.HOSTS), ["agent-scout.ai"])
                with mock.patch.dict(os.environ, {"SCOUT_LEGACY_URL": "https://old.example/"}):
                    sys.modules.pop("probe_hosts", None)
                    both = importlib.import_module("probe_hosts")
                    self.assertEqual(both.HOSTS["old address"], "https://old.example/")
                    self.assertIn("old address", both.HOST_MARKERS)
        finally:
            sys.modules.pop("probe_hosts", None)
            sys.path.remove(os.path.join(ROOT, "v2", "scripts"))


if __name__ == "__main__":
    unittest.main()
