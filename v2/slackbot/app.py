"""Agent Scout in Slack: the Bolt app. Run on the Mac mini: `python -m slackbot.app` from v2/, with
SLACK_BOT_TOKEN, SLACK_APP_TOKEN, SCOUT_ENGINE_URL and ASK_SLACK_KEY in the environment.

Behaviour (like Claude in Slack): DM it or @mention it; it answers in the thread; follow-ups in the
same thread keep the scope; while it works the one message it posted is edited in place with the
engine's stage lines; the answer carries the verified line and buttons for Research deeper, Sources,
What was cut and the permalink. In a workspace with the Agents & AI Apps feature it also sets the
suggested prompts when an assistant thread opens. Ack first, work in a thread pool (Slack's 3 s)."""
from __future__ import annotations

import logging
import os
import re
import sys
import threading

from slack_bolt import App, Assistant
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk.errors import SlackApiError

from slackbot import engine, render, state

log = logging.getLogger("scout.slack")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

app = App(token=os.environ.get("SLACK_BOT_TOKEN"), token_verification_enabled=False)
assistant = Assistant()
_BOT_USER = {"id": None}


def _bot_user_id(client) -> str | None:
    if _BOT_USER["id"] is None:
        try:
            _BOT_USER["id"] = client.auth_test()["user_id"]
        except Exception:
            return None
    return _BOT_USER["id"]


def _clean(text: str, client) -> str:
    uid = _bot_user_id(client)
    t = re.sub(rf"<@{uid}>", "", text or "") if uid else (text or "")
    return re.sub(r"\s+", " ", t).strip()


def _thread_key(team: str, channel: str, thread_ts: str) -> str:
    return f"{team}:{channel}:{thread_ts}"


def answer_in_thread(client, *, team: str, channel: str, thread_ts: str, user: str, question: str,
                     mode: str = "quick", ts_for_token: str | None = None) -> None:
    """The whole exchange for one question, in a worker thread."""
    mode = "deep" if mode == "deep" else "quick"
    if not question:
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text="Ask me a question about one of the competitors Scout tracks.")
        return
    if len(question) > 400:
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text="Keep it under 400 characters, please.")
        return
    ok, cap = state.allow(user, mode)
    if not ok:
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=render.limit_text(mode, cap))
        return
    working = client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=render.stage_text("facts"))
    wts = working["ts"]
    rid = engine.request_token(team, channel, ts_for_token or wts, salt=mode)
    hist = state.history(_thread_key(team, channel, thread_ts))
    final = None
    try:
        for frame in engine.ask(question, mode=mode, rid=rid, history=hist):
            if "stage" in frame:
                _edit(client, channel, wts, render.stage_text(frame["stage"], frame.get("extra") or ""))
            elif "activity" in frame and frame["activity"]:
                _edit(client, channel, wts, render.stage_text("search", str(frame["activity"])[:140]))
            elif frame.get("done"):
                final = frame
                break
            elif "error" in frame:
                final = frame
                break
    except Exception as e:  # noqa: BLE001
        log.exception("engine stream failed")
        final = {"error": f"Scout could not reach its engine ({type(e).__name__}). Try again in a minute."}
    if not final or "error" in (final or {}):
        msg = (final or {}).get("error") or "Scout could not answer that."
        _edit(client, channel, wts, render.refusal_text(msg))
        return
    answer = final.get("answer") or {}
    answer.setdefault("kind", final.get("kind") or mode)
    answer.setdefault("id", final.get("id"))
    text, blocks = render.answer_blocks(answer)
    client.chat_update(channel=channel, ts=wts, text=text[:3000], blocks=blocks)
    state.remember(_thread_key(team, channel, thread_ts), question, {**answer, "question": question})


def _edit(client, channel, ts, text):
    try:
        client.chat_update(channel=channel, ts=ts, text=text)
    except SlackApiError as e:
        log.warning("chat.update failed: %s", e.response.get("error") if e.response else e)


def _spawn(fn, *a, **kw):
    threading.Thread(target=fn, args=a, kwargs=kw, daemon=True).start()


# --- assistant threads (Agents & AI Apps workspaces) --------------------------------------------
@assistant.thread_started
def _started(say, set_suggested_prompts):
    say(render.WELCOME)
    try:
        set_suggested_prompts(prompts=[{"title": q[:40], "message": q} for q in render.EXAMPLES], title="Try one of these")
    except Exception:  # noqa: BLE001
        pass


@assistant.user_message
def _assistant_message(payload, client, context, set_status):
    try:
        set_status("thinking…")
    except Exception:  # noqa: BLE001
        pass
    _spawn(answer_in_thread, client, team=context.team_id or "", channel=payload["channel"],
           thread_ts=payload.get("thread_ts") or payload["ts"], user=payload.get("user") or "",
           question=_clean(payload.get("text", ""), client), ts_for_token=payload["ts"])


app.use(assistant)


# --- classic DMs and mentions (every workspace) ------------------------------------------------
@app.event("message")
def _dm(event, client, context):
    if event.get("channel_type") != "im" or event.get("subtype") or event.get("bot_id"):
        return
    _spawn(answer_in_thread, client, team=context.team_id or "", channel=event["channel"],
           thread_ts=event.get("thread_ts") or event["ts"], user=event.get("user") or "",
           question=_clean(event.get("text", ""), client), ts_for_token=event["ts"])


@app.event("app_mention")
def _mention(event, client, context):
    _spawn(answer_in_thread, client, team=context.team_id or "", channel=event["channel"],
           thread_ts=event.get("thread_ts") or event["ts"], user=event.get("user") or "",
           question=_clean(event.get("text", ""), client), ts_for_token=event["ts"])


@app.event("team_join")
def _welcome(event, client):
    uid = (event.get("user") or {}).get("id") if isinstance(event.get("user"), dict) else event.get("user")
    if not uid:
        return
    try:
        dm = client.conversations_open(users=[uid])["channel"]["id"]
        client.chat_postMessage(channel=dm, text=render.WELCOME, blocks=[
            {"type": "section", "text": {"type": "mrkdwn", "text": render.WELCOME}},
            {"type": "actions", "elements": [{"type": "button", "action_id": "suggest", "value": q,
                                               "text": {"type": "plain_text", "text": q[:70]}} for q in render.EXAMPLES]}])
    except SlackApiError as e:
        log.warning("welcome DM failed: %s", e)


# --- buttons -------------------------------------------------------------------------------------
@app.action("suggest")
def _suggest(ack, body, client, context):
    ack()
    q = body["actions"][0]["value"]; ch = body["channel"]["id"]; ts = body["message"]["ts"]
    _spawn(answer_in_thread, client, team=context.team_id or "", channel=ch, thread_ts=ts,
           user=body["user"]["id"], question=q, ts_for_token=f"{ts}:{q[:20]}")


@app.action("deeper")
def _deeper(ack, body, client, context):
    ack()
    a = state.answer(body["actions"][0]["value"]) or {}
    ch = body["channel"]["id"]; ts = body["message"].get("thread_ts") or body["message"]["ts"]
    q = a.get("question") or ""
    _spawn(answer_in_thread, client, team=context.team_id or "", channel=ch, thread_ts=ts,
           user=body["user"]["id"], question=q, mode="deep", ts_for_token=f"{body['message']['ts']}:deep")


@app.action("sources")
def _sources(ack, body, client):
    ack()
    a = state.answer(body["actions"][0]["value"]) or {}
    client.chat_postMessage(channel=body["channel"]["id"], thread_ts=body["message"].get("thread_ts") or body["message"]["ts"],
                            text=render.sources_text(a))


@app.action("cut")
def _cut(ack, body, client):
    ack()
    a = state.answer(body["actions"][0]["value"]) or {}
    client.chat_postMessage(channel=body["channel"]["id"], thread_ts=body["message"].get("thread_ts") or body["message"]["ts"],
                            text=render.cut_text(a))


@app.action("open")
def _open(ack):
    ack()


def main():
    for k in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SCOUT_ENGINE_URL", "ASK_SLACK_KEY"):
        if not os.environ.get(k):
            print(f"[slackbot] {k} is not set", file=sys.stderr)
            sys.exit(2)
    who = app.client.auth_test()
    log.info("connected as %s in %s", who.get("user"), who.get("team"))
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()


if __name__ == "__main__":
    main()
