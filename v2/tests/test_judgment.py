"""The private judgment pack: byte-identical blocks, fail-closed loading, nothing proprietary in
the public tree (2026-10-02)."""
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

from scout import judgment

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
_MIRROR_PACK = os.path.join(judgment.MIRROR, "judgment", "pack.json")


def _tool():
    spec = importlib.util.spec_from_file_location("judgment_pack", os.path.join(_SCRIPTS, "judgment_pack.py"))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


class _Isolated(unittest.TestCase):
    """Each test gets a private loader state and restores the real one."""

    def setUp(self):
        self._pack, self._asked, self._stale = judgment._pack, list(judgment._asked), judgment._stale

    def tearDown(self):
        judgment._pack = self._pack
        judgment._asked[:] = self._asked
        judgment._stale = self._stale


@unittest.skipUnless(os.path.isfile(_MIRROR_PACK), "the private pack is not on this machine")
class Golden(unittest.TestCase):
    def test_every_block_is_byte_identical_to_its_frozen_hash(self):
        cwd = os.getcwd()
        os.chdir(os.path.dirname(_SCRIPTS))
        try:
            self.assertEqual(_tool().verify(), 0)
        finally:
            os.chdir(cwd)

    def test_no_placeholder_in_any_loaded_block(self):
        tool = _tool()
        for mod_name, targets in tool.REGISTRY.items():
            mod = __import__(f"scout.{mod_name}", fromlist=["x"])
            for t in targets:
                self.assertNotIn(judgment.SENTINEL, tool._live(mod, t), f"{mod_name}.{t}")


class PublicTree(unittest.TestCase):
    def test_modules_name_their_blocks_and_carry_no_text(self):
        tool = _tool()
        root = os.path.dirname(_SCRIPTS)
        for mod_name, targets in tool.REGISTRY.items():
            src = open(os.path.join(root, "scout", f"{mod_name}.py")).read()
            for t in targets:
                self.assertIn(f'judgment.get("{mod_name}.{t}"', src)
        for key, (mod_name, _func) in tool.FUNC_BLOCKS.items():
            self.assertIn(f'judgment.text("{key}"', open(os.path.join(root, "scout", f"{mod_name}.py")).read())
        self.assertFalse(os.path.exists(os.path.join(root, "methodology.md")))
        self.assertFalse(os.path.exists(os.path.join(root, "..", "v1", "methodology.md")))

    def test_stub_fixture_names_every_block_and_holds_no_real_text(self):
        tool = _tool()
        stub = json.load(open(os.path.join(os.path.dirname(_SCRIPTS), "tests", "fixtures", "judgment_stub.json")))
        names = [f"{m}.{t}" for m, ts in tool.REGISTRY.items() for t in ts] + list(tool.FILE_BLOCKS) + list(tool.FUNC_BLOCKS)
        self.assertEqual(sorted(stub["blocks"]), sorted(names))
        self.assertTrue(all(len(v) < 120 for v in stub["blocks"].values()))


class FailClosed(_Isolated):
    def test_unavailable_pack_yields_placeholders_and_blocks_model_calls(self):
        judgment._pack = None; judgment._asked[:] = []
        with mock.patch.object(judgment, "_read_sources", return_value=None):
            v = judgment.get("propagate._JUDGE_SYSTEM")
            self.assertIn(judgment.SENTINEL, v)
            with self.assertRaises(judgment.JudgmentUnavailable):
                judgment.require()
        with self.assertRaises(judgment.JudgmentUnavailable):
            judgment.assert_clean("system text", f"a prompt with {judgment.SENTINEL} x.Y inside")
        judgment.assert_clean("clean system", "clean prompt", None)

    def test_late_load_refills_module_constants_including_templates(self):
        mod = types.ModuleType("scout._jt"); mod.WHO = "world"
        sys.modules["scout._jt"] = mod
        try:
            judgment._pack = None; judgment._asked[:] = []
            with mock.patch.object(judgment, "_read_sources", return_value=None):
                mod.GREETING = judgment.get("_jt.GREETING", {"WHO": mod.WHO})
            self.assertIn(judgment.SENTINEL, mod.GREETING)
            pack = {"version": "t", "blocks": {"_jt.GREETING": "hello ⟦WHO⟧"}}
            with mock.patch.object(judgment, "_read_sources", return_value=pack):
                judgment.require()
            self.assertEqual(mod.GREETING, "hello world")
        finally:
            sys.modules.pop("scout._jt", None)

    def test_missing_block_is_named(self):
        judgment._asked[:] = []
        judgment._pack = {"version": "t", "blocks": {}}
        judgment.get("nowhere.NOTHING")
        judgment._stale = False                      # nothing to refill: the block does not exist
        with self.assertRaises(judgment.JudgmentUnavailable) as cm:
            judgment.require()
        self.assertIn("nowhere.NOTHING", str(cm.exception))


class Sources(_Isolated):
    def test_explicit_path_then_rc_mirror_then_production_mirror(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "judgment")); os.makedirs(os.path.join(d, "rc", "judgment"))
            json.dump({"version": "prod", "blocks": {}}, open(os.path.join(d, "judgment", "pack.json"), "w"))
            json.dump({"version": "rc", "blocks": {}}, open(os.path.join(d, "rc", "judgment", "pack.json"), "w"))
            explicit = os.path.join(d, "x.json"); json.dump({"version": "explicit", "blocks": {}}, open(explicit, "w"))
            with mock.patch.object(judgment, "MIRROR", d):
                with mock.patch.dict(os.environ, {"SCOUT_JUDGMENT_PACK": explicit}):
                    self.assertEqual(judgment._read_sources()["version"], "explicit")
                env = {k: v for k, v in os.environ.items() if k not in ("SCOUT_JUDGMENT_PACK", "SCOUT_SELFSERVE_DATA_PREFIX")}
                with mock.patch.dict(os.environ, {**env, "SCOUT_SELFSERVE_DATA_PREFIX": "rc"}, clear=True):
                    self.assertEqual(judgment._read_sources()["version"], "rc")
                with mock.patch.dict(os.environ, env, clear=True):
                    self.assertEqual(judgment._read_sources()["version"], "prod")

    def test_baseline_pack_keeps_the_eval_period(self):
        self.assertIsNone(judgment.period_tag(None)); self.assertIsNone(judgment.period_tag(judgment.BASELINE_VERSION))
        self.assertEqual(judgment.period_tag("abc"), "abc")


if __name__ == "__main__":
    unittest.main()
