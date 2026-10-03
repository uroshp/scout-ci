"""Thread history and per-user daily counts, in one JSON file with atomic writes."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
PATH = os.path.expanduser(os.environ.get("SCOUT_SLACK_STATE", "~/scout-slack/state.json"))
QUICK_PER_USER = int(os.environ.get("SCOUT_SLACK_QUICK_PER_USER", "10"))
DEEP_PER_USER = int(os.environ.get("SCOUT_SLACK_DEEP_PER_USER", "1"))
_LOCK = threading.Lock()


def _load() -> dict:
    try:
        with open(PATH) as f:
            return json.load(f)
    except Exception:
        return {"threads": {}, "usage": {}, "answers": {}}


def _save(d: dict) -> None:
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(PATH), prefix=".state-")
    with os.fdopen(fd, "w") as f:
        json.dump(d, f)
    os.replace(tmp, PATH)


def today() -> str:
    return datetime.now(PT).date().isoformat()


def history(thread_key: str) -> list:
    return list(_load()["threads"].get(thread_key) or [])


def remember(thread_key: str, question: str, answer: dict) -> None:
    with _LOCK:
        d = _load()
        d["threads"].setdefault(thread_key, []).append({"question": question, "answer_id": answer.get("id")})
        d["threads"][thread_key] = d["threads"][thread_key][-12:]
        d["answers"][answer.get("id") or ""] = answer
        if len(d["answers"]) > 400:                       # keep the file small: the engine keeps the records
            for k in list(d["answers"])[:-400]:
                d["answers"].pop(k, None)
        _save(d)


def answer(aid: str) -> dict | None:
    return _load()["answers"].get(aid)


def allow(user: str, kind: str) -> tuple[bool, int]:
    """(allowed, limit). Counts the question when allowed."""
    cap = DEEP_PER_USER if kind == "deep" else QUICK_PER_USER
    with _LOCK:
        d = _load()
        day = d["usage"].setdefault(today(), {})
        u = day.setdefault(user, {"quick": 0, "deep": 0})
        if u[kind] >= cap:
            return False, cap
        u[kind] += 1
        for k in [k for k in d["usage"] if k != today()]:  # one day of history is enough
            d["usage"].pop(k, None)
        _save(d)
    return True, cap
