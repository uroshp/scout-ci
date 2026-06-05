"""Monitoring engine (v2 build step 4): subject-key-centric, date-scoped change detection.

Loop (spec §6): load baseline -> date-scoped retrieve -> cheap TRIAGE gate (Haiku, few
searches) -> escalate to Opus MATERIALITY judgment ONLY when triage flags a SUBSTANTIAL
development -> in-place update claims + alerts + dedup -> advance last_checked.

Cost shape: triage runs every check and is deliberately cheap (Haiku + capped searches +
sub-dollar budget). The expensive Opus stage runs ONLY on windows with genuinely material
news — a quiet or minor-only window exits triage-only at pennies. Most checks are quiet.

Detection is shape (B): the agent searches for NEW signals since last_checked, maps each to a
tracked subject_key (or a genuinely new material subject), and updates ONLY the affected claims.
It does NOT re-research everything — that's what keeps a no-change check cheap.

Update semantics: in-place revise of the matched claim (git history is the version trail).
Dedup: semantic fingerprint = subject_key + normalized new value (value-changes dedup naturally
against committed claims.json; net-new events dedup via the fingerprint set in meta).

NOT here (your awake-review items): the email side-effect and the Actions cron. This engine
updates the store and returns a structured result; committing + emailing are separate steps.
"""
import asyncio
import hashlib
import json
import os
import re
from datetime import datetime, timedelta

from claude_agent_sdk import ClaudeAgentOptions

from scout import config, store
from scout.fetch_tool import FETCH_SERVER, FETCH_TOOL_NAME, reset_log
from scout.generate import _drive, _extract_json
from scout.grounding import ground_claims
from scout.render import claims_to_markdown, clean_output, format_report
from scout.schema import claim_id, pregrounding_errors, validation_errors

MATERIAL_CATEGORIES = (
    "funding, IPO/S-1 filing, M&A, exec hire/departure, pricing/packaging change, "
    "usage-limit/budget-cap change, major product launch, product discontinuation/sunset, "
    "security incident/breach, outage, legal action, layoffs, partnership shift, "
    "CONTRACT CANCELLATION / CUSTOMER LOSS / CHURN / DEFECTION, public strategy change"
)


def _title(meta: dict) -> str:
    comp, me = meta.get("competitor"), meta.get("my_company")
    if me:
        return f"# Competitive Intelligence Brief: {me} vs {comp}"
    return f"# Competitive Intelligence Brief: {comp}"


def _tracked_digest(claims: list[dict]) -> str:
    """Compact 'subject_key — current claim' list to anchor detection on tracked subjects."""
    lines = []
    for c in claims:
        lines.append(f"- {c.get('subject_key')} — {str(c.get('claim',''))[:160]}")
    return "\n".join(lines)


def _fingerprint(subject_key: str, new_value: str) -> str:
    # Strip ALL whitespace so "$852B" and "$852 B" dedup to the same transition.
    norm = re.sub(r"\s+", "", f"{subject_key}|{new_value}".lower())
    return "f_" + hashlib.sha256(norm.encode()).hexdigest()[:12]


def _since_date(s: str | None) -> str | None:
    """Normalize a stored last_checked/baseline (a plain date OR a full datetime
    ISO string, now that last_checked carries a time) to a YYYY-MM-DD cutoff for
    the date-scoped triage search."""
    return s[:10] if s else s


# --- Stage 1: cheap triage gate ----------------------------------------------
_TRIAGE_SYSTEM = f"""You are a monitoring TRIAGE GATE for a living competitive-intelligence
battlecard. You run on EVERY check and most windows are quiet, so you must be CHEAP and decisive.
Your job: surface developments that are (a) genuinely NEW since a cutoff date AND (b) NOT already
reflected in the tracked claims, and decide whether ANY of them is SUBSTANTIAL enough to justify
the expensive downstream judge. Do a SMALL number of searches and STOP.

Do AT MOST {config.TRIAGE_MAX_SEARCHES} date-scoped WebSearches for the competitor's recent news,
then DECIDE — do not keep searching. Material categories:
{MATERIAL_CATEGORIES}.

Deliberately hunt ADVERSE / competitive-threat signals, not just announcements and wins —
cancellations, customer losses, churn, budget caps, outages, layoffs, lawsuits are exactly the
high-value developments to surface. Anchor on reputable NEWS outlets; never treat Wikipedia, a
wiki, an encyclopedia, or a promo/SEO listicle as a source.

Apply TWO STRICT FILTERS before surfacing anything:
1. DATE: surface a development ONLY if it is dated ON OR AFTER the cutoff date given. Discard older
   items — anything before the cutoff is already covered by the baseline.
2. ALREADY-CAPTURED (not merely "already-mentioned"): the tracked claims below are the OLD state.
   Surface a candidate if the development reports a DIFFERENT value or status than the tracked claim
   (a changed metric, a new CEO, a price change, a strategy reversal, a product superseded) OR
   concerns a subject not tracked at all. Drop a candidate ONLY when a tracked claim ALREADY states
   this exact development (the update would be a no-op). When genuinely unsure whether something is
   new, surface it (with your best "substantial" judgment below).

Then, for EACH surfaced candidate, set "substantial":
  - true  — a CONCRETE, consequential event in the material categories that would move the
            battlecard: a funding round / IPO / S-1, M&A, exec hire or departure, a pricing /
            packaging / usage-limit change, a major product launch or discontinuation, a security
            incident, legal action, layoffs, a partnership shift, or a customer loss / churn /
            defection — a real change worth a human's attention, with a date and a credible source.
  - false — incremental or routine: a minor feature, a blog/opinion post, a restated known fact, a
            rumor without a source, sentiment churn, or anything whose importance is marginal.

ESCALATION RULE (this governs cost): the expensive judge runs ONLY if at least one candidate is
"substantial": true. A quiet window — nothing new, or only minor/routine items — is the COMMON,
correct, CHEAP outcome: report what you found and the pipeline stops here. Reserve
"substantial": true for genuinely notable news; do NOT mark marginal items substantial "to be
safe" — a missed minor item is simply re-checked next day. Keep the routine check cheap.

Return ONLY a single fenced ```json block:
{{"has_candidates": <bool>, "candidates": [
  {{"signal": "<one line, INCLUDING the development's date>", "subject_key": "<matching tracked subject_key, or NEW>",
    "substantial": <bool>,
    "why_new": "<why this is new since the cutoff AND not already in the tracked claims>",
    "source_hint": "<url or outlet>"}} ]}}
If nothing passes the filters, return has_candidates=false and an empty list — that is the common,
correct, cheap outcome on a quiet window."""


async def _run_triage(meta, since, claims):
    comp, me = meta.get("competitor"), meta.get("my_company")
    user = (f"Competitor: {comp}" + (f" (we are {me})" if me else "") +
            f"\nCUTOFF DATE: {since}. Surface ONLY developments dated on/after {since} that are NOT "
            f"already reflected in the tracked subjects below (apply both strict filters).\n\n"
            f"TRACKED SUBJECTS (subject_key — current value already known):\n"
            + _tracked_digest(claims))
    options = ClaudeAgentOptions(
        model=config.FAST_MODEL,
        system_prompt={"type": "preset", "preset": "claude_code", "append": _TRIAGE_SYSTEM},
        mcp_servers={"scoutfetch": FETCH_SERVER},
        allowed_tools=["WebSearch", FETCH_TOOL_NAME],
        disallowed_tools=["WebFetch"],
        permission_mode="bypassPermissions",
        # Triage-specific tight caps (lever B): few turns structurally bound the number of
        # searches, and a sub-dollar budget hard-stops the routine check at pennies.
        max_turns=config.TRIAGE_MAX_TURNS,
        max_budget_usd=config.TRIAGE_MAX_BUDGET_USD,
    )
    return await _drive(user, options, "triage")


# --- Stage 2: materiality judgment (Opus) ------------------------------------
_MATERIALITY_SYSTEM = """You are the MATERIALITY JUDGE for a living competitive battlecard. Triage
surfaced candidate signals. For EACH candidate decide if it is genuinely MATERIAL — it moves a
battlecard zone, a price, a positioning claim, a metric the brief tracks, or introduces a risk —
versus NOISE (routine posts, minor features, restated known facts, sentiment churn).

For each MATERIAL change: use fetch_page to READ the source, then emit an updated claim object
(same contract as generation: subject_key, claim, claim_type, section [executive_summary|snapshot|
recent_moves|positioning|pricing|battlecard|sentiment|objection_handling], zone [battlecard only,
else null], order, source_url, source_tier [primary|reputable_secondary|sentiment_only],
evidence_excerpt [VERBATIM from fetch_page's real page text], as_of [YYYY-MM-DD], confidence)
AND an alert. If the change updates an existing tracked subject, REUSE that subject_key EXACTLY so
it updates in place; if genuinely new, use a fresh subject_key in the same style.

Do NOT include id/verified/grounding (filled downstream). Every alert MUST carry a "so_what" — the
decision it changes — or the item is NOT material.

SOURCING: anchor source_url on a reputable NEWS outlet (Reuters, Bloomberg, The Information, CNBC,
TechCrunch, major outlet) or a primary filing/announcement. NEVER Wikipedia, a wiki, an
encyclopedia, or a promo/SEO listicle — a deterministic check cuts wiki/encyclopedia anchors, so
you would lose the change. An ADVERSE development (cancellation, churn, loss) should trace to
independent reporting, not only the affected company.

Return ONLY a single fenced ```json block:
{"material": [ {"claim": { ...claim object... },
               "alert": {"old_value": "<prior, or null if new>", "new_value": "<now>",
                         "headline": "<one line>", "so_what": "<the decision it changes>"}} ],
 "immaterial": [ {"signal": "<...>", "why_not": "<...>"} ]}"""


async def _run_materiality(meta, since, candidates, claims):
    comp, me = meta.get("competitor"), meta.get("my_company")
    user = (f"Competitor: {comp}" + (f" (we are {me})" if me else "") +
            f"\nChanges SINCE {since}.\n\nTRACKED SUBJECTS (subject_key — current value):\n"
            + _tracked_digest(claims) +
            "\n\nCANDIDATE SIGNALS FROM TRIAGE:\n" + json.dumps(candidates, ensure_ascii=False))
    options = ClaudeAgentOptions(
        model=config.ORCHESTRATOR_MODEL,
        system_prompt={"type": "preset", "preset": "claude_code", "append": _MATERIALITY_SYSTEM},
        mcp_servers={"scoutfetch": FETCH_SERVER},
        allowed_tools=["WebSearch", FETCH_TOOL_NAME],
        disallowed_tools=["WebFetch"],
        permission_mode="bypassPermissions",
        max_turns=config.MAX_TURNS,
        max_budget_usd=config.MAX_BUDGET_USD,
    )
    return await _drive(user, options, "materiality")


def _apply_updates(claims, material_grounded, alerted_fingerprints):
    """In-place update claims.json content + build new alert records (deduped).
    material_grounded: list of (claim_dict, alert_dict). Returns (new_claims, new_alerts)."""
    by_id = {c["id"]: c for c in claims}
    new_alerts = []
    seen = set(alerted_fingerprints)
    now = datetime.now()
    for claim, alert in material_grounded:
        fp = _fingerprint(claim["subject_key"], str(alert.get("new_value", claim.get("claim", ""))))
        if fp in seen:
            continue  # already alerted this exact subject->value transition
        seen.add(fp)
        by_id[claim["id"]] = claim  # in-place revise (same id) or add net-new
        new_alerts.append({
            "date": now.date().isoformat(),
            "detected_at": now.isoformat(timespec="seconds"),  # full time -> "recent" badge + feed (A3/A4)
            "subject_key": claim["subject_key"],
            "old_value": alert.get("old_value"),
            "new_value": alert.get("new_value"),
            "headline": alert.get("headline"),
            "so_what": alert.get("so_what"),
            "source_url": claim.get("source_url"),
            "fingerprint": fp,
        })
    return list(by_id.values()), new_alerts


def check(slug: str, write: bool = False, since_override: str | None = None) -> dict:
    """One monitoring check. write=False measures without mutating the store (for cost runs).
    since_override forces the detection window (e.g. an old date to simulate a stale baseline)."""
    meta = store.load_meta(slug) or {}
    claims = store.load_claims(slug)
    since = _since_date(since_override or meta.get("last_checked") or meta.get("baseline_date"))
    checked_at = datetime.now().isoformat(timespec="seconds")  # full timestamp, not just a date
    reset_log()

    # Stage 1: triage (cheap)
    triage = asyncio.run(_run_triage(meta, since, claims))
    try:
        tdata = _extract_json(triage["text"])
    except Exception:
        tdata = {"has_candidates": False, "candidates": []}
    candidates = tdata.get("candidates", []) if tdata.get("has_candidates") else []
    # Strict gate: escalate to the expensive Opus judge ONLY when triage flagged a genuinely
    # SUBSTANTIAL development. Minor/routine candidates are surfaced for the record but do NOT
    # trigger the full pipeline — that's what keeps most checks cheap, triage-only.
    substantial = [c for c in candidates if c.get("substantial") is True]

    result = {
        "slug": slug, "since": since, "no_change": not substantial,
        "candidates": len(candidates), "substantial": len(substantial),
        "minor_skipped": len(candidates) - len(substantial),
        "material": [], "alerts": [],
        "cost": {"triage": triage.get("cost_usd"), "materiality": 0.0},
        "last_checked": checked_at,
    }

    if not substantial:
        # Quiet OR minor-only window: triage-only, cheap. Advance last_checked and stop —
        # the Opus materiality stage never runs.
        if write:
            meta["last_checked"] = checked_at
            store.write_baseline(slug, claims, meta, _current_md(slug))
        return result

    # Stage 2: materiality (Opus) on the SUBSTANTIAL candidates only
    mat = asyncio.run(_run_materiality(meta, since, substantial, claims))
    result["cost"]["materiality"] = mat.get("cost_usd")
    try:
        mdata = _extract_json(mat["text"])
    except Exception:
        mdata = {"material": []}

    # Validate + ground the proposed updated claims
    pending = []
    for m in (mdata.get("material") or []):
        c = m.get("claim")
        if not isinstance(c, dict) or "subject_key" not in c:
            continue
        c["id"] = claim_id(slug, str(c["subject_key"]))
        c["verified"] = True
        if not pregrounding_errors(c):
            pending.append((c, m.get("alert", {})))
    grounded = ground_claims([c for c, _ in pending])
    kept_ids = {c["id"] for c in grounded["kept"]}
    grounded_by_id = {c["id"]: c for c in grounded["kept"]}
    material_grounded = [
        (grounded_by_id[c["id"]], alert) for c, alert in pending
        if c["id"] in kept_ids and not validation_errors(grounded_by_id[c["id"]])
    ]

    new_claims, new_alerts = _apply_updates(
        claims, material_grounded, meta.get("alerted_fingerprints", []))

    result["material"] = [
        {"subject_key": c["subject_key"], "alert": a} for c, a in material_grounded]
    result["alerts"] = new_alerts

    if write and new_alerts:
        meta["last_checked"] = checked_at
        meta.setdefault("alerted_fingerprints", []).extend(a["fingerprint"] for a in new_alerts)
        current_md = format_report(clean_output(
            claims_to_markdown(new_claims, _title(meta),
                               my_company=meta.get("my_company"), competitor=meta.get("competitor"))))
        store.write_baseline(slug, new_claims, meta, current_md)
        _append_alerts(slug, new_alerts)
    elif write:  # candidates existed but nothing survived as material -> bump timestamp only
        meta["last_checked"] = checked_at
        store.write_baseline(slug, claims, meta, _current_md(slug))
    return result


def _current_md(slug):
    path = os.path.join(store.battlecard_dir(slug), "current.md")
    return open(path).read() if os.path.exists(path) else ""


def _append_alerts(slug, alerts):
    d = store.battlecard_dir(slug)
    with open(os.path.join(d, "alerts.jsonl"), "a") as f:
        for a in alerts:
            f.write(json.dumps(a, ensure_ascii=False) + "\n")
    with open(os.path.join(d, "alerts.md"), "a") as f:
        for a in alerts:
            f.write(f"- **{a['date']} — {a['headline']}** ({a['subject_key']}): "
                    f"{a['old_value']} → {a['new_value']}. **So what:** {a['so_what']}\n")


def _latest_passed_anchor(now: datetime) -> datetime | None:
    """The most recent daily anchor instant at or before `now`, or None if anchors
    are disabled. Anchors are wall-clock UTC times (config.MONITOR_ANCHORS_UTC);
    last_checked is written naive-UTC (datetime.now() on the UTC Actions runner), so
    comparing against naive anchors built from `now` is apples-to-apples. If `now`
    is before today's first anchor, the latest passed one is yesterday's last."""
    anchors = []
    for a in config.MONITOR_ANCHORS_UTC:
        h, m = a.split(":")
        anchors.append((int(h), int(m)))
    if not anchors:
        return None
    anchors.sort()
    todays = [now.replace(hour=h, minute=m, second=0, microsecond=0) for h, m in anchors]
    passed = [t for t in todays if t <= now]
    if passed:
        return max(passed)
    h, m = anchors[-1]
    return (now - timedelta(days=1)).replace(hour=h, minute=m, second=0, microsecond=0)


def _is_due(meta: dict, now: datetime | None = None) -> bool:
    """Window-anchored due-gate (the launch promise): a card is due when it hasn't
    been checked since the most recent passed anchor (config.MONITOR_ANCHORS_UTC =
    7am + 1pm ET). This anchors freshness to the clock so updates land in the morning
    + midday windows and never drift. Cards with no last_checked, or an unparseable
    timestamp, are always due (fail toward checking).

    Legacy fallback: with anchors disabled (MONITOR_ANCHORS_UTC empty), reverts to the
    per-competitor relative cadence gate (due when cadence_hours have elapsed)."""
    now = now or datetime.now()
    raw = meta.get("last_checked") or meta.get("baseline_date")
    if not raw:
        return True
    try:
        last = datetime.fromisoformat(raw)
    except ValueError:
        return True
    anchor = _latest_passed_anchor(now)
    if anchor is not None:
        return last < anchor
    cadence_hours = meta.get("cadence_hours") or config.DEFAULT_CADENCE_HOURS
    return now >= last + timedelta(hours=cadence_hours)


def run_all(write: bool = True, send: bool = True, email_dry_run: bool = True,
            force: bool = False) -> list[dict]:
    """Cron entrypoint: check every DUE battlecard, write per policy, email digests.

    Due-gate (_is_due): by default a card is only checked when it hasn't been checked
    since the most recent passed anchor (7am + 1pm ET), so the burst cron lands one
    check in the morning window and one at midday without drift. `force=True` ignores
    the gate (manual runs).

    Side-effects gated for safety: write=False computes without mutating the store; email
    is dry unless email_dry_run=False AND creds are configured. The Actions cron runs live
    (SCOUT_MONITOR_LIVE=1); git commit/push is done by the workflow, not here.
    """
    from scout.display import list_battlecards
    from scout import notify

    summary = []
    for slug in list_battlecards():
        meta = store.load_meta(slug) or {}
        # Showcase cards can opt out of monitoring (e.g. the Batman vs Superman
        # stress-test card): it still renders in the viewer but never burns a check.
        if meta.get("monitored") is False:
            summary.append({"slug": slug, "skipped": "not monitored"})
            continue
        if not force and not _is_due(meta):
            summary.append({"slug": slug, "skipped": "not due",
                            "cadence_hours": meta.get("cadence_hours") or config.DEFAULT_CADENCE_HOURS,
                            "last_checked": meta.get("last_checked")})
            continue
        res = check(slug, write=write)
        emailed = None
        if send and res["alerts"]:
            meta = store.load_meta(slug) or {}
            emailed = notify.send_digest(meta.get("competitor"), res["alerts"], dry_run=email_dry_run)
        cost = res["cost"]
        summary.append({
            "slug": slug, "no_change": res["no_change"], "material": len(res["material"]),
            "alerts": len(res["alerts"]),
            "cost_usd": round((cost.get("triage") or 0) + (cost.get("materiality") or 0), 4),
            "emailed": emailed,
        })
    return summary


def _print_check(res):
    cost = res["cost"]
    total = (cost.get("triage") or 0) + (cost.get("materiality") or 0)
    print(f"\n=== monitor.check({res['slug']}) since {res['since']} ===")
    print(f"no_change={res['no_change']}  candidates={res['candidates']}  "
          f"substantial={res.get('substantial', 0)}  escalated={res.get('substantial', 0) > 0}  "
          f"material={len(res['material'])}")
    print(f"cost: triage=${cost.get('triage')}  materiality=${cost.get('materiality')}  TOTAL=${total:.4f}")
    for m in res["material"]:
        a = m["alert"]
        print(f"  MATERIAL {m['subject_key']}: {a.get('old_value')} -> {a.get('new_value')}  | {a.get('so_what','')[:80]}")


if __name__ == "__main__":
    import json as _json
    # LIVE only when the cron sets SCOUT_MONITOR_LIVE=1: then write the store and send real
    # email. Default (any other context) is fully dry: compute, no writes, no email sent.
    live = os.environ.get("SCOUT_MONITOR_LIVE") == "1"
    out = run_all(write=live, send=True, email_dry_run=not live)
    print(_json.dumps(out, indent=2, default=str))
