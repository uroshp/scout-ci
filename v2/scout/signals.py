"""Signals (WS3, 2026-09-29): event-driven monitoring, model-free.

A poller (the mini, hourly, $0) watches two structured sources per card and writes what it finds
to the private store; a run of the monitor reads the open signals and puts them in front of
triage. The store is the truth; a workflow dispatch is only the nudge that starts a run early.

  filing          a new SEC filing (8-K, 10-Q, 10-K, Form D) on a watched CIK -> DISPATCH that
                  card's check now (at most SIGNAL_MAX_DISPATCHES_PER_DAY across all cards, never
                  within SIGNAL_MIN_HOURS_SINCE_CHECK of the last check).
  new_department  a hiring department the board has not shown in SIGNAL_NEW_DEPT_LOOKBACK_DAYS
                  days, with at least SIGNAL_NEW_DEPT_MIN_ROLES open roles -> QUEUED for the next
                  scheduled run (no dispatch). Uroš 2026-09-29: "the major signal is a brand-new
                  department, where investment is going".
  hiring context  every other job-board delta (net-new, by department) -> a one-line CONTEXT block
                  handed to materiality and the router, never a trigger ("hiring is a footnote;
                  ten roles is nothing; interpret it through strategy").

Store layout (under the RC prefix on RC):
  signals/<slug>/state.json     {edgar: {cik: {seen: [accessions], since, company}}, boards: {host:token: {ids: {id: dept}, departments: {dept: first_seen}, snapshot: {day, net, by_department}}}}
  signals/<slug>/events.jsonl   one Signal per line: {kind, fingerprint, detected_at, source_url, source_class, summary, ..., consumed_by}
  signals/_dispatch.json        {day, count, last: {slug: iso}}
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone

import httpx

from scout import config, selfserve
from scout.sources import edgar, jobs

STATE = "signals/{slug}/state.json"
EVENTS = "signals/{slug}/events.jsonl"
DISPATCH = "signals/_dispatch.json"
_GH_API = "https://api.github.com"


def _utcnow() -> datetime:
    """Naive UTC, the clock the Actions runner stamps last_checked with (the mini runs on Pacific;
    comparing the two raw would make the 6-hour guard 7 hours wrong)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _now() -> str:
    return _utcnow().isoformat(timespec="seconds")


def _today() -> str:
    return _utcnow().date().isoformat()


def _fp(*parts) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:16]


# --- watch config ----------------------------------------------------------------------------------------
def watch_for(meta: dict | None) -> dict:
    """{edgar_ciks: [str], job_boards: [{host, token}]} from meta.watch (set by scripts/set_watch.py;
    `edgar_cik` (one) or `edgar_ciks` (a card with two public companies watches both)."""
    w = (meta or {}).get("watch") or {}
    boards = [b for b in (w.get("job_boards") or []) if isinstance(b, dict) and b.get("host") and b.get("token")]
    ciks = [str(c).strip() for c in ([w.get("edgar_cik")] + list(w.get("edgar_ciks") or [])) if c and str(c).strip()]
    return {"edgar_ciks": list(dict.fromkeys(ciks)), "job_boards": boards}


# --- state -----------------------------------------------------------------------------------------------
def load_state(slug: str) -> dict:
    try:
        raw = selfserve.read_data(STATE.format(slug=slug))
        s = json.loads(raw) if raw else {}
    except Exception:
        s = {}
    s.setdefault("edgar", {})
    s.setdefault("boards", {})
    return s


def save_state(slug: str, state: dict) -> None:
    selfserve.write_data(STATE.format(slug=slug), json.dumps(state, indent=1, ensure_ascii=False), f"signals: state {slug}")


def append_events(slug: str, signals: list[dict]) -> None:
    if not signals:
        return
    def tx(cur):
        lines = [l for l in (cur or "").splitlines() if l.strip()]
        have = {json.loads(l).get("fingerprint") for l in lines}
        for s in signals:
            if s["fingerprint"] not in have:
                lines.append(json.dumps(s, ensure_ascii=False))
        return "\n".join(lines[-500:]) + "\n"
    selfserve.update_data(EVENTS.format(slug=slug), tx, f"signals: {len(signals)} event(s) {slug}")


def events(slug: str) -> list[dict]:
    try:
        raw = selfserve.read_data(EVENTS.format(slug=slug)) or ""
    except Exception:
        return []
    out = []
    for l in raw.splitlines():
        try:
            out.append(json.loads(l))
        except Exception:
            continue
    return out


OPEN_MAX_DAYS = 7


def open_signals(slug: str) -> list[dict]:
    """Signals no run has consumed yet, oldest first. A signal older than OPEN_MAX_DAYS is dropped
    from the open set (a consume that failed cannot re-escalate a card forever)."""
    cutoff = (_utcnow() - timedelta(days=OPEN_MAX_DAYS)).isoformat(timespec="seconds")
    return [e for e in events(slug) if not e.get("consumed_by") and str(e.get("detected_at") or "") >= cutoff]


def mark_dispatched(slug: str, fingerprints: list[str], when: str | None = None) -> None:
    """Stamp `dispatched_at` on the named open events (a dispatched filing is never dispatched twice;
    a refused one keeps no stamp and is retried on the next poll)."""
    want = set(fingerprints or [])
    when = when or _now()
    def tx(cur):
        out = []
        for l in (cur or "").splitlines():
            if not l.strip():
                continue
            try:
                e = json.loads(l)
            except Exception:
                continue
            if e.get("fingerprint") in want:
                e["dispatched_at"] = when
            out.append(json.dumps(e, ensure_ascii=False))
        return "\n".join(out) + ("\n" if out else "")
    try:
        selfserve.update_data(EVENTS.format(slug=slug), tx, f"signals: dispatched {slug}")
    except Exception:
        pass


def consume(slug: str, run_ts: str, fingerprints: list[str] | None = None) -> int:
    """Mark open signals consumed by a run (all of them, or the named ones). Returns the count."""
    want = set(fingerprints or [])
    n = 0
    def tx(cur):
        nonlocal n
        out = []
        for l in (cur or "").splitlines():
            if not l.strip():
                continue
            try:
                e = json.loads(l)
            except Exception:
                continue
            if not e.get("consumed_by") and (not want or e.get("fingerprint") in want):
                e["consumed_by"] = run_ts
                n += 1
            out.append(json.dumps(e, ensure_ascii=False))
        return "\n".join(out) + ("\n" if out else "")
    try:
        selfserve.update_data(EVENTS.format(slug=slug), tx, f"signals: consumed by {run_ts} {slug}")
    except Exception:
        return 0
    return n


# --- polling ---------------------------------------------------------------------------------------------
def poll_edgar(cik: str, state: dict, today: str | None = None) -> list[dict]:
    """New filings on the CIK since the state's watermark -> Signals (kind "filing"). First poll of
    a CIK only sets the watermark (the card was generated with the history it needed)."""
    today = today or _today()
    all_st = state.setdefault("edgar", {})
    if "seen" in all_st and "since" in all_st and not any(isinstance(v, dict) for v in all_st.values()):
        all_st = state["edgar"] = {str(cik): all_st}                       # legacy single-CIK shape
    st = all_st.setdefault(str(cik), {"seen": [], "since": None, "company": None})
    rows = edgar.filings(cik, forms=config.SIGNAL_FORMS, since=st.get("since"), limit=40)
    seen_list = list(st.get("seen") or [])
    seen = set(seen_list)
    out = []
    if st.get("since") is None:
        st["since"] = today
        st["seen"] = [r["accession"] for r in rows][:60]
        try:
            st["company"] = edgar.company_name(cik)                         # so a wrong CIK is visible in the log
        except Exception:
            st["company"] = None
        return []
    for r in rows:
        if r["accession"] in seen:
            continue
        summary = f"New {r['form']} filed {r['filed']}" + (f" (items {r['items']})" if r.get("items") else "") \
            + (f": {r['description']}" if r.get("description") else "")
        out.append({"kind": "filing", "form": r["form"], "filed": r["filed"], "accession": r["accession"],
                    "source_url": r["url"], "index_url": r.get("index_url"), "source_class": "filing",
                    "summary": summary, "fingerprint": _fp("filing", cik, r["accession"]), "detected_at": _now(),
                    "consumed_by": None, "trigger": True})
        seen.add(r["accession"]); seen_list.append(r["accession"])
    st["seen"] = seen_list[-200:]                                            # ordered: trimming drops the oldest
    st["since"] = max(st.get("since") or "", max((r["filed"] for r in rows), default=st.get("since") or today))
    return out


def poll_jobs(host: str, token: str, state: dict, today: str | None = None) -> tuple[dict, list[dict]]:
    """One board -> (context, signals). `context` = {net, added, removed, by_department, open} always;
    a Signal ONLY for a brand-new department (not seen on this board in the lookback window, with at
    least the minimum open roles). The first poll of a board only takes the snapshot."""
    today = today or _today()
    key = f"{host}:{token}"
    boards = state.setdefault("boards", {})
    st = boards.setdefault(key, {"ids": {}, "departments": {}, "snapshot": None})
    cur = jobs.postings(host, token)
    prev = [{"id": i, "department": d} for i, d in (st.get("ids") or {}).items()]
    first = st.get("snapshot") is None
    d = jobs.diff(prev, cur)
    depts_now: dict[str, int] = {}
    for r in cur:
        dept = (r.get("department") or "").strip()
        if dept:
            depts_now[dept] = depts_now.get(dept, 0) + 1
    history = st.get("departments") or {}
    cutoff = (date.fromisoformat(today) - timedelta(days=config.SIGNAL_NEW_DEPT_LOOKBACK_DAYS)).isoformat()
    signals = []
    if not first:
        for dept, n in depts_now.items():
            seen_on = history.get(dept)
            if (seen_on is None or seen_on < cutoff) and n >= config.SIGNAL_NEW_DEPT_MIN_ROLES:
                signals.append({"kind": "new_department", "department": dept, "roles": n, "host": host,
                                "source_url": jobs.board_url(host, token), "source_class": "job_posting",
                                "summary": f"New hiring department on {host}: {dept} ({n} open roles)",
                                "fingerprint": _fp("dept", host, token, dept, today[:7]), "detected_at": _now(),
                                "consumed_by": None, "trigger": False})
    for dept in depts_now:
        history.setdefault(dept, today)
    st["departments"] = history
    st["ids"] = {r["id"]: (r.get("department") or "") for r in cur}
    context = {"host": host, "open": len(cur), "net": 0 if first else d["net"], "added": 0 if first else len(d["added"]),
               "removed": 0 if first else len(d["removed"]), "by_department": {} if first else d["by_department"], "day": today}
    st["snapshot"] = context
    return context, signals


def context_block(slug: str, state: dict | None = None) -> str:
    """The one-line hiring CONTEXT for the prompts (materiality, router): never a trigger."""
    state = state or load_state(slug)
    lines = []
    for key, st in (state.get("boards") or {}).items():
        snap = st.get("snapshot") or {}
        if not snap:
            continue
        by = snap.get("by_department") or {}
        top = ", ".join(f"{k} {'+' if v > 0 else ''}{v}" for k, v in sorted(by.items(), key=lambda kv: -abs(kv[1]))[:5])
        lines.append(f"- {snap.get('host')} board: {snap.get('open')} open roles, net {snap.get('net'):+d} since the last poll"
                     + (f" ({top})" if top else "") + f", as of {snap.get('day')}")
    if not lines:
        return ""
    return ("\n\nHIRING CONTEXT (structured, from the public job board; context for judgment, never a "
            "development on its own; a headcount move matters only through what it says about strategy):\n"
            + "\n".join(lines))


# --- dispatch --------------------------------------------------------------------------------------------
def _dispatch_state() -> dict:
    try:
        raw = selfserve.read_data(DISPATCH)
        s = json.loads(raw) if raw else {}
    except Exception:
        s = {}
    if s.get("day") != _today():
        s = {"day": _today(), "count": 0, "last": {}}
    s.setdefault("count", 0); s.setdefault("last", {})
    return s


def _near_anchor(now: datetime) -> bool:
    """Inside the band around a scheduled anchor (90 min before to 120 min after): the scheduled run
    will carry the signal (it is in the store already); dispatching here would double-pay the card
    and could race the queued run's write."""
    for hhmm in (getattr(config, "MONITOR_ANCHORS_UTC", None) or []):
        try:
            h, m = str(hhmm).split(":")
            anchor = now.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
        except Exception:
            continue
        for a in (anchor, anchor - timedelta(days=1)):
            if a - timedelta(minutes=90) <= now <= a + timedelta(minutes=120):
                return True
    return False


def _monitor_busy() -> bool:
    """A monitor run queued or in progress: dispatching now would queue behind it and re-check a
    card the run may be writing. One cheap read with the store token."""
    if not (config.SELFSERVE_GH_TOKEN and config.SELFSERVE_DISPATCH_REPO):
        return False
    url = f"{_GH_API}/repos/{config.SELFSERVE_DISPATCH_REPO}/actions/workflows/{config.MONITOR_DISPATCH_WORKFLOW}/runs"
    try:
        for status in ("in_progress", "queued"):
            r = httpx.get(url, headers=selfserve._headers(), params={"status": status, "per_page": 1}, timeout=15)
            if r.status_code == 200 and (r.json().get("total_count") or 0) > 0:
                return True
    except Exception:
        return False
    return False


def may_dispatch(slug: str, meta: dict | None, now: datetime | None = None) -> tuple[bool, str]:
    """The caps: at most N dispatches a day across all cards; never within H hours of the card's
    last check (a triggered run counts as the day's check for the due gate)."""
    now = now or _utcnow()
    s = _dispatch_state()
    if s["count"] >= config.SIGNAL_MAX_DISPATCHES_PER_DAY:
        return False, f"daily dispatch cap ({config.SIGNAL_MAX_DISPATCHES_PER_DAY}) reached"
    if _near_anchor(now):
        return False, "inside the scheduled run's window; it will carry the signal"
    last = (meta or {}).get("last_checked")
    try:
        if last and (now - datetime.fromisoformat(str(last)[:19])) < timedelta(hours=config.SIGNAL_MIN_HOURS_SINCE_CHECK):
            return False, f"checked within the last {config.SIGNAL_MIN_HOURS_SINCE_CHECK:g} h"
    except Exception:
        pass
    if s["last"].get(slug, "") >= (now - timedelta(hours=config.SIGNAL_MIN_HOURS_SINCE_CHECK)).isoformat():
        return False, "dispatched for this card recently"
    return True, ""


def dispatch(slug: str, reason: str, meta: dict | None = None) -> tuple[bool, str]:
    """Start that card's check now: a workflow_dispatch of the monitor with slugs=<slug>, force=true.
    Records the dispatch under the cap. The run reads the store; `reason` is a log hint only."""
    ok, why = may_dispatch(slug, meta)
    if not ok:
        return False, why
    if not (config.SELFSERVE_GH_TOKEN and config.SELFSERVE_DISPATCH_REPO):
        return False, "no dispatch token"
    if _monitor_busy():
        return False, "a monitor run is queued or in progress"
    # the code ref follows the store: an RC poller (prefix set) dispatches rc, production main
    ref = "rc" if getattr(config, "SELFSERVE_DATA_PREFIX", "") else "main"
    url = f"{_GH_API}/repos/{config.SELFSERVE_DISPATCH_REPO}/actions/workflows/{config.MONITOR_DISPATCH_WORKFLOW}/dispatches"
    try:
        r = httpx.post(url, headers=selfserve._headers(), timeout=20,
                       json={"ref": ref, "inputs": {"slugs": slug, "force": True, "reason": reason[:200]}})
    except httpx.HTTPError as e:
        return False, f"dispatch failed: {type(e).__name__}"
    if r.status_code != 204:
        return False, f"dispatch failed: HTTP {r.status_code}"
    def tx(cur):
        s = _dispatch_state() if not cur else json.loads(cur)
        if s.get("day") != _today():
            s = {"day": _today(), "count": 0, "last": {}}
        s["count"] = int(s.get("count") or 0) + 1
        s.setdefault("last", {})[slug] = _now()
        return json.dumps(s, indent=1)
    try:
        selfserve.update_data(DISPATCH, tx, f"signals: dispatched {slug}")
    except Exception:
        pass
    return True, "dispatched"


# --- one card, one poll ---------------------------------------------------------------------------------
def poll_card(slug: str, meta: dict, write: bool = True, allow_dispatch: bool = True, today: str | None = None) -> dict:
    """Poll every watched source of a card, persist state + events, dispatch on a filing. Returns a
    summary dict; never raises (a source's failure is recorded, the others still run)."""
    w = watch_for(meta)
    out = {"slug": slug, "filings": 0, "new_departments": 0, "context": [], "dispatched": None, "errors": [], "companies": []}
    if not (w["edgar_ciks"] or w["job_boards"]):
        out["skipped"] = "no watch"
        return out
    state = load_state(slug)
    found: list[dict] = []
    for cik in w["edgar_ciks"]:
        try:
            sig = poll_edgar(cik, state, today)
            found += sig; out["filings"] += len(sig)
            out["companies"].append((state.get("edgar", {}).get(str(cik)) or {}).get("company") or cik)
        except Exception as e:
            out["errors"].append(f"edgar {cik}: {type(e).__name__}: {e}")
    for b in w["job_boards"]:
        try:
            ctx, sig = poll_jobs(b["host"], b["token"], state, today)
            found += sig; out["new_departments"] += len(sig); out["context"].append(ctx)
        except Exception as e:
            out["errors"].append(f"{b['host']}: {type(e).__name__}: {e}")
    if write:
        try:
            save_state(slug, state)
            append_events(slug, found)
        except Exception as e:
            out["errors"].append(f"store: {type(e).__name__}: {e}")
    if allow_dispatch and write:
        # from the STORE, not this poll: a filing whose dispatch was refused last hour is retried;
        # one that was dispatched carries `dispatched_at` and is never dispatched twice
        due = [e for e in open_signals(slug) if e.get("trigger") and not e.get("dispatched_at")]
        if due:
            ok, why = dispatch(slug, "; ".join(e["summary"] for e in due)[:200], meta)
            out["dispatched"] = why
            if ok:
                mark_dispatched(slug, [e["fingerprint"] for e in due])
    return out
