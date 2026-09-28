"""scout/replaybackends.py: request-body contracts for both local arms, the fit pre-flight, the error
mapper, refusal, truncation skip, and refusal of unknown / off-device backends. httpx and
`fm count-tokens` are mocked; nothing is called."""
import unittest
from unittest import mock

from scout import replaybackends as rb


def rec(role="judge", system="S", user="U", truncated=()):
    return {"call_id": "c_1", "role": role, "model": "claude-opus-4-8", "fidelity": "exact",
            "system": {"kind": "string", "text": system}, "user": user, "transcript": [],
            "result": {"text": "{}"}, "truncated": list(truncated), "options": {"max_turns": 6, "max_budget_usd": 0.5}}


class _Resp:
    def __init__(self, status, body=None, text=""):
        self.status_code = status
        self._body = body
        self.text = text or (str(body) if body else "")

    def json(self):
        return self._body


class Backends(unittest.TestCase):
    def test_unknown_and_off_device_backends_are_rejected(self):
        for bad in ("apple_pcc", "mistral_hosted", "openai", ""):
            with self.assertRaises(rb.UnknownBackend):
                rb.drive_replay(rec(), bad)

    def test_truncated_exact_field_is_skipped_before_any_call(self):
        with mock.patch.object(rb.httpx, "post") as post, mock.patch.object(rb, "fm_count_tokens") as ct:
            r = rb.drive_replay(rec(truncated=["user"]), "apple_ondevice")
        self.assertEqual((r["status"], r["reason"]), ("skipped", "truncated"))
        post.assert_not_called(); ct.assert_not_called()

    def test_apple_fit_uses_exact_count_and_reserve(self):
        with mock.patch.object(rb, "fm_count_tokens", return_value=7971), mock.patch.object(rb.httpx, "post") as post:
            r = rb.drive_replay(rec("judge"), "apple_ondevice")      # 7971 + 1024 > 8192
        self.assertEqual((r["status"], r["reason"], r["observed_token_count"]), ("skipped", "context_exceeded", 7971))
        post.assert_not_called()

    def test_apple_request_contract_and_success(self):
        body = {"choices": [{"message": {"role": "assistant", "refusal": None, "content": '{"verdict":"cut","reason":"r"}'}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "completion_tokens_details": {"reasoning_tokens": 0}}}
        with mock.patch.object(rb, "fm_count_tokens", return_value=100), \
             mock.patch.object(rb, "apple_version", return_value={"macos_build": "x"}), \
             mock.patch.object(rb.httpx, "post", return_value=_Resp(200, body)) as post:
            r = rb.drive_replay(rec("gate_judge"), "apple_ondevice")
        sent = post.call_args.kwargs["json"]
        self.assertFalse(sent["stream"]); self.assertEqual(sent["temperature"], 0)
        self.assertEqual(sent["response_format"]["type"], "json_schema")
        self.assertEqual(sent["response_format"]["json_schema"]["schema"]["properties"]["verdict"]["enum"], ["confirm", "reject"])
        self.assertEqual(sent["messages"][0], {"role": "system", "content": "S"})
        self.assertEqual(r["status"], "ok"); self.assertEqual(r["tokens"]["input"], 100)
        self.assertEqual(r["reasoning"], "unsupported"); self.assertTrue(r["schema_enforced"])
        self.assertEqual(r["cost_usd"], 0.0)

    def test_apple_error_mapping(self):
        cases = {"The model's safety guardrails were triggered.": ("error", "guardrail"),
                 "The model refused to answer.": ("error", "refusal"),
                 "Prompt exceeded the model's context size": ("skipped", "context_exceeded"),
                 "rate limit reached": ("error", "rate_limited"),
                 "something else": ("error", "error")}
        for msg, want in cases.items():
            with mock.patch.object(rb, "fm_count_tokens", return_value=100), \
                 mock.patch.object(rb, "apple_version", return_value={}), \
                 mock.patch.object(rb.httpx, "post", return_value=_Resp(500, {"error": {"message": msg}})), \
                 mock.patch.object(rb, "_apple_cli", return_value={"status": "error", "reason": "refusal", "text": "x"}):
                r = rb.drive_replay(rec("gate_judge"), "apple_ondevice")
            self.assertEqual((r["status"], r["reason"]), want, msg)

    def test_apple_refusal_falls_back_to_cli_then_permissive(self):
        body = {"choices": [{"message": {"refusal": "I can't help with that", "content": ""}}], "usage": {"prompt_tokens": 5}}
        cli = [{"status": "error", "reason": "refusal", "text": "refused"},
               {"status": "ok", "reason": None, "text": '{"verdict":"confirm","reason":"r"}', "duration_ms": 900}]
        with mock.patch.object(rb, "fm_count_tokens", return_value=5), mock.patch.object(rb, "apple_version", return_value={}), \
             mock.patch.object(rb.httpx, "post", return_value=_Resp(200, body)), \
             mock.patch.object(rb, "_apple_cli", side_effect=cli) as c:
            r = rb.drive_replay(rec("gate_judge"), "apple_ondevice")
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["backend_version"]["via"], "fm_respond")
        self.assertEqual(r["backend_version"]["guardrails"], "permissive-content-transformations")
        self.assertEqual(c.call_count, 2)
        with mock.patch.object(rb, "fm_count_tokens", return_value=5), mock.patch.object(rb, "apple_version", return_value={}), \
             mock.patch.object(rb.httpx, "post", return_value=_Resp(200, body)), \
             mock.patch.object(rb, "_apple_cli", return_value={"status": "error", "reason": "refusal", "text": "refused"}):
            r2 = rb.drive_replay(rec("gate_judge"), "apple_ondevice")
        self.assertEqual((r2["status"], r2["reason"]), ("error", "refusal"))

    def test_ollama_contract_and_fit(self):
        body = {"message": {"role": "assistant", "content": '{"verdict":"confirm","reason":"r"}', "thinking": "hmm"},
                "prompt_eval_count": 80, "eval_count": 30}
        with mock.patch.object(rb, "ollama_version", return_value={"tag": rb.OLLAMA_TAG}), \
             mock.patch.object(rb.httpx, "post", return_value=_Resp(200, body)) as post:
            r = rb.drive_replay(rec("gate_judge"), "ollama")
        sent = post.call_args.kwargs["json"]
        self.assertEqual(sent["options"]["num_ctx"], rb.OLLAMA_NUM_CTX)
        self.assertEqual(sent["options"]["temperature"], 0); self.assertTrue(sent["think"])
        self.assertEqual(sent["format"]["properties"]["verdict"]["enum"], ["confirm", "reject"])
        self.assertEqual(r["thinking"], "hmm"); self.assertEqual(r["reasoning"], "thinking")
        big = rec("judge", user="w" * (rb.OLLAMA_NUM_CTX * 3))
        with mock.patch.object(rb, "ollama_version", return_value={}), mock.patch.object(rb.httpx, "post") as post:
            r2 = rb.drive_replay(big, "ollama")
        self.assertEqual((r2["status"], r2["reason"]), ("skipped", "context_exceeded")); post.assert_not_called()

    def test_anthropic_refuses_without_allow_spend(self):
        with mock.patch.object(rb.httpx, "post") as post:
            r = rb.drive_replay(rec(), "anthropic")
        self.assertEqual((r["status"], r["reason"]), ("skipped", "spend_blocked")); post.assert_not_called()

    def test_frozen_prompt_inlines_tool_activity(self):
        r = rec("materiality", system="S")
        r["system"] = {"kind": "preset", "preset": "claude_code", "append": "APPEND"}
        r["transcript"] = [{"kind": "tool_use", "name": "fetch_page", "input": {"url": "u"}, "tool_use_id": "t"},
                           {"kind": "tool_result", "tool_use_id": "t", "content": "PAGE TEXT"}]
        system, user = rb.frozen_prompt(r)
        self.assertTrue(system.startswith("APPEND")); self.assertIn("NO tools", system)
        self.assertIn("PAGE TEXT", user); self.assertIn("fetch_page", user)


if __name__ == "__main__":
    unittest.main()
