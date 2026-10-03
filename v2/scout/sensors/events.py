"""Findings as durable events (the signals pattern): `sensors/<entity>/events.jsonl`, one JSON line
per finding, deduped on fingerprint, consumed PER CARD (`consumed_by: {slug: run_ts}`) only by a
written check. A screen that fails, a dispatched run, or a second card on the same entity all see
what they should. Lines older than KEEP_DAYS are pruned on append.

Finding: {"fingerprint", "entity", "kind": page_change|value_change|redesign|feed_item|news,
          "source_url" (publisher URL or the page), "source_host", "source_class", "title",
          "published", "text" (new block(s), the summary or the snippet, capped), "page_kind",
          "flag", "seen_at", "consumed_by": {}}
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

from scout import selfserve

EVENTS = "sensors/{entity}/events.jsonl"
KEEP_DAYS = 30
OPEN_DAYS = 3
MAX_LINES = 3000
TEXT_CAP = 1200


def fingerprint(*parts) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:16]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def make(entity: str, kind: str, source_url: str, title: str, text: str, *, published: str | None = None,
         source_host: str | None = None, source_class: str | None = None, page_kind: str | None = None,
         flag: str | None = None, key: str | None = None) -> dict:
    from scout.sensors import feeds
    text = (text or "").strip()[:TEXT_CAP]
    return {
        "fingerprint": fingerprint(entity, kind, key or source_url, title or text[:120]),
        "entity": entity, "kind": kind, "source_url": source_url,
        "source_host": source_host or feeds.host_of(source_url), "source_class": source_class,
        "title": (title or "")[:300], "published": published, "text": text, "page_kind": page_kind,
        "flag": flag, "seen_at": _now(), "consumed_by": {},
    }


def _lines(entity: str) -> list[dict]:
    try:
        raw = selfserve.read_data(EVENTS.format(entity=entity)) or ""
    except Exception:
        return []
    out = []
    for l in raw.splitlines():
        try:
            out.append(json.loads(l))
        except Exception:
            continue
    return out


def all_events(entity: str) -> list[dict]:
    return _lines(entity)


def append(entity: str, findings: list[dict]) -> int:
    """Append new findings (dedupe on fingerprint), prune old lines. Returns how many were new."""
    if not findings:
        return 0
    added = {"n": 0}
    cutoff = (datetime.now() - timedelta(days=KEEP_DAYS)).isoformat(timespec="seconds")

    def tx(cur):
        rows = []
        for l in (cur or "").splitlines():
            if not l.strip():
                continue
            try:
                e = json.loads(l)
            except Exception:
                continue
            if str(e.get("seen_at") or "") >= cutoff:
                rows.append(e)
        have = {e.get("fingerprint") for e in rows}
        for f in findings:
            if f.get("fingerprint") and f["fingerprint"] not in have:
                rows.append(f)
                have.add(f["fingerprint"])
                added["n"] += 1
        rows = rows[-MAX_LINES:]
        return "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + ("\n" if rows else "")
    selfserve.update_data(EVENTS.format(entity=entity), tx, f"sensors: {len(findings)} finding(s) {entity}")
    return added["n"]


def open_for(entity: str, slug: str, days: int = OPEN_DAYS, now: datetime | None = None) -> list[dict]:
    """Findings this card has not consumed, newest window first (oldest first within it)."""
    now = now or datetime.now()
    cutoff = (now - timedelta(days=days)).isoformat(timespec="seconds")
    return [e for e in _lines(entity)
            if str(e.get("seen_at") or "") >= cutoff and slug not in (e.get("consumed_by") or {})]


def recent(entity: str, days: int = OPEN_DAYS, now: datetime | None = None) -> list[dict]:
    """Every finding in the window, consumed or not (the compare step's view)."""
    now = now or datetime.now()
    cutoff = (now - timedelta(days=days)).isoformat(timespec="seconds")
    return [e for e in _lines(entity) if str(e.get("seen_at") or "") >= cutoff]


def consume(entity: str, slug: str, run_ts: str, fingerprints: list[str]) -> int:
    want = set(fingerprints or [])
    if not want:
        return 0
    n = {"v": 0}

    def tx(cur):
        rows = []
        for l in (cur or "").splitlines():
            if not l.strip():
                continue
            try:
                e = json.loads(l)
            except Exception:
                continue
            if e.get("fingerprint") in want and slug not in (e.get("consumed_by") or {}):
                e.setdefault("consumed_by", {})[slug] = run_ts
                n["v"] += 1
            rows.append(e)
        return "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + ("\n" if rows else "")
    try:
        selfserve.update_data(EVENTS.format(entity=entity), tx, f"sensors: {slug} consumed {len(want)} finding(s) of {entity}")
    except Exception:
        return 0
    return n["v"]
