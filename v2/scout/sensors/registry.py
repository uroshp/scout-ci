"""Entities and their source registries (private store, `sensors/<entity>/registry.json`).

Entity identity is the card's DISPLAY name, slugified: "Slack", "Google Cloud" and "Microsoft
Teams" are their own entities (the SEC registry maps Slack to Salesforce and Teams to Microsoft,
right for a CIK and wrong for a news query). One entity serves every card that names it.

Registry shape:
  {"entity", "name", "names": [aliases], "cards": [slugs],
   "sources": [{"url", "kind", "feed": bool, "added_by", "added_on", "cadence": "daily|weekly|off",
                "noise": int, "last_ok": iso|None, "last_status": str|None}],
   "news_queries": [str], "seeded_at": iso, "notes": [str]}
Kinds: newsroom, blog, press, pricing, docs, releases, status, careers, ir, cited, feed.
"""
from __future__ import annotations

import json
from datetime import datetime

from scout import selfserve, store

REGISTRY = "sensors/{entity}/registry.json"
STATE = "sensors/{entity}/state.json"
KINDS = ("newsroom", "blog", "press", "pricing", "docs", "releases", "status", "careers", "ir", "cited", "feed")


def entity_key(name) -> str:
    """'Google Cloud' -> 'google-cloud'; the card's display name decides, never a parent company."""
    return store._slug_part(str(name or "")) or "unknown"


def entities_for(meta: dict) -> list[dict]:
    """The one or two entities a card watches: [{key, name, role}] with role competitor|my_company."""
    out = []
    comp = (meta or {}).get("competitor")
    me = (meta or {}).get("my_company")
    if comp:
        out.append({"key": entity_key(comp), "name": str(comp), "role": "competitor"})
    if me and entity_key(me) != (out[0]["key"] if out else None):
        out.append({"key": entity_key(me), "name": str(me), "role": "my_company"})
    return out


def load(entity: str) -> dict | None:
    try:
        raw = selfserve.read_data(REGISTRY.format(entity=entity))
        return json.loads(raw) if raw else None
    except Exception:
        return None


def save(entity: str, reg: dict, message: str | None = None) -> None:
    reg = dict(reg)
    reg.setdefault("entity", entity)
    reg["updated_at"] = datetime.now().isoformat(timespec="seconds")
    selfserve.write_data(REGISTRY.format(entity=entity), json.dumps(reg, indent=1, ensure_ascii=False),
                         message or f"sensors: registry {entity}")


def load_state(entity: str) -> dict:
    try:
        raw = selfserve.read_data(STATE.format(entity=entity))
        s = json.loads(raw) if raw else {}
    except Exception:
        s = {}
    s.setdefault("pages", {})          # url -> {hashes, shapes, etag, last_modified, fetched_at, status, blocks}
    s.setdefault("seen", {})           # feed/news item key -> first seen date
    return s


def save_state(entity: str, state: dict) -> None:
    selfserve.write_data(STATE.format(entity=entity), json.dumps(state, ensure_ascii=False, separators=(",", ":")),
                         f"sensors: state {entity}")


def active_sources(reg: dict, today: str) -> list[dict]:
    """Sources due today: daily ones always; weekly ones on the entity's weekday; `off` never."""
    out = []
    for s in (reg or {}).get("sources") or []:
        cad = s.get("cadence") or "daily"
        if cad == "off" or not s.get("url"):
            continue
        if cad == "weekly":
            import hashlib
            from datetime import date
            wd = int(hashlib.sha256(str(s["url"]).encode()).hexdigest(), 16) % 6   # Mon..Sat
            if date.fromisoformat(today).weekday() != wd:
                continue
        out.append(s)
    return out
