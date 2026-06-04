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
from datetime import datetime, timedelta

from scout import config, store

# How recently a monitor run must have touched a claim for the "NEW" badge (A4).
NEW_BADGE_WINDOW_HOURS = 24


def _git(args: list[str]) -> str:
    try:
        return subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=10
        ).stdout
    except Exception:
        return ""


def _parse_ts(s: str | None) -> datetime | None:
    """Parse a meta/alert timestamp that may be a date ('2026-06-04') or a full
    ISO datetime ('2026-06-04T17:10:12'). Returns None on anything unparseable."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


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
    """Last-checked / next-check, cadence-aware. `cadence_hours` lives in meta
    (per-competitor, A1); next_check is last_checked + cadence, emitted as a full
    ISO datetime so the viewer can render a live ticking countdown (A2)."""
    cadence_hours = meta.get("cadence_hours") or config.DEFAULT_CADENCE_HOURS
    last_raw = meta.get("last_checked") or meta.get("baseline_date")
    last_dt = _parse_ts(last_raw)
    next_iso = None
    if last_dt is not None:
        next_iso = (last_dt + timedelta(hours=cadence_hours)).isoformat(timespec="seconds")
    return {
        "baseline_date": meta.get("baseline_date"),
        "last_checked": last_raw,                 # raw (date or datetime) as stored
        "last_checked_ts": last_dt.isoformat(timespec="seconds") if last_dt else None,
        "next_check": next_iso,                   # ISO datetime; powers the countdown
        "cadence_hours": cadence_hours,
    }


# --- 2. per-card change feed (git history is the heartbeat) -------------------
def change_feed(slug: str, limit: int = 25) -> list[dict]:
    # Local datetime (not just date) so frequent updates read as genuinely recent (A3).
    path = store.battlecard_dir(slug)
    out = _git(["log", f"-{limit}", "--date=format-local:%Y-%m-%d %H:%M",
                "--format=%h%x09%ad%x09%s", "--", path])
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
    raw = meta.get("last_checked") or meta.get("baseline_date")
    last_dt = _parse_ts(raw)
    last = last_dt.strftime("%Y-%m-%d %H:%M") if last_dt else (raw or "—")
    n, a = len(claims), len(alerts)
    # Frame as active monitoring, never "nothing's happening". A quiet window reads
    # as "all current", and real movement is surfaced by the "Just updated" panel.
    if a:
        line = f"Monitoring {n} verified claims · {a} material change(s) flagged · last checked {last}."
    else:
        line = f"Monitoring {n} verified claims, all current · last checked {last}."
    return {"line": line, "claims_tracked": n, "alerts_total": a, "last_checked": last}


# --- 4. timestamps on every claim + the "NEW" badge (A4) ---------------------
def recent_updates(slug: str, within_hours: int = NEW_BADGE_WINDOW_HOURS,
                   now: datetime | None = None) -> list[dict]:
    """Alerts whose detected timestamp is within the window — i.e. the claims a
    monitor run actually ADDED or CHANGED recently. The badge is keyed off a
    monitor ACTION (an emitted alert), never off claim age, so a freshly
    generated baseline (which has no alerts) badges nothing."""
    now = now or datetime.now()
    cutoff = now - timedelta(hours=within_hours)
    out = []
    for a in load_alerts(slug):
        ts = _parse_ts(a.get("detected_at") or a.get("date"))
        if ts is not None and ts >= cutoff:
            out.append(a)
    return out


def recent_update_keys(slug: str, within_hours: int = NEW_BADGE_WINDOW_HOURS,
                       now: datetime | None = None) -> set:
    return {a.get("subject_key")
            for a in recent_updates(slug, within_hours, now) if a.get("subject_key")}


def claim_timestamps(claims: list[dict], recent_keys: set | None = None) -> list[dict]:
    recent_keys = recent_keys or set()
    rows = []
    for c in claims:
        grounding = c.get("grounding") or {}
        rows.append({
            "subject_key": c.get("subject_key"),
            "section": c.get("section"),
            "as_of": c.get("as_of"),                  # fact is true as-of this date
            "verified_on": grounding.get("fetched_at"),  # grounding last confirmed it on the page
            "is_new": c.get("subject_key") in recent_keys,  # touched by a monitor run <24h ago
        })
    return rows


# --- aggregate: the full data shape the UI renders for one card --------------
def card_status(slug: str) -> dict:
    meta = store.load_meta(slug) or {}
    claims = store.load_claims(slug)
    recent = recent_updates(slug)
    recent_keys = {a.get("subject_key") for a in recent if a.get("subject_key")}
    return {
        "slug": slug,
        "meta": meta,
        "checkpoints": checkpoints(meta),
        "change_feed": change_feed(slug),
        "agent_activity": agent_activity(slug, meta, claims),
        "claim_timestamps": claim_timestamps(claims, recent_keys),
        "recent_updates": recent,        # powers the "Just updated" sidebar + NEW badges
        "recent_keys": sorted(recent_keys),
    }
