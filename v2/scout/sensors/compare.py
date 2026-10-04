"""The shadow's proof. For every alert that LANDS on a card through today's triage, was a finding
behind it? Two levels, because triage's own output is noisy (its `source_hint` is prose, both sides
say NEW for new subjects, and triage finds a given story in about one run of four):

  Level A (the cutover gate): a landed alert is COVERED when a finding in the window contains the
  claim's evidence excerpt, or shares the claim's source host, or a screen candidate named its
  subject key. Otherwise it is a MISS, with a reason class the FYI shows: `no_source` (no registry
  page or feed on that host and no news item), `no_finding` (the host is watched, nothing new was
  read there) or `screened_out` (a finding was there, the screen did not surface it).

  Level B (reported only): triage's substantial candidates with no screen candidate on the same
  non-NEW subject key and no finding sharing three or more distinctive tokens.

Records: `sensors/_compare/<date>/<slug>.json`; the streak in `sensors/_compare/streak.json`.
"""
from __future__ import annotations

import json
import re
from datetime import datetime

from scout import selfserve
from scout.sensors import feeds
from scout.sources import classify

RECORD = "sensors/_compare/{date}/{slug}.json"
STREAK = "sensors/_compare/streak.json"
_WS = re.compile(r"\s+")
_TOK = re.compile(r"[a-z0-9][a-z0-9.$%-]{2,}")
_STOP = {"the", "and", "for", "with", "that", "this", "from", "its", "has", "have", "will", "are", "was",
         "were", "new", "into", "over", "after", "before", "about", "than", "more", "less", "per", "month",
         "year", "announces", "announced", "launches", "launched", "says", "said", "report", "reports"}


def _norm(s: str) -> str:
    return _WS.sub(" ", (s or "").replace("’", "'").replace(" ", " ")).strip().lower()


def tokens(s: str, drop: set | None = None) -> set:
    toks = {t.strip(".$%-") for t in _TOK.findall(_norm(s))}
    return {t for t in toks if t and t not in _STOP and t not in (drop or set())}


def entity_tokens(names: list[str]) -> set:
    out = set()
    for n in names or []:
        out |= tokens(n)
    return out


def _bare(url: str) -> str:
    try:
        from urllib.parse import urlparse
        p = urlparse(url or "")
        return f"{(p.hostname or '').lower().removeprefix('www.')}{p.path.rstrip('/')}"
    except Exception:
        return url or ""


def recent_screen_candidates(slug: str, dates: list[str]) -> list[dict]:
    """The screen's candidates for this card over the compare records of the given dates (a story that
    landed through triage today may have been screened yesterday and consumed for this card)."""
    out = []
    for d in dates:
        try:
            raw = selfserve.read_data(RECORD.format(date=d, slug=slug))
            if raw:
                out += ((json.loads(raw).get("screen") or {}).get("candidates") or [])
        except Exception:
            continue
    return out


def level_a(landed: list[tuple[dict, dict]], findings: list[dict], screen_cands: list[dict],
            watched_hosts: set, names: list[str] | None = None, baseline: str | None = None) -> list[dict]:
    """One row per landed (claim, alert): would the SENSOR PATH have produced this alert?
    covered_by `screen` when a screen candidate names its subject key or shares three distinctive
    tokens with the claim; otherwise a MISS whose reason says where the chain broke:
    `screened_out` (a finding carried it: same URL, the excerpt, or a story sharing three tokens),
    `no_finding` (the host is watched, nothing new was read there), `no_source` (nothing watches it).
    An alert whose event (`as_of`) predates the sensors' baseline day is `pre_baseline`: not a miss,
    not covered, not comparable (2026-10-04: a 19-day catch-up landed nine alerts about events the
    sensors never had a chance to read, and every one counted as a miss)."""
    rows = []
    ent = entity_tokens(names or [])
    f_texts = [(_norm((f.get("title") or "") + " " + (f.get("text") or "")), tokens((f.get("title") or "") + " " + (f.get("text") or "")[:400], ent), f) for f in findings]
    f_urls = {_bare(f.get("source_url") or "") for f in findings}
    f_hosts = {f.get("source_host") for f in findings if f.get("source_host")}
    sk_cands = {str(c.get("subject_key")) for c in screen_cands if c.get("subject_key") and c.get("subject_key") != "NEW"}
    cand_toks = [tokens(c.get("signal") or "", ent) for c in screen_cands]
    for claim, alert in landed:
        sk = str(claim.get("subject_key") or alert.get("subject_key") or "")
        url = claim.get("source_url") or alert.get("source_url") or ""
        host = feeds.host_of(url)
        excerpt = _norm(claim.get("evidence_excerpt") or "")
        ctoks = tokens((claim.get("claim") or "") + " " + (alert.get("headline") or ""), ent)
        row = {"subject_key": sk, "source_url": url, "host": host}
        as_of = str(claim.get("as_of") or "")[:10]
        if baseline and as_of and as_of < baseline:
            row["covered_by"] = "pre_baseline"
            row["as_of"] = as_of
        elif sk in sk_cands or any(len(ctoks & t) >= 3 for t in cand_toks):
            row["covered_by"] = "screen"
        else:
            row["miss"] = True
            seen = (_bare(url) in f_urls
                    or (excerpt and len(excerpt) >= 30 and any(excerpt[:120] in t for t, _, _ in f_texts))
                    or any(len(ctoks & ft) >= 3 for _, ft, _ in f_texts))
            if seen:
                row["miss_reason"] = "screened_out"
            elif host and (host in watched_hosts or host in f_hosts or classify.classify(f"https://{host}/") in ("news", "company_statement", "filing", "government", "research")):
                row["miss_reason"] = "no_finding"          # a watched page or an indexed outlet: nothing new was read there
            else:
                row["miss_reason"] = "no_source"           # nothing watches this host and no index carries it
        rows.append(row)
    return rows


def level_b(triage_cands: list[dict], screen_cands: list[dict], findings: list[dict], names: list[str]) -> dict:
    ent = entity_tokens(names)
    pool = [tokens((f.get("title") or "") + " " + (f.get("text") or "")[:300], ent) for f in findings]
    pool += [tokens(c.get("signal") or "", ent) for c in screen_cands]
    sk_cands = {str(c.get("subject_key")) for c in screen_cands if c.get("subject_key") and c.get("subject_key") != "NEW"}
    matched, misses = [], []
    for c in triage_cands:
        if c.get("substantial") is not True:
            continue
        sk = str(c.get("subject_key") or "NEW")
        toks = tokens(c.get("signal") or "", ent)
        if (sk != "NEW" and sk in sk_cands) or any(len(toks & p) >= 3 for p in pool):
            matched.append({"subject_key": sk, "signal": (c.get("signal") or "")[:160]})
        else:
            misses.append({"subject_key": sk, "signal": (c.get("signal") or "")[:160], "source_hint": (c.get("source_hint") or "")[:160]})
    screen_only = [{"subject_key": c.get("subject_key"), "signal": (c.get("signal") or "")[:160], "substantial": c.get("substantial")}
                   for c in screen_cands
                   if not any(len(tokens(c.get("signal") or "", ent) & tokens(t.get("signal") or "", ent)) >= 3 for t in triage_cands)]
    return {"matched": matched, "misses": misses, "screen_only": screen_only}


def record(slug: str, date: str, payload: dict, write: bool) -> dict:
    doc = {"schema_version": 1, "slug": slug, "date": date, "written_at": datetime.now().isoformat(timespec="seconds"), **payload}
    if write:
        try:
            selfserve.write_data(RECORD.format(date=date, slug=slug), json.dumps(doc, indent=1, ensure_ascii=False, default=str),
                                 f"sensors: compare {date} {slug}")
        except Exception as e:
            doc["write_error"] = f"{type(e).__name__}: {e}"
    return doc


def load_streak() -> dict:
    try:
        raw = selfserve.read_data(STREAK)
        s = json.loads(raw) if raw else {}
    except Exception:
        s = {}
    s.setdefault("runs", [])
    s.setdefault("clean_streak", 0)
    return s


def update_streak(date: str, cards: list[dict], *, gate_runs: int, write: bool) -> dict:
    """One row per scheduled run. A run is CLEAN when every card had zero Level A misses, the sensor
    error ratio stayed under 10%, and the screen cost at most $0.03 a card. The screen's substantial
    count is RECORDED next to triage's, not gated: the first real comparison (2026-10-03) had the
    screen surface two Salesforce acquisitions triage missed in the same window, and a "no more than
    triage" rule would have called better recall a failure. The bill is bounded by the run ceiling;
    the checkpoint report projects it from the escalation rate so the call is informed.
    `cards`: [{slug, misses_a, errors, sources, screen_cost, screen_subst, triage_subst, unavailable}]."""
    s = load_streak()
    misses = sum(int(c.get("misses_a") or 0) for c in cards)
    errors = sum(int(c.get("errors") or 0) for c in cards)
    sources = sum(int(c.get("sources") or 0) for c in cards)
    cost = sum(float(c.get("screen_cost") or 0) for c in cards)
    screen_subst = sum(int(c.get("screen_subst") or 0) for c in cards)
    triage_subst = sum(int(c.get("triage_subst") or 0) for c in cards)
    n = max(1, len(cards))
    err_ratio = (errors / sources) if sources else 0.0
    findings = sum(int(c.get("findings") or 0) for c in cards)
    # the day the sensors only set their baselines (no finding anywhere, nothing screened) is not a
    # clean run and not a failed one: the streak starts the day after
    baseline = findings == 0 and screen_subst == 0 and sum(int(c.get("findings_recent") or 0) for c in cards) == 0
    clean = (not baseline) and misses == 0 and err_ratio < 0.10 and (cost / n) <= 0.03
    reasons = []
    if baseline:
        reasons.append("baseline day: sensors read everything for the first time, no findings yet")
    if misses:
        reasons.append(f"{misses} Level A miss(es)")
    if err_ratio >= 0.10:
        reasons.append(f"sensor errors {err_ratio:.0%}")
    if (cost / n) > 0.03:
        reasons.append(f"screen ${cost / n:.3f} a card")
    escalating = sum(1 for c in cards if int(c.get("screen_subst") or 0) > 0)
    row = {"date": date, "cards": len(cards), "misses_a": misses, "errors": errors, "sources": sources,
           "screen_cost": round(cost, 4), "screen_subst": screen_subst, "triage_subst": triage_subst,
           "screen_escalating_cards": escalating,                 # cards that would pay for materiality in gate mode
           "triage_escalating_cards": sum(1 for c in cards if int(c.get("triage_subst") or 0) > 0),
           "findings": findings, "baseline": baseline, "clean": clean, "reasons": reasons}
    s["runs"] = [r for r in s["runs"] if r.get("date") != date][-60:] + [row]
    streak = 0
    for r in reversed(s["runs"]):
        if r.get("baseline"):
            continue                              # neither counts nor breaks
        if r.get("clean"):
            streak += 1
        else:
            break
    s["clean_streak"] = streak
    s["gate_runs"] = gate_runs
    s["ready"] = streak >= gate_runs
    s["updated_at"] = datetime.now().isoformat(timespec="seconds")
    if write:
        try:
            selfserve.write_data(STREAK, json.dumps(s, indent=1, ensure_ascii=False), f"sensors: streak {date} ({streak} clean)")
        except Exception as e:
            s["write_error"] = f"{type(e).__name__}: {e}"
    return s
