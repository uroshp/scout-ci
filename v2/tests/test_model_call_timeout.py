"""Every model call has a wall-clock deadline (2026-10-08: the 4 AM run hung for six hours inside one
call and the morning was lost). A stream that stops yielding raises ModelCallTimeout with the spend
so far, like every other mid-stream failure."""
import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scout import config, generate  # noqa: E402


class ModelCallDeadline(unittest.TestCase):
    def test_a_stalled_stream_raises_with_the_spend_so_far(self):
        async def stalled(prompt, options):
            yield type("AssistantMessage", (), {"usage": {"input_tokens": 10, "output_tokens": 2}, "content": [], "parent_tool_use_id": None})()
            await asyncio.sleep(30)        # never yields again
            yield None
        opts = type("O", (), {"system_prompt": "s", "model": "m"})()
        with mock.patch.object(generate, "query", stalled), mock.patch.object(config, "MODEL_CALL_TIMEOUT_S", 1), mock.patch.object(config, "MODEL_CALL_IDLE_S", 1), mock.patch.object(config, "MODEL_CALL_STALL_RETRIES", 0), \
             mock.patch.object(generate.judgment, "require", lambda: None), mock.patch.object(generate.judgment, "assert_clean", lambda *a, **k: None), \
             mock.patch.object(generate.calllog, "start", return_value=None):
            with self.assertRaises(generate.ModelCallTimeout) as cm:
                asyncio.run(generate._drive("p", opts, "triage"))
        self.assertIn("triage call", str(cm.exception))
        self.assertIsNone(cm.exception.scout_cost_usd)          # one message seen, cost unknown: the ledger fails closed

    def test_a_stream_that_never_started_is_a_known_zero(self):
        async def silent(prompt, options):
            await asyncio.sleep(30)
            yield None
        opts = type("O", (), {"system_prompt": "s", "model": "m"})()
        with mock.patch.object(generate, "query", silent), mock.patch.object(config, "MODEL_CALL_TIMEOUT_S", 1), mock.patch.object(config, "MODEL_CALL_IDLE_S", 1), mock.patch.object(config, "MODEL_CALL_STALL_RETRIES", 0), \
             mock.patch.object(generate.judgment, "require", lambda: None), mock.patch.object(generate.judgment, "assert_clean", lambda *a, **k: None), \
             mock.patch.object(generate.calllog, "start", return_value=None):
            with self.assertRaises(generate.ModelCallTimeout) as cm:
                asyncio.run(generate._drive("p", opts, "judge"))
        self.assertEqual(cm.exception.scout_cost_usd, 0.0)

    def test_a_stall_is_retried_once_from_a_fresh_process_then_fails(self):
        calls = {"n": 0}

        async def stalls_every_time(prompt, options):
            calls["n"] += 1
            yield type("AssistantMessage", (), {"usage": {"input_tokens": 1, "output_tokens": 1}, "content": [], "parent_tool_use_id": None})()
            await asyncio.sleep(30)
            yield None
        opts = type("O", (), {"system_prompt": "s", "model": "m"})()
        with mock.patch.object(generate, "query", stalls_every_time), mock.patch.object(config, "MODEL_CALL_IDLE_S", 1), \
             mock.patch.object(config, "MODEL_CALL_TIMEOUT_S", 60), mock.patch.object(config, "MODEL_CALL_STALL_RETRIES", 1), \
             mock.patch.object(generate.judgment, "require", lambda: None), mock.patch.object(generate.judgment, "assert_clean", lambda *a, **k: None), \
             mock.patch.object(generate.calllog, "start", return_value=None):
            with self.assertRaises(generate.ModelCallTimeout) as cm:
                asyncio.run(generate._drive("p", opts, "materiality"))
        self.assertEqual(calls["n"], 2)                             # one retry, then the loud failure
        self.assertIn("went silent", str(cm.exception))

    def test_a_stall_that_recovers_on_retry_returns_normally(self):
        calls = {"n": 0}

        async def second_time_lucky(prompt, options):
            calls["n"] += 1
            if calls["n"] == 1:
                await asyncio.sleep(30)
            yield type("ResultMessage", (), {"result": "ok", "total_cost_usd": 0.2, "duration_ms": 5, "duration_api_ms": 4, "num_turns": 1, "parent_tool_use_id": None, "usage": None})()
        opts = type("O", (), {"system_prompt": "s", "model": "m"})()
        with mock.patch.object(generate, "query", second_time_lucky), mock.patch.object(config, "MODEL_CALL_IDLE_S", 1), \
             mock.patch.object(config, "MODEL_CALL_TIMEOUT_S", 60), mock.patch.object(config, "MODEL_CALL_STALL_RETRIES", 1), \
             mock.patch.object(generate.judgment, "require", lambda: None), mock.patch.object(generate.judgment, "assert_clean", lambda *a, **k: None), \
             mock.patch.object(generate.calllog, "start", return_value=None), mock.patch.object(generate, "_merge_role_totals", lambda *a, **k: None):
            out = asyncio.run(generate._drive("p", opts, "judge"))
        self.assertEqual(out["text"], "ok")
        self.assertEqual(calls["n"], 2)

    def test_default_deadline_is_generous_but_bounded(self):
        self.assertGreaterEqual(config.MODEL_CALL_TIMEOUT_S, 600)
        self.assertLessEqual(config.MODEL_CALL_TIMEOUT_S, 3600)


if __name__ == "__main__":
    unittest.main()
