"""Ask Scout engine (WS2 step 4b, 2026-09-28).

    POST /ask            bearer-authenticated; body {question, mode? ("quick" default | "deep"), rid?,
                         slug?, competitor?, my_company?, persona?, history?}; streams Server-Sent
                         Events: {"stage": k, "extra": ...} and {"activity": ...} as the loop moves,
                         then {"done": true, "id", "kind", "html", "cost_usd", "seconds"} (or {"error"}).
                         The request stays open for the whole answer (45-120 s), so Cloud Run never
                         throttles the CPU under it (plan C11); a comment ping every 15 s keeps proxies
                         from closing an idle stream.
    GET  /healthcheck    "ok"
    GET  /ask/dry        a stored answer replayed with fake stages, $0, for the postdeploy probe

Auth (all in code, plan C23-C25): an owner key from ASK_API_KEYS (comma list), or a viewer page
token minted by the viewer with the shared ASK_VIEWER_SECRET ("v1.<exp>.<cid>.<sig>", one hour),
which proves the caller came through a rendered page recently. The daily ceiling is a Ledger in
the private store (ask/state.json): the engine refuses to start a question that could cross it.
Everything else is the viewer's job (cookies, quotas per visitor, rate limit per IP).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import queue
import re
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from scout import ask, askui, calllog, config, ledger

ASK_API_KEYS = [k.strip() for k in os.environ.get("ASK_API_KEYS", "").split(",") if k.strip()]
ASK_VIEWER_SECRET = os.environ.get("ASK_VIEWER_SECRET", "")
# Slack (2026-10-02): the bot on the Mac mini calls /ask with its own key, so its traffic is told
# apart in the records (asked_by "slack") and capped on its own under the engine's daily ceiling.
ASK_SLACK_KEY = os.environ.get("ASK_SLACK_KEY", "").strip()
ASK_SLACK_DAILY_USD = float(os.environ.get("SCOUT_ASK_SLACK_DAILY_USD", "3"))
# Slack's own reservations are sized to what answers actually cost (quick $0.03 to $0.75, deep
# about $1.20), not to the engine's worst-case caps: with $1.50 held per question, a $3 day allowed
# one question in flight and refused the second as "budget spent" (2026-10-02, the first review).
SLACK_RESERVE = {"quick": float(os.environ.get("SCOUT_ASK_SLACK_RESERVE_QUICK", "0.75")),
                 "deep": float(os.environ.get("SCOUT_ASK_SLACK_RESERVE_DEEP", "1.50"))}
ASK_DAILY_CEILING_USD = float(os.environ.get("SCOUT_ASK_DAILY_CEILING_USD", "10"))
ASK_ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("ASK_ALLOWED_ORIGINS", "").split(",") if o.strip()]
LEDGER = ledger.Ledger("ask/state.json", ASK_DAILY_CEILING_USD, config.ASK_MAX_USD)
SLACK_LEDGER = ledger.Ledger("ask/slack_state.json", ASK_SLACK_DAILY_USD, config.ASK_QUICK_MAX_USD)
PING_S = 15

# --- MCP (WS4, 2026-09-29): the same tools as `python -m scout.mcp_server`, over Streamable HTTP at
# /mcp. Reads are free; `ask_scout` spends, so a tools/call for it needs an owner key (Uroš:
# owner-key only). Mounted only with SCOUT_MCP=1; its session manager runs in the app lifespan.
MCP_ENABLED = os.environ.get("SCOUT_MCP", "0") == "1"
if MCP_ENABLED:
    from scout import mcp_server


@asynccontextmanager
async def _lifespan(_app):
    if MCP_ENABLED:
        async with mcp_server.mcp.session_manager.run():
            yield
    else:
        yield


app = FastAPI(title="Ask Scout engine", docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan)
if ASK_ALLOWED_ORIGINS:
    app.add_middleware(CORSMiddleware, allow_origins=ASK_ALLOWED_ORIGINS, allow_methods=["POST", "GET"],
                       allow_headers=["Authorization", "Content-Type"], max_age=600)





# --- auth (page tokens live in scout/asktoken.py, shared with the viewer) -------------------
from scout.asktoken import check_token, page_token  # noqa: E402,F401


def _who(authorization: str | None) -> tuple[str | None, str | None]:
    """(kind, id): ("owner", key-hash) | ("visitor", cid) | (None, None)."""
    tok = (authorization or "").removeprefix("Bearer ").strip()
    if not tok:
        return None, None
    for k in ASK_API_KEYS:
        if hmac.compare_digest(tok, k):
            return "owner", hashlib.sha256(k.encode()).hexdigest()[:8]
    if ASK_SLACK_KEY and hmac.compare_digest(tok, ASK_SLACK_KEY):
        return "slack", "slack"
    if ASK_VIEWER_SECRET:
        cid = check_token(tok, ASK_VIEWER_SECRET)
        if cid:
            return "visitor", cid
    return None, None


# --- routes ----------------------------------------------------------------------------------------------
@app.get("/healthcheck")
def healthcheck():
    return PlainTextResponse("ok")


def _sse(obj: dict) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


@app.post("/ask")
async def ask_route(request: Request, authorization: str | None = Header(default=None)):
    kind, who = _who(authorization)
    if not kind:
        return JSONResponse({"message": "Not authorized."}, status_code=401)
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    question = str((body or {}).get("question") or "").strip()
    if not question or len(question) > 400:
        return JSONResponse({"message": "Ask a question of up to 400 characters."}, status_code=400)
    persona = (body or {}).get("persona")
    persona = persona if persona in ("eng_led", "technical_evaluator", "economic_buyer", "security_regulated", "exec_top_down") else None
    history = [h for h in ((body or {}).get("history") or [])[-6:]
               if isinstance(h, dict) and isinstance(h.get("question"), str) and re.fullmatch(r"a_[0-9a-f]{12}", str(h.get("answer_id") or ""))]
    # the panel's request token decides the answer id up front, so a reader whose stream drops (or
    # who reloads) can fetch the answer from the viewer by id; the same token twice replays the
    # finished record instead of paying for a second run
    rid = str((body or {}).get("rid") or "")
    record_id = ask.record_id_for(rid) if re.fullmatch(r"[A-Za-z0-9_-]{8,64}", rid) else None
    if record_id:
        prior = _stored(record_id)
        if prior:
            return StreamingResponse(_replay(prior), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    # quick (default): no tools, from what Scout already verified, ~30-40 s; deep: today's research
    # loop, minutes. Same verifier on both; the ledger reserves each path's own cap.
    mode = "deep" if (body or {}).get("mode") == "deep" else "quick"
    reserve = config.ASK_MAX_USD if mode == "deep" else config.ASK_QUICK_MAX_USD
    ok, state = LEDGER.start(reserve)
    if not ok:
        return JSONResponse({"message": "Today's question budget is spent. Come back tomorrow, or browse the answers so far.",
                             "room_usd": LEDGER.room(state)}, status_code=429)
    slack_reserved = False
    slack_reserve = SLACK_RESERVE[mode]
    if kind == "slack":                                    # Slack's own cap, inside the engine's ceiling
        ok2, state2 = SLACK_LEDGER.start(slack_reserve)
        if not ok2:
            LEDGER.settle(0.0, reserve)
            room = SLACK_LEDGER.room(state2)
            busy = (state2 or {}).get("in_flight_usd", 0) > 0 and (state2 or {}).get("spend_usd", 0) + slack_reserve <= ASK_SLACK_DAILY_USD
            msg = ("Agent Scout is answering other questions right now. Ask again in a minute." if busy else
                   "Agent Scout's Slack budget for today is spent. Ask again tomorrow, or read the briefs at agent-scout.ai.")
            return JSONResponse({"message": msg, "room_usd": room}, status_code=429)
        slack_reserved = True

    q: queue.Queue = queue.Queue()
    asked_at = datetime.now().isoformat(timespec="seconds")

    def on_stage(k, extra=""):
        if k == "activity":
            if extra:
                q.put({"activity": extra})
        else:
            q.put({"stage": k, "extra": extra})

    def run():
        calllog.begin_run("ask")
        try:
            fn = ask.ask if mode == "deep" else ask.quick_ask
            a = fn(question, slug=(body or {}).get("slug") or None, competitor=(body or {}).get("competitor") or None,
                   my_company=(body or {}).get("my_company") or None, persona=persona, history=history, persist=True,
                   on_stage=on_stage, record_id=record_id)
            a["asked_by"] = kind
            q.put({"done": True, "id": a["id"], "kind": a.get("kind", mode), "html": askui.answer_html(a, show_question=False, chat=True),
                   "cost_usd": a["cost_usd"], "seconds": a["seconds"], "verified": a["verified"],
                   "answer": structured(a)})
            LEDGER.settle(a["cost_usd"], reserve)
            if slack_reserved:
                SLACK_LEDGER.settle(a["cost_usd"], slack_reserve)
        except Exception as e:
            # honest failure: a crashed run is NOT free. generate._drive attaches the cost so far
            # (a known 0.0 when the process died before its first message); an UNKNOWN cost (None)
            # settles the research cap, so the ledger fails closed.
            spent = getattr(e, "scout_cost_usd", None)
            spent = (config.ASK_RESEARCH_BUDGET_USD if mode == "deep" else config.ASK_QUICK_DRAFT_BUDGET_USD) if spent is None else float(spent)
            low = str(e).lower()
            msg = ("Scout ran out of research budget on that question before it could verify an answer" if "budget" in low
                   else "Scout ran out of research steps on that question before it could verify an answer" if "maximum number of turns" in low
                   else "Scout hit a problem answering that")
            text = f"{msg}. Try a narrower question, or one about a single company."
            q.put({"error": text, "cost_usd": round(spent, 2), "id": record_id})
            LEDGER.settle(spent, reserve)
            if slack_reserved:
                SLACK_LEDGER.settle(spent, slack_reserve)
            if record_id:
                ask.persist_failure(record_id, question, text, spent, asked_at, kind=mode)
        finally:
            try:
                calllog.flush_run(True)
            except Exception:
                pass
            q.put(None)

    threading.Thread(target=run, daemon=True).start()

    def events():
        yield _sse({"stage": "facts", "extra": "", "id": record_id, "kind": mode})
        while True:
            try:
                item = q.get(timeout=PING_S)
            except queue.Empty:
                yield ": ping\n\n"
                continue
            if item is None:
                break
            yield _sse(item)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _stored(aid: str) -> dict | None:
    """The persisted answer (or failure) record with this id, newest month first; None if absent."""
    from scout import selfserve
    if not re.fullmatch(r"a_[0-9a-f]{12}", aid or ""):
        return None
    try:
        for month in sorted(selfserve.list_data(ask.ASK_DIR, include_dirs=True) or [], reverse=True):
            if "." in month:
                continue
            raw = selfserve.read_data(f"{ask.ASK_DIR}/{month}/{aid}.json")
            if raw:
                return json.loads(raw)
    except Exception:
        return None
    return None


def structured(a: dict) -> dict:
    """The answer as data (the same shape the MCP tool returns), for clients that render their own
    surface (Slack, 2026-10-02). The panel keeps using `html`."""
    return {"id": a.get("id"), "kind": a.get("kind"), "about": a.get("card") or a.get("competitor"),
            "paragraphs": a.get("paragraphs") or [],
            "sources": [{k: s.get(k) for k in ("n", "url", "class", "tier", "as_of", "excerpt")} for s in (a.get("sources") or [])],
            "cut_log": a.get("cut_log") or [], "unanswered": a.get("unanswered") or [], "verified": a.get("verified"),
            "seconds": a.get("seconds"), "cost_usd": a.get("cost_usd"),
            "permalink": f"https://agent-scout.ai/answers/{a.get('id')}"}


def _replay(rec: dict):
    """A finished record as one SSE frame: $0, no run."""
    if rec.get("failed"):
        yield _sse({"error": rec.get("error") or "Scout could not answer that.", "cost_usd": rec.get("cost_usd", 0), "id": rec["id"], "replay": True})
    else:
        yield _sse({"done": True, "id": rec["id"], "kind": rec.get("kind", "deep"), "html": askui.answer_html(rec, show_question=False, chat=True),
                    "cost_usd": rec.get("cost_usd", 0), "seconds": rec.get("seconds", 0), "verified": rec.get("verified", False), "replay": True,
                    "answer": structured(rec)})


@app.get("/ask/dry")
def ask_dry():
    """A stored answer replayed with fake stages: $0, for the postdeploy probe."""
    rec = _stored(os.environ.get("SCOUT_ASK_CANNED", ""))
    if not rec:
        return JSONResponse({"message": "no stored answer configured"}, status_code=503)

    def events():
        for k in ("facts", "search", "ground", "floor", "verify"):
            yield _sse({"stage": k, "extra": ""})
        yield _sse({"done": True, "id": rec["id"], "html": askui.answer_html(rec, show_question=False, chat=True),
                    "cost_usd": 0.0, "seconds": rec.get("seconds"), "verified": rec.get("verified"), "dry": True})
    return StreamingResponse(events(), media_type="text/event-stream")


if MCP_ENABLED:
    # Mounted LAST at the root so /mcp is served at exactly that path (a mount at "/mcp" would
    # redirect POST /mcp to /mcp/, which MCP clients do not follow) without shadowing the routes
    # above. Auth for the metered tool is inside the tool (scout/mcp_server.py); reads are free.
    app.mount("", mcp_server.mcp.streamable_http_app())
