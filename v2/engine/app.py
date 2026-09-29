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
from datetime import datetime

from fastapi import FastAPI, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from scout import ask, askui, calllog, config, ledger

ASK_API_KEYS = [k.strip() for k in os.environ.get("ASK_API_KEYS", "").split(",") if k.strip()]
ASK_VIEWER_SECRET = os.environ.get("ASK_VIEWER_SECRET", "")
ASK_DAILY_CEILING_USD = float(os.environ.get("SCOUT_ASK_DAILY_CEILING_USD", "10"))
ASK_ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("ASK_ALLOWED_ORIGINS", "").split(",") if o.strip()]
LEDGER = ledger.Ledger("ask/state.json", ASK_DAILY_CEILING_USD, config.ASK_MAX_USD)
PING_S = 15

app = FastAPI(title="Ask Scout engine", docs_url=None, redoc_url=None, openapi_url=None)
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
                   "cost_usd": a["cost_usd"], "seconds": a["seconds"], "verified": a["verified"]})
            LEDGER.settle(a["cost_usd"], reserve)
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


def _replay(rec: dict):
    """A finished record as one SSE frame: $0, no run."""
    if rec.get("failed"):
        yield _sse({"error": rec.get("error") or "Scout could not answer that.", "cost_usd": rec.get("cost_usd", 0), "id": rec["id"], "replay": True})
    else:
        yield _sse({"done": True, "id": rec["id"], "kind": rec.get("kind", "deep"), "html": askui.answer_html(rec, show_question=False, chat=True),
                    "cost_usd": rec.get("cost_usd", 0), "seconds": rec.get("seconds", 0), "verified": rec.get("verified", False), "replay": True})


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
