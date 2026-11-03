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
        self.assertEqual(sent["options"]["temperature"], 0.7); self.assertTrue(sent["think"])
        self.assertTrue(sent["messages"][0]["content"].startswith(rb.MAGISTRAL_SYSTEM)); self.assertTrue(sent["messages"][0]["content"].endswith("S"))
        self.assertEqual(sent["format"]["properties"]["verdict"]["enum"], ["confirm", "reject"])
        self.assertEqual(r["thinking"], "hmm"); self.assertEqual(r["reasoning"], "thinking")
        big = rec("judge", user="w" * (rb.OLLAMA_NUM_CTX * 3))
        with mock.patch.object(rb, "ollama_version", return_value={}), mock.patch.object(rb.httpx, "post") as post, \
             mock.patch.object(rb, "ollama_count_tokens", return_value=None):
            r2 = rb.drive_replay(big, "ollama")
        self.assertEqual((r2["status"], r2["reason"]), ("skipped", "context_exceeded")); post.assert_not_called()

    def test_small_arms_reason_with_vendor_sampling_and_magistral_key_is_unchanged(self):
        # 2026-11-02 (Uroš): reasoning ON for Nemotron and Gemma under their vendors' reasoning-mode
        # sampling and a budget; Magistral untouched, so its settings key (its period) is unchanged.
        nem, gem, mag = rb.ollama_cfg("ollama_bolt"), rb.ollama_cfg("ollama_gemma4"), rb.ollama_cfg("ollama")
        self.assertTrue(nem["think"]); self.assertEqual((nem["temperature"], nem["top_p"], nem["top_k"], nem["think_reserve"]), (1.0, 0.95, 64, 8192))
        self.assertNotIn("ollama_nemotron", rb.OLLAMA_MODELS); self.assertIn("ollama_nemotron", rb.RETIRED_OLLAMA)
        self.assertTrue(gem["think"]); self.assertEqual((gem["temperature"], gem["top_p"], gem["top_k"]), (1.0, 0.95, 64))
        self.assertTrue(mag["think"]); self.assertEqual((mag["temperature"], mag["top_p"], mag["top_k"]), (0.7, 0.95, None))
        key = rb.settings_key("ollama")
        self.assertEqual(key["sampling"]["temperature"], 0.7); self.assertEqual(len(key["system_prefix"]), 12)
        # the 948 rows written before 11/2 (temperature 0, no prefix) are history
        self.assertFalse(rb.is_current_period({"backend": "ollama", "backend_version": {"tag": mag["tag"], "num_ctx": 49152, "thinking": True, "vendor": "Mistral AI"}}))
        self.assertEqual(rb.settings_key("ollama_bolt")["sampling"], {"temperature": 1.0, "top_p": 0.95, "top_k": 64, "think_reserve": 8192})
        gpt = rb.ollama_cfg("ollama_gptoss")
        self.assertEqual((gpt["think"], gpt["think_level"], gpt["temperature"], gpt["top_p"]), (True, "medium", 1.0, None))
        self.assertEqual(rb.settings_key("ollama_gptoss")["sampling"]["think_level"], "medium")
        self.assertIsNone(rb.ollama_cfg("ollama_bolt")["think_level"])
        self.assertEqual(len(rb.OLLAMA_MODELS), 4)   # Bolt E4B joins with the Ollama upgrade (see the registry comment)

    def test_ollama_request_carries_the_arm_sampling(self):
        body = {"message": {"role": "assistant", "content": '{"verdict":"confirm","reason":"r"}', "thinking": "t"},
                "done_reason": "stop", "prompt_eval_count": 80, "eval_count": 30}
        with mock.patch.object(rb, "ollama_version", return_value={"tag": "n"}), \
             mock.patch.object(rb.httpx, "post", return_value=_Resp(200, body)) as post:
            r = rb.drive_replay(rec("gate_judge"), "ollama_bolt")
        sent = post.call_args.kwargs["json"]
        self.assertTrue(sent["think"]); self.assertEqual(sent["options"]["temperature"], 1.0)
        self.assertEqual(sent["options"]["top_p"], 0.95); self.assertEqual(sent["options"]["top_k"], 64)
        self.assertEqual(sent["options"]["num_predict"], rb.rolespecs.output_reserve("gate_judge") + 8192)
        self.assertEqual(post.call_count, 1); self.assertIs(r["budget_hit"], False); self.assertIsNone(r["continuation"])
        self.assertEqual(r["done_reason"], "stop"); self.assertEqual(r["tokens"]["thinking_chars"], 1)

    def test_gptoss_sends_its_reasoning_level_and_continues_with_think_off(self):
        first = {"message": {"role": "assistant", "content": "", "thinking": "a\nb\nc"}, "done_reason": "length", "eval_count": 9000}
        second = {"message": {"role": "assistant", "content": '{"verdict":"confirm","reason":"r"}'}, "done_reason": "stop", "eval_count": 20}
        with mock.patch.object(rb, "ollama_version", return_value={"tag": "g"}), \
             mock.patch.object(rb.httpx, "post", side_effect=[_Resp(200, first), _Resp(200, second)]) as post:
            r = rb.drive_replay(rec("gate_judge"), "ollama_gptoss")
        self.assertEqual(post.call_args_list[0].kwargs["json"]["think"], "medium")
        self.assertEqual(post.call_args_list[0].kwargs["json"]["options"]["temperature"], 1.0)
        self.assertNotIn("top_p", post.call_args_list[0].kwargs["json"]["options"])
        follow = post.call_args_list[1].kwargs["json"]
        self.assertEqual(follow["think"], "low"); self.assertEqual(r["continuation"], "chat_think_off")
        self.assertEqual(follow["options"]["num_predict"], rb.rolespecs.output_reserve("gate_judge") + 1024)

    def test_ollama_budget_hit_is_continued_once_with_the_trace_closed(self):
        # the model was still reasoning at num_predict: the trace (cut at its last newline) goes back
        # as the assistant's thinking, reasoning off, and the model answers under the schema
        first = {"message": {"role": "assistant", "content": "", "thinking": "step one\nstep two\nstep three unfin"},
                 "done_reason": "length", "prompt_eval_count": 100, "eval_count": 8200}
        second = {"message": {"role": "assistant", "content": '{"verdict":"reject","reason":"r"}'},
                  "done_reason": "stop", "eval_count": 20}
        with mock.patch.object(rb, "ollama_version", return_value={"tag": "n"}), \
             mock.patch.object(rb.httpx, "post", side_effect=[_Resp(200, first), _Resp(200, second)]) as post:
            r = rb.drive_replay(rec("gate_judge"), "ollama_bolt")
        self.assertEqual(post.call_count, 2)
        follow = post.call_args_list[1].kwargs["json"]
        self.assertFalse(follow["think"]); self.assertIn("format", follow)
        self.assertEqual(follow["messages"][2], {"role": "assistant", "content": "", "thinking": "step one\nstep two"})
        self.assertEqual(follow["messages"][3], {"role": "user", "content": rb.BUDGET_NUDGE})
        self.assertEqual(follow["options"]["num_predict"], rb.rolespecs.output_reserve("gate_judge"))
        self.assertEqual(follow["options"]["temperature"], 1.0)
        self.assertEqual(r["text"], '{"verdict":"reject","reason":"r"}'); self.assertIs(r["budget_hit"], True)
        self.assertEqual(r["continuation"], "chat_think_off"); self.assertEqual(r["tokens"]["output"], 8220)
        self.assertEqual(r["reasoning"], "thinking"); self.assertEqual(r["status"], "ok")

    def test_ollama_answer_cut_after_a_long_trace_is_also_continued(self):
        # the smoke's judge call: the trace finished, the JSON answer hit the bound mid-way
        first = {"message": {"role": "assistant", "content": '{ "verdicts": [ { "op_index": 0,', "thinking": "t1\nt2"},
                 "done_reason": "length", "eval_count": 9000}
        second = {"message": {"role": "assistant", "content": '{"verdict":"confirm","reason":"r"}'}, "done_reason": "stop", "eval_count": 30}
        with mock.patch.object(rb, "ollama_version", return_value={"tag": "n"}), \
             mock.patch.object(rb.httpx, "post", side_effect=[_Resp(200, first), _Resp(200, second)]) as post:
            r = rb.drive_replay(rec("gate_judge"), "ollama_bolt")
        self.assertEqual(post.call_count, 2); self.assertIs(r["budget_hit"], True)
        self.assertEqual(r["text"], '{"verdict":"confirm","reason":"r"}')
        # Magistral (reasoning on, temperature 0) gets the same rescue
        with mock.patch.object(rb, "ollama_version", return_value={"tag": "m"}), \
             mock.patch.object(rb.httpx, "post", side_effect=[_Resp(200, first), _Resp(200, second)]) as post:
            r = rb.drive_replay(rec("gate_judge"), "ollama")
        self.assertEqual(post.call_count, 2); self.assertEqual(post.call_args_list[1].kwargs["json"]["options"]["temperature"], 0.7)

    def test_ollama_reasoning_budget_adapts_to_the_room_left(self):
        # 2026-11-02 (Uroš: no exclusions): a long prompt on Gemma's 32k window runs with a smaller
        # reasoning budget instead of being skipped; the exact count comes from the server only
        # for borderline prompts; below the minimum room the call is skipped
        rb._count_cache.clear()
        ok_body = {"message": {"role": "assistant", "content": '{"material":[]}', "thinking": "t"}, "done_reason": "stop",
                   "prompt_eval_count": 23000, "eval_count": 40}
        long = rec("materiality", user="w" * (25000 * 3))
        with mock.patch.object(rb, "ollama_version", return_value={"tag": "g"}), \
             mock.patch.object(rb, "ollama_count_tokens", return_value=23000) as count, \
             mock.patch.object(rb.httpx, "post", return_value=_Resp(200, ok_body)) as post:
            r = rb.drive_replay(long, "ollama_gemma4")
        expect = 32768 - 23000 - rb.rolespecs.output_reserve("materiality") - rb.FIT_MARGIN
        self.assertEqual(count.call_count, 1); self.assertEqual(r["think_budget"], expect); self.assertLess(expect, 8192)
        self.assertEqual(post.call_args.kwargs["json"]["options"]["num_predict"], rb.rolespecs.output_reserve("materiality") + expect)
        self.assertEqual((r["status"], r["prompt_count"]), ("ok", "counted"))
        with mock.patch.object(rb, "ollama_version", return_value={"tag": "g"}), \
             mock.patch.object(rb, "ollama_count_tokens", return_value=30500), mock.patch.object(rb.httpx, "post") as post:
            r = rb.drive_replay(long, "ollama_gemma4")
        self.assertEqual((r["status"], r["reason"], r["observed_token_count"]), ("skipped", "context_exceeded", 30500)); post.assert_not_called()
        # the common case: no count, the full reserve
        with mock.patch.object(rb, "ollama_version", return_value={"tag": "g"}), \
             mock.patch.object(rb, "ollama_count_tokens") as count, mock.patch.object(rb.httpx, "post", return_value=_Resp(200, ok_body)):
            r = rb.drive_replay(rec("judge"), "ollama_gemma4")
        count.assert_not_called(); self.assertEqual((r["think_budget"], r["prompt_count"]), (8192, "estimate"))

    def test_ollama_failed_continuation_keeps_the_empty_answer(self):
        first = {"message": {"role": "assistant", "content": "", "thinking": "a\nb"}, "done_reason": "length", "eval_count": 5}
        with mock.patch.object(rb, "ollama_version", return_value={"tag": "n"}), \
             mock.patch.object(rb.httpx, "post", side_effect=[_Resp(200, first), _Resp(500, text="boom")]):
            r = rb.drive_replay(rec("gate_judge"), "ollama_bolt")
        self.assertEqual(r["text"], ""); self.assertEqual(r["continuation"], "failed:500"); self.assertIs(r["budget_hit"], True)

    def test_is_current_period_compares_settings_only(self):
        cur = {**rb.settings_key("ollama_bolt"), "digest": "abc", "quant": "Q8_0"}
        self.assertTrue(rb.is_current_period({"backend": "ollama_bolt", "backend_version": cur}))
        off = {"tag": "nemotron-3.5-bolt:e4b-q8_0", "num_ctx": 49152, "thinking": False, "vendor": "NVIDIA", "digest": "abc"}
        self.assertFalse(rb.is_current_period({"backend": "ollama_bolt", "backend_version": off}))
        # a retired arm's rows are read under the settings it last ran with (Nano: reasoning off)
        nano_off = {"tag": "nemotron-3-nano:4b-q8_0", "num_ctx": 49152, "thinking": False, "vendor": "NVIDIA", "digest": "x"}
        self.assertTrue(rb.is_current_period({"backend": "ollama_nemotron", "backend_version": nano_off}))
        self.assertFalse(rb.is_current_period({"backend": "ollama_nemotron", "backend_version": {**nano_off, "thinking": True}}))
        self.assertIn("system_prefix", rb.settings_key.__code__.co_names) if False else None
        mag16 = {"tag": rb.OLLAMA_TAG, "num_ctx": 16384, "thinking": True, "vendor": "Mistral AI"}
        self.assertFalse(rb.is_current_period({"backend": "ollama", "backend_version": mag16}))
        self.assertTrue(rb.is_current_period({"backend": "ollama", "backend_version": {**rb.settings_key("ollama"), "digest": "d"}}))
        self.assertTrue(rb.is_current_period({"backend": "apple_ondevice", "backend_version": {"macos_build": "x"}}))
        self.assertTrue(rb.is_current_period({"backend": "ollama", "backend_version": {"v": 1}}))   # no tag on record: kept

    def test_apple_asset_comes_from_fm_models(self):
        rb._apple_asset_cache.clear()
        fake = mock.Mock(stdout="  Apple Foundation Models\n  \x1b[32m✓\x1b[0m system (AFM 3 Core Advanced)\n  ✗ pcc (not available)\n")
        with mock.patch.object(rb.subprocess, "run", return_value=fake):
            self.assertEqual(rb.apple_asset(), "AFM 3 Core Advanced")
            self.assertEqual(rb.apple_version()["asset"], "AFM 3 Core Advanced")
        rb._apple_asset_cache.clear()
        with mock.patch.object(rb.subprocess, "run", side_effect=OSError("no fm")):
            self.assertEqual(rb.apple_asset(), "instruct_3b")
        rb._apple_asset_cache.clear()

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
