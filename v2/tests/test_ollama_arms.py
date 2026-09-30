"""Three Ollama arms, one resident at a time, each its own name/results/column; the labelling
surface stays blind; a silent arm is a canary finding (2026-09-30)."""
import importlib.util
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from unittest import mock

from scout import modelcompare as mc, replaybackends as rb, localagent

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")


def _script(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_SCRIPTS, f"{name}.py"))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


class _Resp:
    def __init__(self, status, body): self.status_code, self._b, self.text = status, body, json.dumps(body)
    def json(self): return self._b


def _rec(role="gate_judge"):
    return {"call_id": "c_arm", "role": role, "run_ts": "2026-09-30T04:00:00", "model": "claude-opus-5",
            "system": {"kind": "string", "text": "sys"}, "user": "usr", "sizes": {"est_tokens_in": 10}, "truncated": [],
            "result": {"text": '{"verdict":"confirm","reason":"r"}', "cost_usd": 0.1}, "options": {}}


class Registry(unittest.TestCase):
    def test_three_arms_registered_and_unknown_still_raises(self):
        for name in ("ollama", "ollama_nemotron", "ollama_gemma4"):
            self.assertIn(name, rb.BACKENDS); self.assertIn(name, rb.LOCAL_BACKENDS)
            self.assertTrue(rb.is_ollama(name)); self.assertIn("tag", rb.ollama_cfg(name))
            self.assertIn(name, localagent.TOOL_PROTOCOL)
        self.assertFalse(rb.is_ollama("apple_ondevice"))
        for bad in ("ollama.gemma", "ollama_lightning", "mistral_hosted"):
            with self.assertRaises(rb.UnknownBackend):
                rb.drive_replay(_rec(), bad)
        self.assertTrue(all("." not in n for n in rb.OLLAMA_MODELS))   # load_results skips dotted folders
        self.assertEqual(rb.OLLAMA_MODELS["ollama"]["tag"], rb.OLLAMA_TAG)   # Magistral keeps its name

    def test_each_arm_sends_its_own_tag_and_context(self):
        body = {"message": {"role": "assistant", "content": '{"verdict":"confirm","reason":"r"}'},
                "prompt_eval_count": 8, "eval_count": 3}
        for name in ("ollama_nemotron", "ollama_gemma4"):
            with mock.patch.object(rb.httpx, "post", return_value=_Resp(200, body)) as post, \
                 mock.patch.object(rb, "ollama_version", return_value={"tag": rb.ollama_cfg(name)["tag"]}):
                r = rb.drive_replay(_rec(), name)
            sent = post.call_args.kwargs["json"]
            self.assertEqual(sent["model"], rb.ollama_cfg(name)["tag"])
            self.assertEqual(sent["options"]["num_ctx"], rb.ollama_cfg(name)["num_ctx"])
            self.assertEqual(sent["options"]["num_predict"], rb.rolespecs.output_reserve("gate_judge") + rb.ollama_cfg(name)["think_reserve"])   # bounded generation
            self.assertEqual(r["status"], "ok"); self.assertEqual(r["backend_model"], rb.ollama_cfg(name)["tag"])
            self.assertIsNone(r["thinking"])       # a model with no `thinking` field still parses

    def test_fit_uses_the_arms_own_window(self):
        with mock.patch.dict(rb.OLLAMA_MODELS["ollama_nemotron"], {"num_ctx": 64}):
            ok, reason, _ = rb.fits(_rec(), "ollama_nemotron", "x" * 3000, "y" * 3000)
        self.assertEqual((ok, reason), (False, "context_exceeded"))
        ok, reason, _ = rb.fits(_rec(), "ollama_gemma4", "x" * 3000, "y" * 3000)
        self.assertTrue(ok)

    def test_resident_and_unload_target_the_arms_tag(self):
        with mock.patch.object(rb.httpx, "post") as post:
            rb.ollama_unload("ollama_gemma4")
        self.assertEqual(post.call_args.kwargs["json"]["model"], rb.ollama_cfg("ollama_gemma4")["tag"])


class Scorecard(unittest.TestCase):
    def _row(self, backend, call_id, role="gate_judge"):
        return {"backend": backend, "call_id": call_id, "role": role, "mode": "exact", "rep": 0, "status": "ok",
                "reason": None, "comparison": {"summary": {"agree": 1, "judged": 1}}, "duration_ms": 10,
                "reference": {"eligible": True}}

    def test_warming_arm_does_not_shrink_common(self):
        rows = [self._row("ollama", f"c{i}") for i in range(25)] + [self._row("apple_ondevice", f"c{i}") for i in range(25)]
        rows += [self._row("ollama_gemma4", "c1")]        # one result: warming up
        sc = mc.scorecard(rows, labels={})
        self.assertEqual(sc["common_n"], 25); self.assertEqual(sc["warming_up"], ["ollama_gemma4"])
        rows += [self._row("ollama_gemma4", f"c{i}") for i in range(2, 22)]   # crosses the threshold
        sc = mc.scorecard(rows, labels={})
        self.assertEqual(sc["warming_up"], []); self.assertEqual(sc["common_n"], 21)


class Blind(unittest.TestCase):
    def test_digest_never_names_an_arm(self):
        from scout import adjudicate_models as am
        pend = [{"delta_id": "m_1", "role": "gate_judge", "slug": "s", "item_id": "i",
                 "candidates": ["ollama_gemma4", "ollama_nemotron"]}]
        buf = io.StringIO()
        with mock.patch.object(am, "pending", return_value=pend), mock.patch.object(mc, "load_labels", return_value={}), \
             redirect_stdout(buf):
            am._print_digest()
        out = buf.getvalue()
        for name in rb.OLLAMA_MODELS:
            self.assertNotIn(name, out)
        self.assertIn("disagreed by 2 arms", out)


class Canary(unittest.TestCase):
    def test_silent_arm_is_a_finding(self):
        lc = _script("lanes_canary")
        log = "2026-10-01 05:15:00 PDT  === replay start\n2026-10-01 05:18:00 PDT  apple arm exit 0\n" \
              "2026-10-01 05:40:00 PDT  ollama arm exit 0\n2026-10-01 06:10:00 PDT  ollama_nemotron arm exit 0\n"
        probs = lc.arm_problems(log)
        self.assertEqual(len(probs), 1); self.assertIn("ollama_gemma4", probs[0])
        self.assertEqual(lc.arm_problems(log + "2026-10-01 06:50:00 PDT  ollama_gemma4 arm exit 0\n"), [])
        self.assertEqual(lc.arm_problems("2026-10-01 05:15:00 PDT  ollama arms skipped tonight\n"), [])


if __name__ == "__main__":
    unittest.main()
