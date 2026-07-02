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
import time
from datetime import datetime, timedelta, timezone

import httpx

from scout import config, schema, store

# Display-only Eastern conversion — storage stays naive-UTC (scout.monitor's due-gate
# compares stored timestamps against the UTC runner clock; see page.py for the same note).
try:
    from zoneinfo import ZoneInfo
    _ET_TZ = ZoneInfo("America/New_York")
except Exception:                       # no tzdata on host — fixed EST beats crashing the viewer
    _ET_TZ = timezone(timedelta(hours=-5), "ET")

# How recently a monitor run must have touched a claim for the "NEW" badge (A4) — also drives
# the "Just updated" rail panel and every "<Nh" label in the viewer (page.py derives them all
# from this constant). 48h so a once-a-day reader still catches yesterday afternoon's update.
NEW_BADGE_WINDOW_HOURS = 48


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
def _next_anchor_after(dt: datetime, cadence_days: int = 1) -> datetime | None:
    """Earliest monitoring anchor (config.MONITOR_ANCHORS_UTC, wall-clock UTC) STRICTLY after `dt`
    on which the card will ACTUALLY be re-checked, mirroring the engine's due-gate (monitor._is_due)
    so the viewer's countdown matches reality and never drifts. Skips MONITOR_SKIP_WEEKDAYS (the
    weekend), and for a slower per-card cadence (cadence_days>1, e.g. Batman weekly) requires enough
    whole days since the last check. None if anchors are disabled."""
    anchors = []
    for a in config.MONITOR_ANCHORS_UTC:
        h, m = a.split(":")
        anchors.append((int(h), int(m)))
    if not anchors:
        return None
    anchors.sort()
    for day_offset in range(0, 9 + cadence_days):   # clear the cadence gap + a skipped weekend
        base = dt + timedelta(days=day_offset)
        for h, m in anchors:
            cand = base.replace(hour=h, minute=m, second=0, microsecond=0)
            if cand <= dt:
                continue
            if cand.weekday() in config.MONITOR_SKIP_WEEKDAYS:
                continue
            if cadence_days > 1 and (cand.date() - dt.date()).days < cadence_days:
                continue
            return cand
    return None


def checkpoints(meta: dict) -> dict:
    """Last-checked / next-check. next_check is the next monitoring window anchor after
    last_checked (7am + 1pm ET), matching the engine's window-anchored due-gate, emitted as a
    full ISO datetime so the viewer renders a live ticking countdown (A2). Unmonitored cards get
    no next_check (they are never re-checked). Legacy fallback when anchors are disabled:
    last_checked + cadence_hours."""
    cadence_hours = meta.get("cadence_hours") or config.DEFAULT_CADENCE_HOURS
    cadence_days = meta.get("cadence_days") or 1
    last_raw = meta.get("last_checked") or meta.get("baseline_date")
    last_dt = _parse_ts(last_raw)
    next_iso = None
    if last_dt is not None and meta.get("monitored") is not False:
        nxt = _next_anchor_after(last_dt, cadence_days)   # anchored schedule, per-card cadence + weekend skip
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


_CF_API_CACHE: dict = {}      # repo_path -> (expires_epoch, rows)
_CF_API_TTL = 600             # 10 min; a card's commit history only changes on a deploy


def _commits_via_api(repo_path: str, limit: int) -> list[dict]:
    """Fallback for change_feed when local git history is unavailable — notably the Cloud Run
    image, which ships no .git. Reads the public repo's commit log for the file via the GitHub
    API, mapped to the same row shape _git produces. Best-effort + cached; returns [] on any
    failure so the feed degrades silently rather than crashing the viewer. Caveat: the API does
    not --follow renames, so history before the v1->v2 move may be omitted (recent updates — the
    part that matters — are intact)."""
    now = time.time()
    cached = _CF_API_CACHE.get(repo_path)
    if cached and cached[0] > now:
        return cached[1]
    rows: list[dict] = []
    try:
        owner_repo = config.SOURCE_REPO_URL.rstrip("/").split("github.com/")[-1]
        headers = {"Accept": "application/vnd.github+json"}
        if config.SELFSERVE_GH_TOKEN:
            headers["Authorization"] = f"Bearer {config.SELFSERVE_GH_TOKEN}"
        resp = httpx.get(f"https://api.github.com/repos/{owner_repo}/commits",
                         params={"path": repo_path, "per_page": limit},
                         headers=headers, timeout=10)
        if resp.status_code == 200:
            for c in resp.json():
                a = c["commit"]["author"]
                dt = datetime.fromisoformat(a["date"].replace("Z", "+00:00"))
                rows.append({
                    "hash": c["sha"][:9],
                    "date": dt.astimezone(_ET_TZ).strftime("%b %-d, %-I:%M %p ET"),
                    "epoch": str(int(dt.timestamp())),
                    "email": a.get("email") or "",
                    "subject": (c["commit"]["message"] or "").splitlines()[0],
                })
    except Exception:
        rows = []
    _CF_API_CACHE[repo_path] = (now + _CF_API_TTL, rows)
    return rows


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
        # No local git history (the Cloud Run image ships no .git) — read it from the GitHub API.
        rows = _commits_via_api(f"v2/battlecards/{slug}/current.md", limit)
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
            label = label.replace(" — ", ": ")   # historical commit subjects carry em dashes
        elif subj.startswith("selfserve:"):
            label = "Battlecard regenerated"
        elif subj.startswith("content(card):"):       # an approved propagation edit to this card
            label = subj.split(":", 1)[1].strip().capitalize().replace(" — ", ": ")
        elif subj.startswith("content") and ":" in subj:  # human-approved content commits
            label = subj.split(":", 1)[1].strip().capitalize().replace(" — ", ": ")
        elif r["email"] in _AGENT_AUTHORS:
            label = subj
        else:
            continue                                 # product/code commit — not a card update
        events.append({"hash": r["hash"], "date": r["date"], "epoch": r["epoch"],
                       "subject": label})
    return events


def _iso_ts(s):
    try:
        return datetime.fromisoformat(s) if s else None
    except (ValueError, TypeError):
        return None


def last_update_ts(slug: str) -> datetime:
    """When a card's CONTENT last changed: the most recent of (a) any material-change/feed alert,
    (b) any claim's updated_on (an approved propagation edit — 2026-07-02: these were invisible to
    ordering), (c) the baseline date. Deliberately NOT last_checked (uniform refresh cadence can't
    tell which card changed). Canonical implementation — server.py uses it directly; app_v2.py
    keeps an identical INLINE copy on purpose (its docstring: new cross-module attributes can hit
    Streamlit Cloud's stale-module cache; keep the two in sync)."""
    times = [t for a in load_alerts(slug)
             if (t := _iso_ts(a.get("detected_at") or a.get("date")))]
    times += [t for c in store.load_claims(slug) if (t := _iso_ts(c.get("updated_on")))]
    base = _iso_ts((store.load_meta(slug) or {}).get("baseline_date"))
    if base:
        times.append(base)
    return max(times) if times else datetime.min


def ordered_cards(pinned_slug: str | None = None, pinned_position: int = 3) -> list:
    """Dropdown order: most-recently-UPDATED card first (content-change time, not refresh), with an
    optional showcase card pinned to a fixed slot regardless of its age."""
    slugs = list_battlecards()
    rest = sorted((s for s in slugs if s != pinned_slug), key=last_update_ts, reverse=True)
    if pinned_slug and pinned_slug in slugs:
        rest.insert(min(pinned_position, len(rest)), pinned_slug)
    return rest


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


CHANGELOG_WINDOW_DAYS = 14   # how far back the "Recently updated" changelog reaches


def recently_updated_keys(claims, within_hours=NEW_BADGE_WINDOW_HOURS, now=None):
    """subject_keys of claims whose updated_on (an approved propagation edit landed) is within the
    badge window. Complements the alert-based keys so APPROVED card edits also badge, not just
    monitor-detected competitor news."""
    now = now or datetime.now()
    cutoff = now - timedelta(hours=within_hours)
    out = set()
    for c in claims:
        u = _parse_ts(c.get("updated_on"))
        if u is not None and u >= cutoff and c.get("subject_key"):
            out.add(c.get("subject_key"))
    return out


def _claim_label(text):
    """A short human label for a claim: its bold title if it has one, else the first line."""
    t = (text or "").strip()
    if "**" in t:
        seg = t.split("**", 2)
        if len(seg) >= 3 and seg[1].strip():
            return seg[1].strip().strip('"')
    return (t.splitlines()[0][:70] if t else "")


def recently_updated_claims(claims, within_days=CHANGELOG_WINDOW_DAYS, now=None):
    """The claim-level changelog: active claims edited within the window, newest first, each with
    what the viewer needs to deep-link to it. Powers the 'Recently updated' panel."""
    now = now or datetime.now()
    cutoff = now - timedelta(days=within_days)
    rows = []
    for c in claims:
        if str(c.get("status", "active")) != "active":
            continue
        # only RENDERED sections — the tracked_facts anchor (our own my_company news) must never
        # surface in the competitor-facing changelog, even when stamped with updated_on.
        if c.get("section") not in schema.SECTIONS:
            continue
        u = _parse_ts(c.get("updated_on"))
        if u is not None and u >= cutoff:
            rows.append({"subject_key": c.get("subject_key"), "section": c.get("section"),
                         "zone": c.get("zone"), "updated_on": c.get("updated_on"),
                         "label": _claim_label(c.get("claim"))})
    rows.sort(key=lambda r: r.get("updated_on") or "", reverse=True)
    return rows


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
            "is_new": c.get("subject_key") in recent_keys,  # monitor-touched within the badge window
        })
    return rows


# --- aggregate: the full data shape the UI renders for one card --------------
def card_status(slug: str) -> dict:
    meta = store.load_meta(slug) or {}
    claims = store.load_claims(slug)
    recent = recent_updates(slug)
    # badge keys = monitor alerts AND approved propagation edits (updated_on) within the window
    recent_keys = {a.get("subject_key") for a in recent if a.get("subject_key")}
    recent_keys |= recently_updated_keys(claims)
    return {
        "slug": slug,
        "meta": meta,
        "checkpoints": checkpoints(meta),
        "change_feed": change_feed(slug),
        "agent_activity": agent_activity(slug, meta, claims),
        "claim_timestamps": claim_timestamps(claims, recent_keys),
        "recent_updates": recent,        # powers the "Just updated" sidebar + NEW badges
        "recent_keys": sorted(recent_keys),
        "recently_updated": recently_updated_claims(claims),   # claim-level changelog (deep-links)
    }
