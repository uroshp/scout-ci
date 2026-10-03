"""The engine client: POST /ask over Server-Sent Events, yielding the frames the panel gets.

`rid` is the request token: a hash of team, channel and message ts, so a Slack retry of the same
event replays the finished answer instead of paying for a second run (the engine maps the token to
the answer id)."""
from __future__ import annotations

import hashlib
import json
import os
from typing import Iterator

import httpx

ENGINE_URL = os.environ.get("SCOUT_ENGINE_URL", "").rstrip("/")
ASK_KEY = os.environ.get("ASK_SLACK_KEY", "")
TIMEOUT_S = float(os.environ.get("SCOUT_SLACK_ASK_TIMEOUT_S", "600"))


def request_token(team: str, channel: str, ts: str, salt: str = "") -> str:
    return hashlib.sha256(f"slack:{team}:{channel}:{ts}:{salt}".encode()).hexdigest()[:40]


def ask(question: str, *, mode: str = "quick", rid: str, history: list | None = None,
        persona: str | None = None, client: httpx.Client | None = None) -> Iterator[dict]:
    """Yields each SSE frame as a dict: {"stage"}, {"activity"}, {"done", "answer", ...} or {"error"}.
    A non-200 reply yields one {"error": message, "status": code} frame."""
    payload = {"question": question, "mode": "deep" if mode == "deep" else "quick", "rid": rid,
               "history": [{"question": h["question"], "answer_id": h["answer_id"]} for h in (history or [])[-6:]]}
    if persona:
        payload["persona"] = persona
    headers = {"Authorization": f"Bearer {ASK_KEY}", "Content-Type": "application/json", "Accept": "text/event-stream"}
    own = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(TIMEOUT_S, connect=20.0))
    try:
        with client.stream("POST", f"{ENGINE_URL}/ask", json=payload, headers=headers) as r:
            if r.status_code != 200:
                body = b"".join(r.iter_bytes())
                try:
                    msg = json.loads(body).get("message") or body.decode("utf-8", "replace")
                except Exception:
                    msg = body.decode("utf-8", "replace") or f"engine replied {r.status_code}"
                yield {"error": msg, "status": r.status_code}
                return
            for line in r.iter_lines():
                if not line or not line.startswith("data: "):
                    continue
                try:
                    yield json.loads(line[6:])
                except Exception:
                    continue
    finally:
        if own:
            client.close()
