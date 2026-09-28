"""scout/localagent.py: the one loop over both tool protocols, with fake backends and fake tools.
Turn cap, tool budget, tool_failure classification, arg-schema slips, the Apple fit pre-flight, and
the refusal of non-local backends. Nothing is called: httpx and the tool functions are mocked."""
import json
import unittest
from unittest import mock

from scout import localagent as la, replaybackends as rb


def rec(role="triage", user="Competitor: X\n\nTRACKED SUBJECTS (subject_key — current value already known):\n- x|price — $10\n",
        max_turns=4, run_ts="2026-09-27T04:10:00"):
    return {"call_id": "c_loop1", "role": role, "model": "claude-haiku-4-5-20251001", "fidelity": "toolsful",
            "system": {"kind": "preset", "preset": "claude_code", "append": "TRIAGE SYSTEM"}, "user": user,
            "transcript": [], "result": {"text": "{}"}, "truncated": [],
            "options": {"max_turns": max_turns, "max_budget_usd": 0.5}, "run_ts": run_ts}


FINAL = json.dumps({"has_candidates": True, "candidates": [
    {"signal": "X raised price 2026-09-26", "subject_key": "x|price", "about": "competitor", "valence": "back_foot",
     "substantial": True, "why_new": "new", "source_hint": "https://news.example.com/x"}]})


def _search_ok(query):
    return ("T | https://news.example.com/x | snippet",
            {"name": "search", "query": query, "status": "ok", "http_status": 200, "result_count": 3,
             "unresponsive_engines": [], "hosts": ["news.example.com"]})


def _search_dead(query):
    return ("NO_RESULTS", {"name": "search", "query": query, "status": "empty", "http_status": 200, "result_count": 0,
                           "unresponsive_engines": [["duckduckgo", "CAPTCHA"]], "hosts": []})


def _fetch_ok(url, query):
    return ("PAGE TEXT about the price", {"name": "fetch_page", "url": url, "query": query, "status": "ok",
                                          "http_status": 200, "chars": 25, "hosts": [la._host(url)]})


class NativeProtocol(unittest.TestCase):
    def test_ollama_tool_calls_then_answer(self):
        steps = [
            {"status": "ok", "message": {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "search", "arguments": {"query": "X price"}}}]},
             "text": "", "tool_calls": [{"function": {"name": "search", "arguments": {"query": "X price"}}}],
             "thinking": "let me search", "duration_ms": 10, "tokens": {"input": 100, "output": 20}},
            {"status": "ok", "message": {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "fetch_page", "arguments": json.dumps({"url": "https://news.example.com/x", "query": "price"})}}]},
             "text": "", "tool_calls": [{"function": {"name": "fetch_page", "arguments": json.dumps({"url": "https://news.example.com/x", "query": "price"})}}],
             "duration_ms": 10, "tokens": {"input": 200, "output": 20}},
            {"status": "ok", "message": {"role": "assistant", "content": FINAL}, "text": FINAL, "tool_calls": [],
             "duration_ms": 10, "tokens": {"input": 300, "output": 80}},
        ]
        with mock.patch.object(la, "_ollama_turn", side_effect=steps) as turn, \
             mock.patch.object(la, "tool_search", side_effect=_search_ok), \
             mock.patch.object(la, "tool_fetch", side_effect=_fetch_ok), \
             mock.patch.object(rb, "ollama_version", return_value={"tag": "t"}):
            r = la.run_loop(rec(), "ollama")
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["text"], FINAL)
        self.assertEqual(r["loop"]["tool_protocol"], "native")
        self.assertEqual(r["loop"]["turns"], 3)
        self.assertEqual([t["name"] for t in r["loop"]["tool_calls"]], ["search", "fetch_page"])
        self.assertEqual(r["loop"]["hosts"], ["news.example.com"])
        self.assertFalse(r["loop"]["tool_failure"]); self.assertTrue(r["loop"]["tool_arg_schema_ok"])
        self.assertEqual(r["tokens"]["input"], 600); self.assertIn("let me search", r["thinking"])
        # the tool result went back as a role:tool message with the tool's name (the mock holds the
        # live list, so index the position, not the tail)
        msgs = turn.call_args_list[1][0][0]
        self.assertEqual(msgs[3]["role"], "tool"); self.assertEqual(msgs[3]["tool_name"], "search")
        self.assertIsNotNone(r["loop"]["replay_lag_hours"]); self.assertFalse(r["loop"]["format_retry"])
        self.assertEqual(r["cost_usd"], 0.0)

    def test_bad_args_are_a_schema_slip_not_a_crash(self):
        steps = [{"status": "ok", "message": {"role": "assistant", "content": ""}, "text": "",
                  "tool_calls": [{"function": {"name": "fetch_page", "arguments": {"url": "not-a-url"}}}],
                  "duration_ms": 1, "tokens": {}},
                 {"status": "ok", "message": {"role": "assistant", "content": FINAL}, "text": FINAL, "tool_calls": [],
                  "duration_ms": 1, "tokens": {}}]
        with mock.patch.object(la, "_ollama_turn", side_effect=steps), mock.patch.object(rb, "ollama_version", return_value={}):
            r = la.run_loop(rec(), "ollama")
        self.assertEqual(r["status"], "ok"); self.assertFalse(r["loop"]["tool_arg_schema_ok"])
        self.assertEqual(r["loop"]["tool_calls"][0]["status"], "bad_args")

    def test_turn_cap_forces_a_final_no_tools_turn(self):
        tool_step = {"status": "ok", "message": {"role": "assistant", "content": ""}, "text": "",
                     "tool_calls": [{"function": {"name": "search", "arguments": {"query": "q"}}}], "duration_ms": 1, "tokens": {}}
        final_step = {"status": "ok", "message": {"role": "assistant", "content": FINAL}, "text": FINAL, "tool_calls": [],
                      "duration_ms": 1, "tokens": {}}
        with mock.patch.object(la, "_ollama_turn", side_effect=[tool_step] * 4 + [final_step]) as turn, \
             mock.patch.object(la, "tool_search", side_effect=_search_ok), mock.patch.object(rb, "ollama_version", return_value={}):
            r = la.run_loop(rec(max_turns=4), "ollama")
        self.assertEqual(r["status"], "ok"); self.assertEqual(r["loop"]["turns"], 5)
        self.assertFalse(turn.call_args_list[-1].kwargs["tools"])           # the forced final turn has no tools
        self.assertIsNotNone(turn.call_args_list[-1].kwargs["schema"])      # and is schema-constrained
        self.assertIn("Turn limit reached", turn.call_args_list[-1][0][0][-1]["content"])

    def test_unparseable_final_gets_one_format_retry(self):
        steps = [{"status": "ok", "message": {"role": "assistant", "content": "Here is prose, no JSON"}, "text": "Here is prose, no JSON",
                  "tool_calls": [], "duration_ms": 1, "tokens": {}},
                 {"status": "ok", "message": {"role": "assistant", "content": FINAL}, "text": FINAL, "tool_calls": [],
                  "duration_ms": 1, "tokens": {}}]
        with mock.patch.object(la, "_ollama_turn", side_effect=steps) as turn, mock.patch.object(rb, "ollama_version", return_value={}):
            r = la.run_loop(rec(), "ollama")
        self.assertTrue(r["loop"]["format_retry"]); self.assertEqual(r["text"], FINAL)
        self.assertEqual(turn.call_count, 2)

    def test_tool_failure_when_every_search_is_dead(self):
        steps = [{"status": "ok", "message": {"role": "assistant", "content": ""}, "text": "",
                  "tool_calls": [{"function": {"name": "search", "arguments": {"query": "q1"}}}], "duration_ms": 1, "tokens": {}},
                 {"status": "ok", "message": {"role": "assistant", "content": ""}, "text": "",
                  "tool_calls": [{"function": {"name": "search", "arguments": {"query": "q2"}}}], "duration_ms": 1, "tokens": {}},
                 {"status": "ok", "message": {"role": "assistant", "content": '{"has_candidates": false, "candidates": []}'},
                  "text": '{"has_candidates": false, "candidates": []}', "tool_calls": [], "duration_ms": 1, "tokens": {}}]
        with mock.patch.object(la, "_ollama_turn", side_effect=steps), mock.patch.object(la, "tool_search", side_effect=_search_dead), \
             mock.patch.object(rb, "ollama_version", return_value={}):
            r = la.run_loop(rec(), "ollama")
        self.assertTrue(r["loop"]["tool_failure"])
        # one live search among dead ones is NOT a tool failure
        self.assertFalse(la._tool_failure([_search_dead("a")[1], _search_ok("b")[1]]))
        self.assertFalse(la._tool_failure([]))

    def test_identical_tool_calls_are_not_re_executed(self):
        tool_step = {"status": "ok", "message": {"role": "assistant", "content": ""}, "text": "",
                     "tool_calls": [{"function": {"name": "search", "arguments": {"query": "same"}}}], "duration_ms": 1, "tokens": {}}
        final_step = {"status": "ok", "message": {"role": "assistant", "content": FINAL}, "text": FINAL, "tool_calls": [],
                      "duration_ms": 1, "tokens": {}}
        with mock.patch.object(la, "_ollama_turn", side_effect=[tool_step, tool_step, tool_step, final_step]) as turn, \
             mock.patch.object(la, "tool_search", side_effect=_search_ok) as search, mock.patch.object(rb, "ollama_version", return_value={}):
            r = la.run_loop(rec(max_turns=6), "ollama")
        self.assertEqual(search.call_count, 1)                                  # executed once
        self.assertEqual(r["loop"]["repeated_tool_calls"], 2)
        self.assertEqual([t["status"] for t in r["loop"]["tool_calls"]], ["ok", "repeated", "repeated"])
        msgs = turn.call_args_list[2][0][0]
        self.assertIn("REPEATED CALL", msgs[5]["content"])                     # the nudge went back to the model
        self.assertFalse(r["loop"]["tool_failure"])

    def test_backend_error_mid_loop_is_surfaced(self):
        with mock.patch.object(la, "_ollama_turn", return_value={"status": "error", "reason": "transport", "text": "boom"}), \
             mock.patch.object(rb, "ollama_version", return_value={}):
            r = la.run_loop(rec(), "ollama")
        self.assertEqual((r["status"], r["reason"]), ("error", "transport"))


class ActionJsonProtocol(unittest.TestCase):
    def test_apple_actions_then_answer(self):
        steps = [{"status": "ok", "message": {}, "text": json.dumps({"action": "search", "query": "X price"}), "duration_ms": 1, "tokens": {"input": 50, "output": 5}},
                 {"status": "ok", "message": {}, "text": json.dumps({"action": "fetch_page", "url": "https://news.example.com/x", "query": "price"}), "duration_ms": 1, "tokens": {}},
                 {"status": "ok", "message": {}, "text": json.dumps({"action": "answer", "answer": FINAL}), "duration_ms": 1, "tokens": {}}]
        with mock.patch.object(la, "_apple_turn", side_effect=steps) as turn, mock.patch.object(la, "_apple_fits", return_value=(True, 1000)), \
             mock.patch.object(la, "tool_search", side_effect=_search_ok), mock.patch.object(la, "tool_fetch", side_effect=_fetch_ok), \
             mock.patch.object(rb, "apple_version", return_value={"macos_build": "x"}):
            r = la.run_loop(rec(), "apple_ondevice")
        self.assertEqual(r["status"], "ok"); self.assertEqual(r["text"], FINAL)
        self.assertEqual(r["loop"]["tool_protocol"], "action_json"); self.assertTrue(r["schema_enforced"])
        self.assertEqual(turn.call_args_list[0].kwargs["schema"], la.ACTION_SCHEMA)
        # tool results come back as user messages tagged with the action
        msgs = turn.call_args_list[1][0][0]
        self.assertEqual(msgs[3]["role"], "user"); self.assertTrue(msgs[3]["content"].startswith("[search result]"))
        self.assertEqual(r["reasoning"], "unsupported")

    def test_apple_context_exceeded_mid_loop_records_the_turn(self):
        steps = [{"status": "ok", "message": {}, "text": json.dumps({"action": "search", "query": "q"}), "duration_ms": 1, "tokens": {}}]
        fits = [(True, 1000), (False, 9000)]
        with mock.patch.object(la, "_apple_turn", side_effect=steps), mock.patch.object(la, "_apple_fits", side_effect=fits), \
             mock.patch.object(la, "tool_search", side_effect=_search_ok), mock.patch.object(rb, "apple_version", return_value={}):
            r = la.run_loop(rec(), "apple_ondevice")
        self.assertEqual((r["status"], r["reason"]), ("skipped", "context_exceeded"))
        self.assertEqual(r["loop"]["turn_at_exceed"], 2); self.assertEqual(r["observed_token_count"], 9000)

    def test_unparseable_action_is_a_parse_fail(self):
        with mock.patch.object(la, "_apple_turn", return_value={"status": "ok", "message": {}, "text": "not json", "duration_ms": 1, "tokens": {}}), \
             mock.patch.object(la, "_apple_fits", return_value=(True, 10)), mock.patch.object(rb, "apple_version", return_value={}):
            r = la.run_loop(rec(), "apple_ondevice")
        self.assertEqual((r["status"], r["reason"]), ("error", "parse_fail")); self.assertFalse(r["loop"]["tool_call_parse_ok"])


class Guards(unittest.TestCase):
    def test_non_local_backends_are_refused(self):
        for b in ("anthropic", "apple_pcc", "mistral_hosted"):
            with self.assertRaises(la.UnsupportedBackend):
                la.run_loop(rec(), b)

    def test_prompts_and_turn_cap_come_from_the_record(self):
        system, user = la.loop_prompts(rec(), "native")
        self.assertTrue(system.startswith(la._LOOP_PREAMBLE)); self.assertIn("TRIAGE SYSTEM", system)
        self.assertIn("TRACKED SUBJECTS", user)
        self.assertEqual(la.turn_cap(rec(max_turns=8)), 8)
        r = rec(); r["options"] = {}
        self.assertEqual(la.turn_cap(r), la.config.TRIAGE_MAX_TURNS)

    def test_search_tool_reads_searxng_json_and_budgets_hits(self):
        class _R:
            status_code = 200
            def json(self):
                return {"results": [{"title": f"t{i}", "url": f"https://h{i}.example.com/p", "content": "x" * 500} for i in range(9)],
                        "unresponsive_engines": [["duckduckgo", "CAPTCHA"]]}
        with mock.patch.object(la.httpx, "get", return_value=_R()):
            text, meta = la.tool_search("q")
        self.assertEqual(len(text.splitlines()), la.LOOP_BUDGET["search_hits"])
        self.assertTrue(all(len(line.split(" | ")[2]) <= la.LOOP_BUDGET["hit_chars"] for line in text.splitlines()))
        self.assertEqual(meta["result_count"], 9); self.assertEqual(meta["unresponsive_engines"], [["duckduckgo", "CAPTCHA"]])
        self.assertEqual(meta["hosts"][0], "h0.example.com")

    def test_fetch_tool_uses_the_loop_budget_not_the_live_one(self):
        from scout import fetch_tool
        page = "price " * 5000
        with mock.patch.object(la, "tool_fetch", wraps=la.tool_fetch), \
             mock.patch("scout.grounding._fetch_response", return_value=mock.Mock(status_code=200)), \
             mock.patch("scout.grounding._extract_text", return_value=(page, "html")):
            text, meta = la.tool_fetch("https://x.example.com/p", "price")
        self.assertLessEqual(len(text), la.LOOP_BUDGET["fetch_chars"] + 50)
        self.assertLess(len(text), fetch_tool.WINDOW_BUDGET_CHARS)
        self.assertEqual(meta["hosts"], ["x.example.com"])


if __name__ == "__main__":
    unittest.main()
