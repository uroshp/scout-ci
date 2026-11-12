"""The hosted free arm through OpenRouter (2026-11-11). His rules: free tier only, Scout's prompts
reach the pinned provider and nobody else, the arm never raises the arbiter's bill or his label
queue, and what it claims (who served the call, reasoning on) is read from the stored answer.
No network: httpx.post is faked. Run from v2/:
    python -m unittest discover -s tests
"""
import json
import os
import tempfile
import unittest
from unittest import mock

from scout import adjudicate_models, modelcompare, replaybackends as rb

ARM = "openrouter_gemma4"
REC = {"call_id": "c1", "role": "judge", "system": {"kind": "string", "text": "s"}, "user": "u", "status": "ok", "result": {"text": "{}"}}


class _Resp:
    def __init__(self, code=200, data=None):
        self.status_code, self._d, self.text = code, data or {}, json.dumps(data or {})

    def json(self):
        return self._d


def _ok(provider="Google AI Studio", content='{"verdicts": []}', reasoning="because"):
    return _Resp(200, {"provider": provider, "model": "google/gemma-4-26b-a4b-it:free",
                       "choices": [{"finish_reason": "stop", "message": {"content": content, "reasoning": reasoning}}],
                       "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0}})


class Arm(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.p = [mock.patch.object(rb, "OPENROUTER_USAGE_PATH", os.path.join(self.tmp, "u.json")),
                  mock.patch.object(rb, "OPENROUTER_MIN_GAP_S", 0.0),
                  mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "k"}),
                  mock.patch("time.sleep")]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()

    def _run(self, resp):
        with mock.patch.object(rb.httpx, "post", return_value=resp) as post:
            return rb.drive_replay(REC, ARM), post

    def test_request_pins_the_model_and_the_provider_with_no_fallback_and_no_paid_model(self):
        out, post = self._run(_ok())
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["model"], "google/gemma-4-26b-a4b-it:free")
        self.assertTrue(body["model"].endswith(":free"))
        self.assertEqual(body["provider"], {"only": ["google-ai-studio"], "allow_fallbacks": False})
        self.assertNotIn("models", body)                       # no fallback list that could reach a paid model
        self.assertEqual(body["reasoning"], {"enabled": True})
        self.assertIn("openrouter.ai/api/v1/chat/completions", post.call_args.args[0])
        self.assertEqual(out["status"], "ok"); self.assertEqual(out["thinking"], "because")
        self.assertEqual(out["backend_version"]["served_by"], "Google AI Studio")
        self.assertEqual(out["cost_usd"], 0.0)

    def test_the_random_free_router_is_never_an_arm(self):
        for cfg in rb.OPENROUTER_MODELS.values():
            self.assertNotEqual(cfg["model"], "openrouter/free")
            self.assertTrue(cfg["model"].endswith(":free")); self.assertTrue(cfg["providers"])

    def test_an_answer_from_another_provider_is_kept_as_an_error_never_scored(self):
        out, _ = self._run(_ok(provider="Somewhere Else"))
        self.assertEqual((out["status"], out["reason"]), ("error", "wrong_provider"))
        self.assertFalse(out.get("stop_arm"))

    def test_every_attempt_counts_and_the_cap_stops_the_arm_before_a_request(self):
        self._run(_ok()); self._run(_Resp(500, {"error": {"message": "boom", "code": 500}}))
        self.assertEqual(rb.openrouter_used_today(), 2)
        with mock.patch.object(rb, "OPENROUTER_DAILY_CAP", 2):
            out, post = self._run(_ok())
        self.assertTrue(out["stop_arm"]); self.assertEqual(out["reason"], "daily_cap"); post.assert_not_called()

    def test_no_key_and_a_rate_limit_stop_the_arm_without_a_result(self):
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}):
            out, post = self._run(_ok())
        self.assertTrue(out["stop_arm"]); post.assert_not_called()
        out, post = self._run(_Resp(429, {"error": {"message": "Rate limit exceeded: free-models-per-day", "code": 429}}))
        self.assertTrue(out["stop_arm"]); self.assertEqual(out["reason"], "rate_limited")
        self.assertEqual(post.call_count, 2)                   # one retry after the minute, then stop

    def test_period_follows_the_model_provider_and_sampling(self):
        out, _ = self._run(_ok())
        row = {"backend": ARM, "backend_version": out["backend_version"]}
        self.assertTrue(rb.is_current_period(row))
        row["backend_version"] = {**out["backend_version"], "providers": ["other"]}
        self.assertFalse(rb.is_current_period(row))

    def test_its_disagreements_never_reach_the_arbiter_or_the_label_queue(self):
        item = {"delta_id": "d1", "item_id": "0", "status": "disagree", "reference": "confirm", "candidate": "reject"}
        rows = [{"backend": b, "call_id": "c1", "role": "judge", "status": "ok", "comparison": {"items": [dict(item, delta_id=f"d-{b}")]}}
                for b in (ARM, "ollama_gemma4")]
        with mock.patch.object(adjudicate_models, "_load", return_value=rows), \
             mock.patch.object(modelcompare, "load_labels", return_value={}):
            pend = adjudicate_models.pending()
        self.assertEqual([p["delta_id"] for p in pend], ["d-ollama_gemma4"])

    def test_the_arm_is_a_backend_but_not_a_local_one(self):
        self.assertIn(ARM, rb.BACKENDS); self.assertNotIn(ARM, rb.LOCAL_BACKENDS); self.assertNotIn(".", ARM)


if __name__ == "__main__":
    unittest.main()
