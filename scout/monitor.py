"""Monitoring engine (v2 build step 4): subject-key-centric, date-scoped change detection.

Loop (spec §6): load baseline -> date-scoped retrieve -> cheap TRIAGE gate (Sonnet) ->
Opus MATERIALITY judgment -> in-place update claims + alerts + dedup -> advance last_checked.

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
from datetime import date

from claude_agent_sdk import ClaudeAgentOptions

from scout import config, store
from scout.fetch_tool import FETCH_SERVER, FETCH_TOOL_NAME, reset_log
from scout.generate import _drive, _extract_json
from scout.grounding import ground_claims
from scout.render import claims_to_markdown, clean_output, format_report
from scout.schema import claim_id, pregrounding_errors, validation_errors

MATERIAL_CATEGORIES = (
    "funding, M&A, exec hire/departure, pricing/packaging change, major product launch, "
    "security incident/breach, legal action, partnership shift, public strategy change"
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


# --- Stage 1: cheap triage gate ----------------------------------------------
_TRIAGE_SYSTEM = f"""You are a monitoring TRIAGE GATE for a living competitive-intelligence
battlecard. Your job is to cheaply surface ONLY developments that are (a) genuinely NEW since a
cutoff date AND (b) NOT already reflected in the tracked claims — so the expensive downstream judge
is never re-run on stale or already-known information. You do NOT make the final materiality call.

Do a FEW date-scoped WebSearches for the competitor's recent news. Material categories:
{MATERIAL_CATEGORIES}.

Apply TWO STRICT FILTERS before surfacing anything (this is the whole point of the gate):
1. DATE: surface a development ONLY if it is dated ON OR AFTER the cutoff date given. Discard older
   items even if they appear in results — anything before the cutoff is already covered by the baseline.
2. ALREADY-CAPTURED (note: "already-captured", NOT merely "already-mentioned"): you are given the
   tracked claims with their CURRENT values — this is the OLD state. Surface a candidate if the
   development reports a DIFFERENT value or status than the tracked claim currently states (a changed
   metric, a new CEO, a price change, a strategy reversal, a product superseded) OR concerns a subject
   not in the list at all. Catching when reality has MOVED PAST the tracked value is your MAIN job, so
   do NOT drop a candidate merely because its subject/topic appears in the tracked list. Drop a
   candidate ONLY when a tracked claim ALREADY states this exact development (the update would be a
   no-op). When unsure whether something is genuinely new, SURFACE IT — the downstream judge decides
   importance; MISSING A REAL CHANGE IS WORSE than paying for one extra check.

Your filter is novelty (dated on/after the cutoff) + non-redundancy (the tracked claim does not already
state this exact development) — NOT importance, and NOT "is the subject tracked". Bias toward recall.

Return ONLY a single fenced ```json block:
{{"has_candidates": <bool>, "candidates": [
  {{"signal": "<one line, INCLUDING the development's date>", "subject_key": "<matching tracked subject_key, or NEW>",
    "why_new": "<why this is new since the cutoff AND not already in the tracked claims>",
    "source_hint": "<url or outlet>"}} ]}}
If nothing passes BOTH filters, return has_candidates=false and an empty list — that is the common,
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
        max_turns=config.MAX_TURNS,
        max_budget_usd=config.MAX_BUDGET_USD,
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
    for claim, alert in material_grounded:
        fp = _fingerprint(claim["subject_key"], str(alert.get("new_value", claim.get("claim", ""))))
        if fp in seen:
            continue  # already alerted this exact subject->value transition
        seen.add(fp)
        by_id[claim["id"]] = claim  # in-place revise (same id) or add net-new
        new_alerts.append({
            "date": date.today().isoformat(),
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
    since = since_override or meta.get("last_checked") or meta.get("baseline_date")
    today = date.today().isoformat()
    reset_log()

    # Stage 1: triage (cheap)
    triage = asyncio.run(_run_triage(meta, since, claims))
    try:
        tdata = _extract_json(triage["text"])
    except Exception:
        tdata = {"has_candidates": False, "candidates": []}
    candidates = tdata.get("candidates", []) if tdata.get("has_candidates") else []

    result = {
        "slug": slug, "since": since, "no_change": not candidates,
        "candidates": len(candidates), "material": [], "alerts": [],
        "cost": {"triage": triage.get("cost_usd"), "materiality": 0.0},
        "last_checked": today,
    }

    if not candidates:
        # No-change run: advance last_checked only (cheap timestamp bump).
        if write:
            meta["last_checked"] = today
            store.write_baseline(slug, claims, meta, _current_md(slug))
        return result

    # Stage 2: materiality (Opus) on candidates only
    mat = asyncio.run(_run_materiality(meta, since, candidates, claims))
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
        meta["last_checked"] = today
        meta.setdefault("alerted_fingerprints", []).extend(a["fingerprint"] for a in new_alerts)
        current_md = format_report(clean_output(
            claims_to_markdown(new_claims, _title(meta),
                               my_company=meta.get("my_company"), competitor=meta.get("competitor"))))
        store.write_baseline(slug, new_claims, meta, current_md)
        _append_alerts(slug, new_alerts)
    elif write:  # candidates existed but nothing survived as material -> bump timestamp only
        meta["last_checked"] = today
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


def run_all(write: bool = True, send: bool = True, email_dry_run: bool = True) -> list[dict]:
    """Cron entrypoint: check EVERY tracked battlecard, write per policy, email digests.

    Side-effects gated for safety: write=False computes without mutating the store; email
    is dry unless email_dry_run=False AND creds are configured. The Actions cron runs live
    (SCOUT_MONITOR_LIVE=1); git commit/push is done by the workflow, not here.
    """
    from scout.display import list_battlecards
    from scout import notify

    summary = []
    for slug in list_battlecards():
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
    print(f"no_change={res['no_change']}  candidates={res['candidates']}  material={len(res['material'])}")
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
