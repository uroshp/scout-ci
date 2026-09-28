"""Call capture invariants (scout/calllog.py, 2026-09-28). The capture is a pure observer on the
live path: off = no-op; on = records the exact inputs/outputs of every call, bundled once per run,
private store only, never raising. Pure, no model, no network."""
import json
import unittest
from unittest import mock

from scout import calllog, config, selfserve


class _Opts:
    def __init__(self, system="SYS", allowed=(), model="claude-x", mcp=None):
        self.system_prompt = system
        self.allowed_tools = list(allowed)
        self.disallowed_tools = ["WebSearch", "WebFetch"]
        self.mcp_servers = mcp or {}
        self.model = model
        self.max_turns = 6
        self.max_budget_usd = 0.75
        self.permission_mode = "bypassPermissions"


class _Block:
    def __init__(self, kind, **kw):
        self.__class__ = type(kind, (), {})
        for k, v in kw.items():
            setattr(self, k, v)


class _Msg:
    def __init__(self, kind, content, parent=None, usage=None):
        self.__class__ = type(kind, (), {})
        self.content = content
        self.parent_tool_use_id = parent
        self.usage = usage


def _enable(on=True):
    return mock.patch.object(config, "CALL_CAPTURE_ENABLED", on)


class Disabled(unittest.TestCase):
    def test_everything_is_a_noop_when_disabled(self):
        with _enable(False):
            calllog.begin_run("monitor")
            self.assertFalse(calllog.run_open())
            self.assertIsNone(calllog.start("judge", "u", _Opts()))
            calllog.record_direct(role="persona", model="m", system="s", user="u", result_text="x")
            self.assertEqual(calllog.flush_run(True), [])


class RecordShape(unittest.TestCase):
    def setUp(self):
        calllog._RUN = None
        calllog._CTX.clear()

    def test_exact_call_recorded_whole(self):
        with _enable():
            calllog.begin_run("monitor")
            calllog.set_context(slug="a__vs__b", phase="propagation")
            big = "x" * 50_000
            cap = calllog.start("judge", big, _Opts(system="S" * 10_000))
            self.assertIsNotNone(cap)
            cap.event(_Msg("AssistantMessage", [_Block("TextBlock", text="hello")]), "judge")
            cap.finish({"text": '{"verdicts": []}', "cost_usd": 0.4, "duration_ms": 10, "num_turns": 1,
                        "by_role": {"judge": {"input": 1}}, "model_usage": None})
            rec = calllog._RUN["calls"][0]
            self.assertEqual(rec["fidelity"], "exact")
            self.assertEqual(rec["system"], {"kind": "string", "text": "S" * 10_000})
            self.assertEqual(rec["user"], big)                       # never clipped
            self.assertEqual(rec["result"]["text"], '{"verdicts": []}')
            self.assertEqual(rec["slug"], "a__vs__b")
            self.assertEqual(rec["status"], "ok")
            self.assertEqual(rec["truncated"], [])
            self.assertTrue(rec["call_id"].startswith("c_"))
            self.assertEqual(rec["transcript"][0]["role"], "judge")

    def test_toolsful_call_keeps_tool_results_from_user_messages(self):
        with _enable():
            calllog.begin_run("monitor")
            opts = _Opts(system={"type": "preset", "preset": "claude_code", "append": "APPEND"},
                         allowed=["WebSearch", "fetch_page"], mcp={"scoutfetch": {}})
            cap = calllog.start("materiality", "u", opts)
            cap.event(_Msg("AssistantMessage", [_Block("ToolUseBlock", id="t1", name="fetch_page",
                                                       input={"url": "https://x"})]), "materiality")
            # tool results arrive ONLY as UserMessage(content=[ToolResultBlock]) (R15)
            cap.event(_Msg("UserMessage", [_Block("ToolResultBlock", tool_use_id="t1", is_error=False,
                                                  content=[{"type": "text", "text": "PAGE " * 3000}])],
                           parent="sub1"), "subagent")
            cap.finish({"text": "{}", "cost_usd": 1.0})
            rec = calllog._RUN["calls"][0]
            self.assertEqual(rec["fidelity"], "toolsful")
            self.assertEqual(rec["system"]["kind"], "preset")
            self.assertEqual(rec["options"]["mcp_servers"], ["scoutfetch"])
            kinds = [r["kind"] for r in rec["transcript"]]
            self.assertEqual(kinds, ["tool_use", "tool_result"])
            tr = rec["transcript"][1]
            self.assertEqual(tr["parent_tool_use_id"], "sub1")
            self.assertEqual(tr["tool_use_id"], "t1")
            self.assertEqual(len(tr["content"]), len("PAGE " * 3000))   # whole for the monitor roles
            self.assertEqual(rec["truncated"], [])

    def test_generation_transcripts_are_capped_and_marked(self):
        with _enable():
            calllog.begin_run("selfserve")
            cap = calllog.start("orchestrator", "u", _Opts(allowed=["Agent"], system={"type": "preset", "preset": "claude_code", "append": "A"}))
            cap.event(_Msg("UserMessage", [_Block("ToolResultBlock", tool_use_id="t", is_error=False,
                                                  content="Z" * 10_000)]), "researcher")
            cap.finish({"text": "{}"})
            rec = calllog._RUN["calls"][0]
            self.assertEqual(len(rec["transcript"][0]["content"]), calllog.TRANSCRIPT_ROW_MAX)
            self.assertIn("transcript[0].content", rec["truncated"])
            self.assertEqual(rec["user"], "u")                         # exact fields untouched

    def test_failure_is_recorded_and_exception_still_propagates(self):
        with _enable():
            calllog.begin_run("monitor")
            cap = calllog.start("judge", "u", _Opts())
            try:
                raise RuntimeError("boom")
            except RuntimeError as e:
                cap.fail(e, {"judge": {"input": 5}})
            rec = calllog._RUN["calls"][0]
            self.assertEqual(rec["status"], "failed")
            self.assertIn("boom", rec["error"])

    def test_redaction(self):
        with _enable():
            calllog.begin_run("monitor")
            cap = calllog.start("judge", "key sk-ant-abcdefghijkl here", _Opts(system="ghp_0123456789abcdef"))
            cap.finish({"text": "github_pat_ABCDEFGHIJ_k"})
            rec = calllog._RUN["calls"][0]
            self.assertNotIn("sk-ant-abc", rec["user"])
            self.assertNotIn("ghp_0123", rec["system"]["text"])
            self.assertNotIn("github_pat_ABC", rec["result"]["text"])

    def test_direct_persona_call_recorded(self):
        with _enable():
            calllog.begin_run("monitor")
            calllog.record_direct(role="persona", model="claude-haiku", system="S", user="U",
                                  tools=[{"name": "assign_persona"}], tool_choice={"type": "tool"},
                                  result_text='{"persona": "economic_buyer"}',
                                  usage={"input_tokens": 12, "output_tokens": 3}, duration_ms=40)
            rec = calllog._RUN["calls"][0]
            self.assertEqual(rec["role"], "persona")
            self.assertEqual(rec["fidelity"], "exact")
            self.assertEqual(rec["result"]["by_role"]["persona"]["input"], 12)

    def test_no_open_run_means_no_capture(self):
        with _enable():
            self.assertIsNone(calllog.start("judge", "u", _Opts()))
            calllog.record_direct(role="persona", model="m", system="s", user="u", result_text="x")
            self.assertFalse(calllog.run_open())


class Flush(unittest.TestCase):
    def setUp(self):
        calllog._RUN = None

    def _run_with(self, n=2, size=10):
        calllog.begin_run("monitor")
        for i in range(n):
            cap = calllog.start("judge", "u" * size, _Opts())
            cap.finish({"text": "t", "cost_usd": 0.5})

    def test_dry_run_writes_nothing(self):
        with _enable(), mock.patch.object(selfserve, "write_data") as wd, \
             mock.patch.object(selfserve, "use_github", return_value=True):
            self._run_with()
            self.assertEqual(calllog.flush_run(False), [])
            wd.assert_not_called()
            self.assertFalse(calllog.run_open())

    def test_no_creds_drops_bundle(self):
        with _enable(), mock.patch.object(selfserve, "write_data") as wd, \
             mock.patch.object(selfserve, "use_github", return_value=False):
            self._run_with()
            self.assertEqual(calllog.flush_run(True), [])
            wd.assert_not_called()

    def test_writes_once_under_calls_month_dir(self):
        with _enable(), mock.patch.object(selfserve, "write_data") as wd, \
             mock.patch.object(selfserve, "use_github", return_value=True):
            self._run_with()
            paths = calllog.flush_run(True)
            self.assertEqual(len(paths), 1)
            self.assertTrue(paths[0].startswith("calls/20"))
            self.assertIn("/monitor_", paths[0])
            wd.assert_called_once()
            doc = json.loads(wd.call_args[0][1])
            self.assertEqual(doc["n_calls"], 2)
            self.assertEqual(doc["total_cost_usd"], 1.0)

    def test_split_keeps_every_part_under_the_read_ceiling(self):
        with _enable(), mock.patch.object(selfserve, "write_data") as wd, \
             mock.patch.object(selfserve, "use_github", return_value=True):
            self._run_with(n=6, size=600_000)                    # ~3.6 MB of prompts
            paths = calllog.flush_run(True)
            self.assertGreater(len(paths), 1)
            for call in wd.call_args_list:
                self.assertLessEqual(len(call[0][1].encode("utf-8")), 1_000_000)
                doc = json.loads(call[0][1])
                for c in doc["calls"]:
                    self.assertEqual(len(c["user"]), 600_000)     # stored intact, never clipped
                    self.assertEqual(c["truncated"], [])

    def test_transient_write_failure_retries_then_lands_once(self):
        class _Resp:
            status_code = 502
        err = Exception("502")
        err.response = _Resp()
        with _enable(), mock.patch.object(selfserve, "write_data", side_effect=[err, None]) as wd, \
             mock.patch.object(selfserve, "use_github", return_value=True), \
             mock.patch.object(calllog.time, "sleep"):
            self._run_with()
            self.assertEqual(len(calllog.flush_run(True)), 1)
            self.assertEqual(wd.call_count, 2)

    def test_non_transient_write_failure_is_not_retried(self):
        class _Resp:
            status_code = 401
        err = Exception("401")
        err.response = _Resp()
        with _enable(), mock.patch.object(selfserve, "write_data", side_effect=err) as wd, \
             mock.patch.object(selfserve, "use_github", return_value=True):
            self._run_with()
            self.assertEqual(calllog.flush_run(True), [])
            self.assertEqual(wd.call_count, 1)

    def test_selfserve_run_flushes_even_though_generate_is_write_false(self):
        """run_selfserve.py calls generate(..., write=False) — that flag means 'do not touch the
        battlecards', not 'dry run'; the spend is real, so the bundle must land. The run is opened
        and flushed by the script, and generate's own functions never close it underneath."""
        from scout import generate
        with _enable(), mock.patch.object(selfserve, "write_data") as wd, \
             mock.patch.object(selfserve, "use_github", return_value=True):
            calllog.begin_run("selfserve")
            calllog.set_context(slug="x__vs__y", phase="generate", job_id="job1")
            self.assertFalse(hasattr(generate.generate, "flush_run"))   # generate() never flushes
            cap = calllog.start("generate", "prompt", _Opts(system={"type": "preset", "preset": "claude_code",
                                                                      "append": "A"}, allowed=["WebSearch"]))
            cap.finish({"text": "done", "cost_usd": 2.0})
            self.assertTrue(calllog.run_open())                          # still open after the call
            paths = calllog.flush_run(True)                              # the script's finally
            self.assertEqual(len(paths), 1)
            self.assertIn("/selfserve_", paths[0])
            doc = json.loads(wd.call_args[0][1])
            self.assertEqual(doc["calls"][0]["context"]["job_id"], "job1")
            self.assertEqual(doc["calls"][0]["fidelity"], "toolsful")


class CallSites(unittest.TestCase):
    """The live call sites' ClaudeAgentOptions round-trip through record_from: what a backend gets
    to replay is exactly what the paid model was given (tools-off roles), and the tools-on roles are
    marked so they are never replayed as if they were."""

    def setUp(self):
        calllog._RUN = None
        calllog._CTX.clear()

    def test_tools_off_site_records_exact_fidelity(self):
        from claude_agent_sdk import ClaudeAgentOptions
        opts = ClaudeAgentOptions(model="claude-opus-4-8", system_prompt="ROUTE SYS", mcp_servers={},
                                  allowed_tools=[], disallowed_tools=["WebSearch", "WebFetch"],
                                  permission_mode="bypassPermissions", max_turns=4, max_budget_usd=1.5)
        rec = calllog.record_from("USER", opts, "route")
        self.assertEqual(rec["fidelity"], "exact")
        self.assertEqual(rec["system"], {"kind": "string", "text": "ROUTE SYS"})
        self.assertEqual(rec["user"], "USER")
        self.assertEqual(rec["model"], "claude-opus-4-8")
        self.assertEqual(rec["options"], {"max_turns": 4, "max_budget_usd": 1.5, "allowed_tools": [],
                                          "disallowed_tools": ["WebSearch", "WebFetch"], "mcp_servers": [],
                                          "permission_mode": "bypassPermissions"})

    def test_tools_on_site_records_toolsful_fidelity_with_preset(self):
        from claude_agent_sdk import ClaudeAgentOptions
        opts = ClaudeAgentOptions(model="claude-haiku-4-5-20251001",
                                  system_prompt={"type": "preset", "preset": "claude_code", "append": "TRIAGE"},
                                  mcp_servers={"scoutfetch": {"type": "stdio", "command": "x"}},
                                  allowed_tools=["WebSearch", "mcp__scoutfetch__fetch_page"],
                                  disallowed_tools=["WebFetch"], permission_mode="bypassPermissions",
                                  max_turns=8, max_budget_usd=0.6)
        rec = calllog.record_from("USER", opts, "triage")
        self.assertEqual(rec["fidelity"], "toolsful")
        self.assertEqual(rec["system"], {"kind": "preset", "preset": "claude_code", "append": "TRIAGE"})
        self.assertEqual(rec["options"]["mcp_servers"], ["scoutfetch"])
        self.assertEqual(rec["options"]["allowed_tools"], ["WebSearch", "mcp__scoutfetch__fetch_page"])


class DriveEndToEnd(unittest.TestCase):
    """generate._drive with `query` mocked: per-message roles reach the capture rows, subagent tool
    results are kept, a mid-stream failure is recorded AND still propagates."""

    def setUp(self):
        calllog._RUN = None
        calllog._CTX.clear()

    @staticmethod
    def _stream(messages):
        async def _q(prompt, options):
            for m in messages:
                if isinstance(m, BaseException):
                    raise m
                yield m
        return _q

    def _run(self, coro):
        import asyncio
        return asyncio.run(coro)

    def test_roles_follow_the_subagent_map_and_result_is_captured(self):
        from scout import generate
        agent_use = _Block("ToolUseBlock", name="Agent", id="tu_1", input={"subagent_type": "researcher"})
        msgs = [
            _Msg("AssistantMessage", [_Block("TextBlock", text="planning"), agent_use],
                 usage={"input_tokens": 10, "output_tokens": 5}),
            _Msg("AssistantMessage", [_Block("ToolUseBlock", name="WebSearch", id="tu_2", input={"query": "q"})],
                 parent="tu_1", usage={"input_tokens": 3, "output_tokens": 1}),
            _Msg("UserMessage", [_Block("ToolResultBlock", tool_use_id="tu_2", content="RESULT TEXT")], parent="tu_1"),
            _Msg("AssistantMessage", [_Block("TextBlock", text="final")], usage={"input_tokens": 1, "output_tokens": 1}),
        ]
        res = _Msg("ResultMessage", [])
        res.result = "final"; res.total_cost_usd = 0.42; res.duration_ms = 100; res.duration_api_ms = 90
        res.num_turns = 3; res.model_usage = None
        msgs.append(res)
        with _enable(), mock.patch.object(generate, "query", self._stream(msgs)), \
             mock.patch.object(generate, "_emit_stage"):
            calllog.begin_run("monitor")
            calllog.set_context(slug="s", phase="monitor")
            out = self._run(generate._drive("PROMPT", _Opts(allowed=["WebSearch"]), "triage"))
        self.assertEqual(out["text"], "final")
        rec = calllog._RUN["calls"][0]
        self.assertEqual(rec["status"], "ok")
        self.assertEqual(rec["result"]["text"], "final")
        self.assertEqual(rec["result"]["cost_usd"], 0.42)
        roles = [(r["role"], r["kind"]) for r in rec["transcript"]]
        self.assertIn(("triage", "text"), roles)
        self.assertIn(("researcher", "tool_use"), roles)
        self.assertIn(("researcher", "tool_result"), roles)
        tr = [r for r in rec["transcript"] if r["kind"] == "tool_result"][0]
        self.assertEqual(tr["content"], "RESULT TEXT")
        self.assertEqual(tr["tool_use_id"], "tu_2")

    def test_mid_stream_failure_is_recorded_and_reraised(self):
        from scout import generate
        msgs = [_Msg("AssistantMessage", [_Block("TextBlock", text="partial")],
                     usage={"input_tokens": 10, "output_tokens": 5}),
                RuntimeError("boom")]
        with _enable(), mock.patch.object(generate, "query", self._stream(msgs)), \
             mock.patch.object(generate, "_emit_stage"), mock.patch.object(generate.sys, "stderr"):
            calllog.begin_run("monitor")
            with self.assertRaises(RuntimeError):
                self._run(generate._drive("PROMPT", _Opts(), "judge"))
        rec = calllog._RUN["calls"][0]
        self.assertEqual(rec["status"], "failed")
        self.assertIn("boom", rec["error"])
        self.assertEqual(rec["transcript"][0]["role"], "judge")

    def test_drive_without_open_run_captures_nothing_and_still_returns(self):
        from scout import generate
        res = _Msg("ResultMessage", []); res.result = "ok"; res.total_cost_usd = 0.1
        res.duration_ms = res.duration_api_ms = res.num_turns = None; res.model_usage = None
        with _enable(), mock.patch.object(generate, "query", self._stream([res])), \
             mock.patch.object(generate, "_emit_stage"):
            out = self._run(generate._drive("P", _Opts(), "judge"))
        self.assertEqual(out["text"], "ok")
        self.assertIsNone(calllog._RUN)


if __name__ == "__main__":
    unittest.main()
