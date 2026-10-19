"""The store's opt-in bulk-read mirror (scout/selfserve.py, 2026-10-19): reads under the result and
capture directories come from a local clone of the private store when SCOUT_STORE_MIRROR is set;
label files and everything else stay on the backend. Pure, no network."""
import os
import tempfile
import unittest
from unittest import mock

from scout import config, selfserve


def _w(root, rel, text):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(text)


class Mirror(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="scout-mirror-")
        _w(self.root, "shadow_models/ollama/2026-10/c_1.json", '{"a":1}')
        _w(self.root, "shadow_models/labels.jsonl", "MIRROR COPY\n")
        os.makedirs(os.path.join(self.root, "shadow_models/apple_ondevice"))
        env = mock.patch.dict(os.environ, {"SCOUT_STORE_MIRROR": self.root}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        # the GitHub backend, faked: anything that reaches it answers LIVE
        gh = mock.patch.object(selfserve, "_gh_get", return_value=("LIVE", "sha"))
        ug = mock.patch.object(selfserve, "use_github", return_value=True)
        gl = mock.patch.object(selfserve, "_gh_list", return_value=["LIVE.json"])
        for p in (gh, ug, gl):
            p.start(); self.addCleanup(p.stop)

    def test_bulk_reads_and_listings_come_from_disk(self):
        self.assertEqual(selfserve.read_data("shadow_models/ollama/2026-10/c_1.json"), '{"a":1}')
        self.assertIsNone(selfserve.read_data("shadow_models/ollama/2026-10/c_2.json"))     # absent: None, not the API
        self.assertEqual(selfserve.list_data("shadow_models/ollama/2026-10"), ["c_1.json"])
        self.assertEqual(selfserve.list_data("shadow_models", include_dirs=True), ["apple_ondevice", "labels.jsonl", "ollama"])
        self.assertEqual(selfserve.list_data("shadow_models"), ["labels.jsonl"])          # files only by default, like the API
        self.assertEqual(selfserve.list_data("shadow_models/ollama_nemotron/2026-10"), [])

    def test_label_files_and_everything_else_stay_on_the_backend(self):
        self.assertEqual(selfserve.read_data("shadow_models/labels.jsonl"), "LIVE")    # a stale copy must never be appended to
        self.assertEqual(selfserve.read_data("research/arbiter_state.json"), "LIVE")
        self.assertEqual(selfserve.list_data("research/arbiter/2026-10"), ["LIVE.json"])
        self.assertEqual(selfserve.read_data("shadow_models/ollama/2026-10/c_1.json"), '{"a":1}')

    def test_unset_or_missing_mirror_is_ignored(self):
        with mock.patch.dict(os.environ, {"SCOUT_STORE_MIRROR": "/nonexistent/scout-mirror"}):
            self.assertIsNone(selfserve.mirror_root())
            self.assertEqual(selfserve.read_data("shadow_models/ollama/2026-10/c_1.json"), "LIVE")
        with mock.patch.dict(os.environ, {"SCOUT_STORE_MIRROR": ""}):
            self.assertEqual(selfserve.list_data("shadow_models/ollama/2026-10"), ["LIVE.json"])

    def test_dirs_override_and_the_rc_prefix(self):
        with mock.patch.dict(os.environ, {"SCOUT_STORE_MIRROR_DIRS": "calls/, research/arbiter"}):
            self.assertEqual(selfserve.read_data("shadow_models/ollama/2026-10/c_1.json"), "LIVE")   # no longer served
            _w(self.root, "research/arbiter/2026-10/m_1.json", "R")
            self.assertEqual(selfserve.read_data("research/arbiter/2026-10/m_1.json"), "R")
        _w(self.root, "rc/calls/2026-10/b.json", "RC")
        with mock.patch.object(config, "SELFSERVE_DATA_PREFIX", "rc"), \
             mock.patch.object(config, "SELFSERVE_DATA_READ_FALLBACK", True):
            self.assertEqual(selfserve.read_data("calls/2026-10/b.json"), "RC")                      # the prefixed copy
            self.assertEqual(selfserve.read_data("shadow_models/ollama/2026-10/c_1.json"), '{"a":1}')  # read-through to production's
        with mock.patch.object(config, "SELFSERVE_DATA_PREFIX", "rc"), \
             mock.patch.object(config, "SELFSERVE_DATA_READ_FALLBACK", False):
            self.assertIsNone(selfserve.read_data("shadow_models/ollama/2026-10/c_1.json"))           # no read-through: absent

    def test_refresh_never_raises(self):
        r = selfserve.refresh_mirror(timeout=30)          # a temp dir is not a git clone
        self.assertFalse(r["ok"]); self.assertTrue(r["error"])
        self.assertTrue(selfserve.mirror_note().startswith("[store] mirror pull failed"))
        with mock.patch.dict(os.environ, {"SCOUT_STORE_MIRROR": ""}):
            self.assertIn("no mirror configured", selfserve.mirror_note())


if __name__ == "__main__":
    unittest.main()
