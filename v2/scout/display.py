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
from datetime import datetime, timedelta, timezone

from scout import config, store

# Display-only Eastern conversion — storage stays naive-UTC (scout.monitor's due-gate
# compares stored timestamps against the UTC runner clock; see page.py for the same note).
try:
    from zoneinfo import ZoneInfo
    _ET_TZ = ZoneInfo("America/New_York")
except Exception:                       # no tzdata on host — fixed EST beats crashing the viewer
    _ET_TZ = timezone(timedelta(hours=-5), "ET")

# How recently a monitor run must have touched a claim for the "NEW" badge (A4).
NEW_BADGE_WINDOW_HOURS = 24


def _git(args: list[str]) -> str:
    try:
        # TZ pinned so --date=format-local renders Eastern wherever the server runs
        # (Streamlit Cloud and the Actions runners are both UTC).
        return subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=10,
            env={**os.environ, "TZ": "America/New_York"},
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
def _next_anchor_after(dt: datetime) -> datetime | None:
    """Earliest daily monitoring anchor (config.MONITOR_ANCHORS_UTC = 7am + 1pm ET, wall-clock
    UTC) strictly after `dt`, or None if anchors are disabled. Mirrors the engine's
    window-anchored due-gate (monitor._is_due) so the viewer's 'next check' shows when the card
    will ACTUALLY be re-checked — not a relative cadence guess that drifts off the real schedule."""
    anchors = []
    for a in config.MONITOR_ANCHORS_UTC:
        h, m = a.split(":")
        anchors.append((int(h), int(m)))
    if not anchors:
        return None
    anchors.sort()
    for day_offset in (0, 1):                      # today's anchors, then tomorrow's
        base = dt + timedelta(days=day_offset)
        for h, m in anchors:
            cand = base.replace(hour=h, minute=m, second=0, microsecond=0)
            if cand > dt:
                return cand
    return None


def checkpoints(meta: dict) -> dict:
    """Last-checked / next-check. next_check is the next monitoring window anchor after
    last_checked (7am + 1pm ET), matching the engine's window-anchored due-gate, emitted as a
    full ISO datetime so the viewer renders a live ticking countdown (A2). Unmonitored cards get
    no next_check (they are never re-checked). Legacy fallback when anchors are disabled:
    last_checked + cadence_hours."""
    cadence_hours = meta.get("cadence_hours") or config.DEFAULT_CADENCE_HOURS
    last_raw = meta.get("last_checked") or meta.get("baseline_date")
    last_dt = _parse_ts(last_raw)
    next_iso = None
    if last_dt is not None and meta.get("monitored") is not False:
        nxt = _next_anchor_after(last_dt)          # anchored schedule (current model)
        if nxt is None:                            # anchors disabled → legacy relative cadence
            nxt = last_dt + timedelta(hours=cadence_hours)
        next_iso = nxt.isoformat(timespec="seconds")
    return {
        "baseline_date": meta.get("baseline_date"),
        "last_checked": last_raw,                 # raw (date or datetime) as stored
        "last_checked_ts": last_dt.isoformat(timespec="seconds") if last_dt else None,
        "next_check": next_iso,                   # ISO datetime; powers the countdown
        "cadence_hours": cadence_hours,
    }


# --- 2. per-card change feed (genuine battlecard updates only) ----------------
# The automation authors its commits under these identities, so we can tell an agent-driven
# battlecard update from an incidental product/code commit that merely grazed the files.
_AGENT_AUTHORS = {
    "scout-monitor@users.noreply.github.com",
    "scout-selfserve@users.noreply.github.com",
}


def change_feed(slug: str, limit: int = 25) -> list[dict]:
    """The card's UPDATE history — not every commit that touched the folder. We scope to the
    CONTENT files (current.md / claims.json), which already excludes monitor 'heartbeat'
    commits (those touch only meta.json); then we drop product/code commits (human-authored
    and not the baseline) and relabel the rest to clean, user-facing text. The oldest content
    commit is the card's creation. Local datetime so frequent updates read as recent (A3).
    --follow tracks the file across renames (e.g. the v1/v2 repo restructure) so the lineage
    and the true 'created' date survive a move; it needs a SINGLE path, so we follow
    current.md, which every material update and the baseline both touch."""
    path = store.battlecard_dir(slug)
    out = _git(["log", f"-{limit}", "--follow", "--date=format-local:%b %-d, %-I:%M %p ET",
                "--format=%h%x09%ad%x09%at%x09%ae%x09%s", "--",
                os.path.join(path, "current.md")])
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 5:
            rows.append({"hash": parts[0], "date": parts[1], "epoch": parts[2],
                         "email": parts[3], "subject": parts[4]})
    if not rows:
        return []
    baseline_hash = rows[-1]["hash"]                 # oldest content commit = card creation
    events = []
    for r in rows:
        subj = r["subject"]
        if r["hash"] == baseline_hash:
            label = "Battlecard created"
        elif subj.startswith("monitor:"):
            label = subj.split(":", 1)[1].strip().capitalize() or "Battlecard updated"
        elif subj.startswith("selfserve:"):
            label = "Battlecard regenerated"
        elif r["email"] in _AGENT_AUTHORS:
            label = subj
        else:
            continue                                 # product/code commit — not a card update
        events.append({"hash": r["hash"], "date": r["date"], "epoch": r["epoch"],
                       "subject": label})
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
    if last_dt and len(str(raw).strip()) > 10:      # full timestamp (naive UTC) -> Eastern
        last = (last_dt.replace(tzinfo=timezone.utc).astimezone(_ET_TZ)
                .strftime("%b %-d, %-I:%M %p ET"))
    elif last_dt:                                   # date-only (baseline) — no time to convert
        last = last_dt.strftime("%b %-d, %Y")
    else:
        last = raw or "—"
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
