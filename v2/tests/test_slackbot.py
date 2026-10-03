"""Agent Scout in Slack (V1): the renderer, the engine client against a fake SSE server, the state
file, and the handlers against a fake Slack client. Nothing here reaches Slack or the engine."""
import json
import os
import tempfile
import unittest
from unittest import mock

import httpx

from slackbot import engine, render, state

ANSWER = {"id": "a_0123456789ab", "kind": "quick", "about": "OpenAI",
          "paragraphs": [{"text": "**OpenAI is not cheaper** at the mid tier.", "cites": [1]}, {"text": "Second point.", "cites": []}],
          "sources": [{"n": 1, "url": "https://www.cnbc.com/x", "class": "news", "tier": "reputable_secondary", "as_of": "2026-10-01"}],
          "cut_log": [{"label": "Luna is free", "reason": "no source"}], "unanswered": [], "verified": True,
          "permalink": "https://agent-scout.ai/answers/a_0123456789ab"}


class Render(unittest.TestCase):
    def test_blocks_carry_text_verified_line_and_buttons(self):
        text, blocks = render.answer_blocks(ANSWER)
        self.assertIn("*OpenAI is not cheaper*", text)
        self.assertEqual(blocks[0]["type"], "section"); self.assertIn("1 source", blocks[1]["elements"][0]["text"])
        ids = [b["action_id"] for b in blocks[2]["elements"]]
        self.assertEqual(ids, ["deeper", "sources", "cut", "open"])
        self.assertEqual(blocks[2]["elements"][-1]["url"], ANSWER["permalink"])

    def test_deep_answer_has_no_deeper_button_and_lists_sources_and_cuts(self):
        _, blocks = render.answer_blocks({**ANSWER, "kind": "deep"})
        self.assertNotIn("deeper", [b["action_id"] for b in blocks[2]["elements"]])
        self.assertIn("cnbc.com", render.sources_text(ANSWER)); self.assertIn("secondary", render.sources_text(ANSWER))
        self.assertIn("Luna is free", render.cut_text(ANSWER))

    def test_stage_and_limit_lines(self):
        self.assertIn("Fact-checking", render.stage_text("verify"))
        self.assertIn("10 quick questions", render.limit_text("quick", 10))


class EngineClient(unittest.TestCase):
    def _transport(self, frames, status=200):
        body = "".join(f"data: {json.dumps(f)}\n\n" for f in frames).encode()
        def handler(request):
            self.last = request
            if status != 200:
                return httpx.Response(status, json={"message": "Agent Scout's Slack budget for today is spent."})
            return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})
        return httpx.MockTransport(handler)

    def test_streams_frames_and_sends_the_request_token_and_history(self):
        frames = [{"stage": "facts"}, {"activity": "reading cnbc.com"}, {"done": True, "id": "a_0123456789ab", "answer": ANSWER}]
        client = httpx.Client(transport=self._transport(frames))
        with mock.patch.object(engine, "ENGINE_URL", "http://engine"), mock.patch.object(engine, "ASK_KEY", "slack-key"):
            got = list(engine.ask("Is it cheaper?", rid="tok", history=[{"question": "q", "answer_id": "a_ffffffffffff"}], client=client))
        self.assertEqual([list(f)[0] for f in got], ["stage", "activity", "done"])
        sent = json.loads(self.last.content)
        self.assertEqual(sent["rid"], "tok"); self.assertEqual(sent["mode"], "quick"); self.assertEqual(sent["history"][0]["answer_id"], "a_ffffffffffff")
        self.assertEqual(self.last.headers["authorization"], "Bearer slack-key")

    def test_non_200_becomes_one_error_frame(self):
        client = httpx.Client(transport=self._transport([], status=429))
        with mock.patch.object(engine, "ENGINE_URL", "http://engine"):
            got = list(engine.ask("x", rid="t", client=client))
        self.assertEqual(got[0]["status"], 429); self.assertIn("Slack budget", got[0]["error"])

    def test_request_token_is_stable_per_event_and_mode(self):
        a = engine.request_token("T1", "C1", "123.456"); b = engine.request_token("T1", "C1", "123.456")
        self.assertEqual(a, b); self.assertNotEqual(a, engine.request_token("T1", "C1", "123.456", salt="deep"))


class State(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = mock.patch.object(state, "PATH", os.path.join(self.tmp.name, "state.json")); self.p.start()

    def tearDown(self):
        self.p.stop(); self.tmp.cleanup()

    def test_history_and_answers_round_trip(self):
        self.assertEqual(state.history("T:C:1"), [])
        state.remember("T:C:1", "Is it cheaper?", ANSWER)
        self.assertEqual(state.history("T:C:1"), [{"question": "Is it cheaper?", "answer_id": "a_0123456789ab"}])
        self.assertEqual(state.answer("a_0123456789ab")["about"], "OpenAI")

    def test_daily_limits_per_user(self):
        with mock.patch.object(state, "QUICK_PER_USER", 2), mock.patch.object(state, "DEEP_PER_USER", 1):
            self.assertEqual(state.allow("U1", "quick"), (True, 2)); self.assertEqual(state.allow("U1", "quick"), (True, 2))
            self.assertEqual(state.allow("U1", "quick"), (False, 2))
            self.assertEqual(state.allow("U2", "quick"), (True, 2))                 # another user is not affected
            self.assertEqual(state.allow("U1", "deep"), (True, 1)); self.assertEqual(state.allow("U1", "deep"), (False, 1))


try:
    import slack_bolt  # noqa: F401
    HAVE_BOLT = True
except Exception:
    HAVE_BOLT = False


@unittest.skipUnless(HAVE_BOLT, "slack_bolt is installed only in the bot's own venv on the mini")
class Handlers(unittest.TestCase):
    """The exchange against a fake Slack client and a fake engine stream."""
    def setUp(self):
        os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test")
        self.tmp = tempfile.TemporaryDirectory()
        self.p = [mock.patch.object(state, "PATH", os.path.join(self.tmp.name, "state.json"))]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()
        self.tmp.cleanup()

    def test_answer_in_thread_posts_edits_and_finishes_with_blocks(self):
        from slackbot import app as bot
        calls = []
        class FakeClient:
            def auth_test(self): return {"user_id": "UBOT"}
            def chat_postMessage(self, **kw): calls.append(("post", kw)); return {"ts": "9.1"}
            def chat_update(self, **kw): calls.append(("update", kw)); return {"ok": True}
        frames = [{"stage": "facts"}, {"stage": "verify", "extra": "3 sentences"}, {"done": True, "id": "a_0123456789ab", "kind": "quick", "answer": ANSWER}]
        with mock.patch.object(bot.engine, "ask", return_value=iter(frames)) as ask:
            bot.answer_in_thread(FakeClient(), team="T1", channel="C1", thread_ts="1.0", user="U1", question="Is it cheaper?", ts_for_token="1.0")
        kinds = [k for k, _ in calls]
        self.assertEqual(kinds[0], "post"); self.assertIn("update", kinds)
        final = calls[-1][1]
        self.assertEqual(final["ts"], "9.1"); self.assertEqual(final["blocks"][2]["elements"][0]["action_id"], "deeper")
        self.assertEqual(ask.call_args.kwargs["rid"], bot.engine.request_token("T1", "C1", "1.0", salt="quick"))
        self.assertEqual(state.history("T1:C1:1.0")[0]["answer_id"], "a_0123456789ab")

    def test_limit_refuses_before_any_engine_call(self):
        from slackbot import app as bot
        posts = []
        class FakeClient:
            def auth_test(self): return {"user_id": "UBOT"}
            def chat_postMessage(self, **kw): posts.append(kw); return {"ts": "9.1"}
            def chat_update(self, **kw): return {"ok": True}
        with mock.patch.object(state, "QUICK_PER_USER", 0), mock.patch.object(bot.engine, "ask") as ask:
            bot.answer_in_thread(FakeClient(), team="T1", channel="C1", thread_ts="1.0", user="U1", question="x?")
        self.assertIn("limit resets", posts[0]["text"]); ask.assert_not_called()


if __name__ == "__main__":
    unittest.main()
