"""Display layer for living battlecards — the 4 "show the agentic work" elements.

READ-ONLY. Reads the git-committed store + git history. Does NOT run monitoring.

Built against the DATA SHAPE the monitoring engine will produce (v2-agent-spec §6-9),
so it works now on baseline-only data and gets richer once monitoring writes alerts:
  meta.json        : baseline_date, last_checked, alerted_fingerprints  (next_check derived)
  alerts.jsonl     : one material-change record per line (written by monitoring; may be absent)
  claims[].as_of   : the date each fact is true as-of
  claims[].grounding.fetched_at : when grounding last confirmed the fact on its page
  git history of battlecards/<slug>/ : the change heartbeat

The four elements:
  1. checkpoints      -> last-checked / next-check
  2. change_feed      -> per-card change feed from git history
  3. agent_activity   -> the agent-activity line
  4. claim_timestamps -> timestamps on every claim
"""
import json
import os
import subprocess
from datetime import date, timedelta

from scout import store

# Display-side assumption until the monitor sets a real cadence (no store-schema change).
MONITOR_CADENCE_DAYS = 1


def _git(args: list[str]) -> str:
    try:
        return subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=10
        ).stdout
    except Exception:
        return ""


def list_battlecards() -> list[str]:
    """Slugs of all committed battlecards (folders containing a meta.json)."""
    root = store.STORE_ROOT
    if not os.path.isdir(root):
        return []
    return sorted(
        d for d in os.listdir(root)
        if os.path.exists(os.path.join(root, d, "meta.json"))
    )


# --- 1. last-checked / next-check --------------------------------------------
def checkpoints(meta: dict) -> dict:
    last = meta.get("last_checked") or meta.get("baseline_date")
    nxt = None
    if last:
        try:
            nxt = (date.fromisoformat(last) + timedelta(days=MONITOR_CADENCE_DAYS)).isoformat()
        except ValueError:
            pass
    return {
        "baseline_date": meta.get("baseline_date"),
        "last_checked": last,
        "next_check": nxt,                 # derived; the monitor will own this later
        "cadence_days": MONITOR_CADENCE_DAYS,
    }


# --- 2. per-card change feed (git history is the heartbeat) -------------------
def change_feed(slug: str, limit: int = 25) -> list[dict]:
    path = store.battlecard_dir(slug)
    out = _git(["log", f"-{limit}", "--date=short", "--format=%h%x09%ad%x09%s", "--", path])
    events = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            events.append({"hash": parts[0], "date": parts[1], "subject": parts[2]})
    return events


# --- 3. agent-activity line --------------------------------------------------
def load_alerts(slug: str) -> list[dict]:
    path = os.path.join(store.battlecard_dir(slug), "alerts.jsonl")
    if not os.path.exists(path):
        return []
    alerts = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    alerts.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return alerts


def agent_activity(slug: str, meta: dict | None = None, claims: list | None = None) -> dict:
    meta = meta if meta is not None else (store.load_meta(slug) or {})
    claims = claims if claims is not None else store.load_claims(slug)
    alerts = load_alerts(slug)
    last = meta.get("last_checked") or meta.get("baseline_date") or "unknown"
    n, a = len(claims), len(alerts)
    if a:
        line = f"Agent last checked {last} — tracking {n} verified claims; {a} material change(s) logged."
    else:
        line = f"Agent last checked {last} — tracking {n} verified claims; no material changes yet."
    return {"line": line, "claims_tracked": n, "alerts_total": a, "last_checked": last}


# --- 4. timestamps on every claim --------------------------------------------
def claim_timestamps(claims: list[dict]) -> list[dict]:
    rows = []
    for c in claims:
        grounding = c.get("grounding") or {}
        rows.append({
            "subject_key": c.get("subject_key"),
            "section": c.get("section"),
            "as_of": c.get("as_of"),                  # fact is true as-of this date
            "verified_on": grounding.get("fetched_at"),  # grounding last confirmed it on the page
        })
    return rows


# --- aggregate: the full data shape the UI renders for one card --------------
def card_status(slug: str) -> dict:
    meta = store.load_meta(slug) or {}
    claims = store.load_claims(slug)
    return {
        "slug": slug,
        "meta": meta,
        "checkpoints": checkpoints(meta),
        "change_feed": change_feed(slug),
        "agent_activity": agent_activity(slug, meta, claims),
        "claim_timestamps": claim_timestamps(claims),
    }
