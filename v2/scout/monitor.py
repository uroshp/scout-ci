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
import sys
from datetime import datetime, timedelta

from claude_agent_sdk import ClaudeAgentOptions

from scout import config, selfserve, shadow, store, strengths
from scout.fetch_tool import FETCH_SERVER, FETCH_TOOL_NAME, reset_log
from scout.generate import _drive, _extract_json, _build_retry_payload, _run_retry
from scout.propagate import propagate, apply_ops
from scout.grounding import CUT_ABSENT, ground_claims, is_excluded_source
from scout.prompts import WRITING_STYLE
from scout.render import claims_to_markdown, clean_output, extract_cut_log, format_report
from scout.schema import ANCHOR_SECTION, SOURCE_TIERS, claim_id, pregrounding_errors, validation_errors

# Source-tier preference order for multi-source grounding (best first): a primary filing /
# company release beats reputable secondary reporting, which beats sentiment-only.
_TIER_RANK = {tier: i for i, tier in enumerate(SOURCE_TIERS)}

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


def _is_mine(c: dict, me: str | None) -> bool:
    """A candidate is my_company-side when triage tags it with the literal 'my_company' OR with
    the company's actual name — the 2026-07-01 runs tagged the same Anthropic story both ways,
    and the name-tagged one silently routed through the competitor arm."""
    about = str(c.get("about", "")).strip().lower()
    return about == "my_company" or bool(me) and about == str(me).strip().lower()


def _escalation_floor(candidates: list[dict], claims: list[dict]) -> None:
    """Deterministic substantial floor (the 2026-07-01 Fable-lift miss): a surfaced candidate that
    names a TRACKED subject already passed triage's already-captured filter — it reports a
    DIFFERENT value/status for a subject on the card. Whether that flip is material is the Opus
    judge's call, never the cheap triage grade's (control-vs-model: the subject match is checkable
    in code, so code forces the escalation; the model keeps the materiality judgment)."""
    tracked = {str(c.get("subject_key")) for c in claims}
    for c in candidates:
        if not c.get("substantial") and str(c.get("subject_key") or "") in tracked:
            c["substantial"] = True
            c["escalated_by"] = "tracked_subject_floor"


def _clear_window(meta: dict) -> None:
    meta.pop("unresolved_since", None)
    meta.pop("unresolved_attempts", None)
    meta.pop("unresolved_subjects", None)


def _hold_window(meta: dict, since: str | None, result: dict) -> None:
    """Keep the detection window open for another scan, bounded by
    MONITOR_MAX_UNRESOLVED_RETRIES; at the bound, abandon LOUDLY (surfaced in the run result) —
    a held miss may go unfixed, but it must never go unnoticed."""
    attempts = (meta.get("unresolved_attempts") or 0) + 1
    if attempts < config.MONITOR_MAX_UNRESOLVED_RETRIES:
        meta["unresolved_since"] = since
        meta["unresolved_attempts"] = attempts
        result["unresolved_held"] = {"since": since, "attempt": attempts}
    else:
        result["abandoned_window"] = {
            "since": since, "subjects": meta.get("unresolved_subjects") or []}
        _clear_window(meta)


def _resolve_or_hold(meta: dict, new_alerts: list[dict], result: dict) -> None:
    """WINDOW-CLOSE FIX (the 2026-07-01 permanent miss): an alert resolves a held window ONLY when
    it matches a subject the window was held FOR. An unrelated catch (the Copilot alert) must not
    erase a pending act-grade miss (the Fable lift) — unmatched and legacy windows (no stored
    subjects, so nothing can match) stay open, bounded as ever."""
    held = meta.get("unresolved_since")
    if not held:
        return
    held_subjects = set(meta.get("unresolved_subjects") or [])
    if held_subjects & {str(a.get("subject_key")) for a in new_alerts}:
        _clear_window(meta)
        result["unresolved_resolved"] = {"since": held}
    else:
        _hold_window(meta, held, result)


# --- Stage 1: cheap triage gate ----------------------------------------------
_TRIAGE_SYSTEM = f"""You are a monitoring TRIAGE GATE for a living competitive-intelligence
battlecard. You run on EVERY check and most windows are quiet, so you must be CHEAP and decisive.
Your job: surface developments that are (a) genuinely NEW since a cutoff date AND (b) NOT already
reflected in the tracked claims, and decide whether ANY of them is SUBSTANTIAL enough to justify
the expensive downstream judge. Do a SMALL number of searches and STOP.

Do AT MOST {config.TRIAGE_MAX_SEARCHES} date-scoped WebSearches, then DECIDE — do not keep
searching. Scan BOTH sides when a my_company is given: the COMPETITOR's recent news AND your own
company's (my_company's) recent news, splitting the limited searches across the two (favor the
competitor, but never skip my_company). With no my_company given, scan the competitor only.
When scanning my_company, spend one of its searches on the company's OWN newsroom / official
announcements ("<my_company> announces", or site:<their official domain>) — an official
primary-source announcement is exactly the my_company story a generic news query misses
(the 2026-07-01 Fable-lift miss: the lift was on the company's own site and every major outlet,
but the one generic search returned only an aggregator).
Material categories:
{MATERIAL_CATEGORIES}.

Deliberately hunt ADVERSE / competitive-threat signals, not just announcements and wins —
cancellations, customer losses, churn, budget caps, outages, layoffs, lawsuits are exactly the
high-value developments to surface. Anchor on reputable NEWS outlets; never treat Wikipedia, a
wiki, an encyclopedia, or a promo/SEO listicle as a source.

This card has TWO sides, and a deal-moving development can come from either. A competitor's strong
move, OR our OWN stumble (a product pulled, an outage, a price hike, a security incident), puts us
on the BACK FOOT and is exactly as material as a competitor stumble or our own win that puts us on
the FRONT FOOT. Hunt both. Our own bad news is the case a competitor-only scan misses, so do not
under-weight it. Tag every candidate with whether it is about the competitor or my_company.

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
            A STATUS FLIP on a tracked subject (restricted → lifted, gated → generally available,
            launched → discontinued, beta → GA) is ALWAYS substantial — INCLUDING when it "merely
            executes" or "implements" a change a tracked claim already signaled as planned, partial,
            or ongoing. The execution of a signaled change IS the news, never a restatement.
  - false — incremental or routine: a minor feature, a blog/opinion post, a restated known fact, a
            rumor without a source, sentiment churn, or anything whose importance is marginal.

PRICING / PACKAGING is high-value and easy to miss among announcements: a new list price, a new or
retired tier, a discount, a usage-limit or rate change, a billing-model shift. Actively check for it on
BOTH sides — a pricing move on either party changes what a rep says on cost, and a card can carry a
pricing section that never updates if the scan skips it.

ESCALATION RULE (this governs cost): the expensive judge runs ONLY if at least one candidate is
"substantial": true. A quiet window — nothing new, or only minor/routine items — is the COMMON,
correct, CHEAP outcome: report what you found and the pipeline stops here. Reserve
"substantial": true for genuinely notable news; do NOT mark marginal items substantial "to be
safe" — a missed minor item is simply re-checked next day. Keep the routine check cheap.

Return ONLY a single fenced ```json block:
{{"has_candidates": <bool>, "candidates": [
  {{"signal": "<one line, INCLUDING the development's date>", "subject_key": "<matching tracked subject_key, or NEW — NEVER omit this field; use NEW only when no tracked subject fits>",
    "about": "<competitor | my_company — whose development this is>",
    "valence": "<front_foot = good for us / bad for them | back_foot = bad for us / good for them>",
    "substantial": <bool>,
    "why_new": "<why this is new since the cutoff AND not already in the tracked claims>",
    "source_hint": "<url or outlet>"}} ]}}
If nothing passes the filters, return has_candidates=false and an empty list — that is the common,
correct, cheap outcome on a quiet window."""


async def _run_triage(meta, since, claims):
    comp, me = meta.get("competitor"), meta.get("my_company")
    scope = (f"Scan BOTH sides and tag each candidate's 'about': the competitor {comp} AND your own "
             f"company {me}." if me else f"Scan the competitor {comp}.")
    user = (f"Competitor: {comp}" + (f" (we are {me})" if me else "") +
            f"\n{scope}"
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
(same contract as generation: subject_key, claim, claim_type [fact|interpretation|sentiment — an
IPO/M&A/launch/exec-move is a "fact"; NEVER invent values like "competitive_move"], section [executive_summary|snapshot|
recent_moves|positioning|pricing|battlecard|sentiment|objection_handling], zone [battlecard only,
else null], order, source_url, source_tier [primary|reputable_secondary|sentiment_only],
evidence_excerpt [VERBATIM from fetch_page's real page text], as_of [YYYY-MM-DD], confidence)
AND an alert. If the change updates an existing tracked subject, REUSE that subject_key EXACTLY so
it updates in place; if genuinely new, use a fresh subject_key in the same style.

Do NOT include id/verified/grounding (filled downstream). Every alert MUST carry a "so_what" — the
decision it changes — or the item is NOT material. Every alert also carries a "severity":
"act" when the change demands reps change what they SAY or DO in live deals NOW (a price change,
a launch that moves a battlecard zone, a differentiator gained/lost, a breaking risk);
"watch" when it is material context but changes no rep behavior yet (an early signal, a capacity
datapoint, exec commentary, a roadmap announcement with no shipped product).
OUTAGES ARE NOT AUTOMATICALLY "act": a routine or PARTIAL/single-region cloud-provider outage is
"watch" — those happen constantly and do not move a deal. Only a BROAD or SUSTAINED outage (multi-region
/ global, prolonged, or itself major news) changes what a rep says in a live deal and rates "act". A real
recurring pattern surfaces as major news on its own and is grounded from that source next cycle.

MULTI-SOURCE (this is how a claim survives grounding — do it for EVERY material change): find
EVERY credible source for the development, then RANK them by source tier — primary (SEC/EDGAR
filing, the company's own 8-K / press release / blog announcement, a court document) outranks
reputable_secondary (Reuters, Bloomberg, The Information, CNBC, TechCrunch, a major outlet).
DISCARD anything low-tier: sentiment_only sources, AND any wiki/encyclopedia/promo/SEO listicle/
aggregator (a deterministic check cuts those, so sending one just loses the claim). From what
remains, emit the TOP 2-3 — HIGHEST TIER FIRST — as a "candidate_sources" array, each entry
{source_url, source_tier, evidence_excerpt}. Set the claim's top-level source_url / source_tier /
evidence_excerpt to candidate #1 (the highest-tier source). Grounding will independently re-fetch
each candidate in tier order and keep the best one that verifies, so 2-3 good sources make a true
claim robust to one paywalled/flaky page.

EXCERPTS: every evidence_excerpt (top-level AND each candidate) MUST be copied VERBATIM,
character-for-character, from THAT page's real text as fetch_page returns it — never paraphrased,
never from memory — and SHORT: a single sentence, ideally <=160 chars, so the independent re-fetch
can confirm it. Never list a source you did not actually fetch and read. An ADVERSE development
(cancellation, churn, loss) should trace to independent reporting, not only the affected company.

Return ONLY a single fenced ```json block (the claim object includes "candidate_sources"):
{"material": [ {"claim": { ...claim object... },
               "alert": {"old_value": "<prior, or null if new>", "new_value": "<now>",
                         "headline": "<one line>", "so_what": "<the decision it changes>",
                         "severity": "act|watch"}} ],
 "immaterial": [ {"signal": "<...>", "why_not": "<...>"} ]}"""


async def _run_materiality(meta, since, candidates, claims):
    comp, me = meta.get("competitor"), meta.get("my_company")
    user = (f"Competitor: {comp}" + (f" (we are {me})" if me else "") +
            f"\nChanges SINCE {since}.\n\nTRACKED SUBJECTS (subject_key — current value):\n"
            + _tracked_digest(claims) +
            "\n\nCANDIDATE SIGNALS FROM TRIAGE:\n" + json.dumps(candidates, ensure_ascii=False))
    options = ClaudeAgentOptions(
        model=config.ORCHESTRATOR_MODEL,
        system_prompt={"type": "preset", "preset": "claude_code",
                       "append": _MATERIALITY_SYSTEM + "\n\n" + WRITING_STYLE},
        mcp_servers={"scoutfetch": FETCH_SERVER},
        allowed_tools=["WebSearch", FETCH_TOOL_NAME],
        disallowed_tools=["WebFetch"],
        permission_mode="bypassPermissions",
        max_turns=config.MAX_TURNS,
        max_budget_usd=config.MAX_BUDGET_USD,
    )
    return await _drive(user, options, "materiality")


def _candidate_variants(claim: dict) -> list[dict]:
    """Tier-ranked, deduped, usable source candidates for one claim (best first, max 3).
    Uses the judge's candidate_sources when present, else falls back to the single anchor.
    Drops sources that are unusable up front: missing fields, excluded (wiki/encyclopedia),
    or sentiment_only under a 'fact' (the schema forbids it anyway)."""
    raw = claim.get("candidate_sources") or [{
        "source_url": claim.get("source_url"), "source_tier": claim.get("source_tier"),
        "evidence_excerpt": claim.get("evidence_excerpt")}]
    clean, seen = [], set()
    for s in raw:
        s = s or {}
        url, tier, ex = s.get("source_url"), s.get("source_tier"), s.get("evidence_excerpt")
        if not (url and tier and ex) or tier not in _TIER_RANK:
            continue
        if is_excluded_source(url) or url in seen:
            continue
        if claim.get("claim_type") == "fact" and tier == "sentiment_only":
            continue
        seen.add(url)
        clean.append({"source_url": url, "source_tier": tier, "evidence_excerpt": ex})
    clean.sort(key=lambda s: _TIER_RANK[s["source_tier"]])  # primary first
    return clean[:3]


def _ground_best(claims: list[dict]) -> dict:
    """Multi-source grounding (lever 4): for each claim, independently re-fetch its tier-ranked
    candidate sources and KEEP THE HIGHEST-TIER ONE THAT GROUNDS, demoting the rest to
    corroboration. Falls back to single-anchor behavior when the judge supplied no candidates.
    Same {kept, failed, cut} contract as grounding.ground_claims so check()'s retry round can
    consume it unchanged. A claim only enters `failed` when EVERY candidate failed to ground."""
    kept, failed, cut = [], [], []
    for claim in claims:
        variants = _candidate_variants(claim)
        # Build one grounding-ready variant per candidate (candidate_sources is transient — it is
        # NOT in the claim schema, so it must never reach ground_claims or validation).
        vclaims = []
        for v in variants:
            vc = {k: val for k, val in claim.items() if k != "candidate_sources"}
            vc.update(source_url=v["source_url"], source_tier=v["source_tier"],
                      evidence_excerpt=v["evidence_excerpt"])
            vclaims.append(vc)
        if not vclaims:  # no usable source at all — let ground_claims emit the failure record
            vclaims = [{k: val for k, val in claim.items() if k != "candidate_sources"}]
        g = ground_claims(vclaims)
        if g["kept"]:
            best = min(g["kept"], key=lambda c: _TIER_RANK.get(c.get("source_tier"), 99))
            # Demote the OTHER candidate sources (whatever their fate) to corroboration pointers.
            corro = [c for c in (claim.get("corroboration") or [])
                     if c.get("source_url") != best["source_url"]]
            for v in variants:
                if v["source_url"] == best["source_url"]:
                    continue
                corro.append({"source_url": v["source_url"], "source_tier": v["source_tier"],
                              "note": "tier-ranked alternate source for the same development",
                              "grounded": False})
            if corro:
                best["corroboration"] = corro[:5]
            kept.append(best)
        else:  # every candidate failed — surface ONE failure (the top-tier try) for the retry round
            failed.append(g["failed"][0] if g["failed"]
                          else {"claim": vclaims[0], "status": "absent", "reason": CUT_ABSENT})
            if g["cut"]:
                cut.append(g["cut"][0])
    return {"kept": kept, "failed": failed, "cut": cut}


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
            # Normalized in code (deterministic): anything but a clean "act" demotes to "watch",
            # so a judge hiccup can only under-flag, never invent urgency.
            "severity": "act" if str(alert.get("severity", "")).strip().lower() == "act" else "watch",
            "source_url": claim.get("source_url"),
            "fingerprint": fp,
        })
    return list(by_id.values()), new_alerts


# --- my_company arm: ground OUR OWN developments into tracked_facts anchors (propagation §17) ---
_MY_FACTS_SYSTEM = f"""You GROUND our own company's (my_company's) recent developments into tracked
FACTS for a living competitive battlecard. Triage flagged candidate developments about US — our side,
NOT the competitor. For each, decide if it is genuinely MATERIAL, then emit a GROUNDED FACT describing
the development exactly as the source states it. You do NOT write objections or plays; downstream
propagation turns these facts into rep-facing prose. The section is ALWAYS "{ANCHOR_SECTION}".

FACTS ONLY, CONSEQUENCE-COMPLETE — the cardinal rule. Ground what the SOURCE actually states,
INCLUDING the full consequence the announcement itself reports. If our company announced it paused or
pulled a product for ALL users, ground "paused for all users" when the announcement says so — do NOT
shrink it to the narrower trigger (e.g. a government order's "foreign nationals" wording) when our own
announcement states a broader pull. EQUALLY, never INFER a broader consequence the source does not
state: read our company's actual announcement and ground exactly what it says, no more, no less. An
ungrounded downstream consequence is left for a later pass to ground, never bridged by speculation.

For each MATERIAL development use fetch_page to READ the source, then emit the fact as a claim object
(subject_key, claim, claim_type:"fact", section:"{ANCHOR_SECTION}", zone:null, order, source_url,
source_tier [primary|reputable_secondary], evidence_excerpt [VERBATIM from fetch_page], as_of, confidence)
plus an alert. REUSE an existing subject_key if this updates a tracked development in place.

MULTI-SOURCE (so the fact survives grounding): find every credible source, RANK by tier (primary —
our 8-K / press release / blog announcement, a court/government document — outranks reputable_secondary
news), DISCARD wiki/encyclopedia/SEO/sentiment sources, and emit the top 2-3 HIGHEST-TIER-FIRST as a
"candidate_sources" array, each {{source_url, source_tier, evidence_excerpt}}. Set the top-level
source_url/source_tier/evidence_excerpt to candidate #1. Every excerpt copied VERBATIM, character-for-
character, SHORT (<=160 chars), from the real fetched page — never paraphrased, never from memory.

severity: "act" when this changes what reps SAY or DO in live deals NOW (our product pulled or
restricted, our price hike, our security incident, a major customer loss); "watch" for
material context that changes no rep behavior yet. An outage is "act" ONLY if BROAD or SUSTAINED
(multi-region / global, prolonged, or itself major news); a routine or partial/single-region outage is
"watch" — they happen constantly and do not move a deal.

Return ONLY a single fenced ```json block:
{{"facts": [ {{"claim": {{ ...claim object incl. candidate_sources... }},
              "alert": {{"old_value": "<prior or null>", "new_value": "<now>", "headline": "<one line>",
                        "so_what": "<the decision it changes>", "severity": "act|watch"}} }} ],
 "immaterial": [ {{"signal": "<...>", "why_not": "<...>"}} ]}}"""


async def _run_my_facts(meta, since, candidates, claims):
    comp, me = meta.get("competitor"), meta.get("my_company")
    user = (f"We are {me} (competing against {comp}).\nOUR developments SINCE {since}.\n\n"
            "TRACKED SUBJECTS (subject_key — current value):\n" + _tracked_digest(claims) +
            "\n\nCANDIDATE OWN-SIDE SIGNALS FROM TRIAGE:\n" + json.dumps(candidates, ensure_ascii=False))
    options = ClaudeAgentOptions(
        model=config.ORCHESTRATOR_MODEL,
        system_prompt={"type": "preset", "preset": "claude_code",
                       "append": _MY_FACTS_SYSTEM + "\n\n" + WRITING_STYLE},
        mcp_servers={"scoutfetch": FETCH_SERVER},
        allowed_tools=["WebSearch", FETCH_TOOL_NAME],
        disallowed_tools=["WebFetch"],
        permission_mode="bypassPermissions",
        max_turns=config.MAX_TURNS,
        max_budget_usd=config.MAX_BUDGET_USD,
    )
    return await _drive(user, options, "my_facts")


def _my_company_facts(slug, meta, since, my_substantial, claims):
    """Ground our own (my_company) substantial signals into tracked_facts anchor facts. Returns
    {'grounded': [(fact, alert), ...], 'cost': float}. SECTION is forced to the anchor section in
    code so a prompt slip can't file our news into a rendered section. Mirrors the competitor
    materiality+grounding path (multi-source, verbatim excerpt, _ground_best) but emits FACTS only —
    propagation authors the rep-facing prose."""
    mat = asyncio.run(_run_my_facts(meta, since, my_substantial, claims))
    try:
        mdata = _extract_json(mat["text"])
    except Exception:
        mdata = {"facts": []}
    alert_by_id, to_ground = {}, []
    for m in (mdata.get("facts") or []):
        c = m.get("claim")
        if not isinstance(c, dict) or "subject_key" not in c:
            continue
        c["section"], c["zone"] = ANCHOR_SECTION, None      # force the anchor home in code
        c["claim_type"] = "fact"
        c["id"] = claim_id(slug, str(c["subject_key"]))
        c["verified"] = True
        cand = _candidate_variants(c)
        if cand and not c.get("source_url"):
            c.update(source_url=cand[0]["source_url"], source_tier=cand[0]["source_tier"],
                     evidence_excerpt=cand[0]["evidence_excerpt"])
        if pregrounding_errors({k: v for k, v in c.items() if k != "candidate_sources"}):
            continue
        alert_by_id[c["id"]] = m.get("alert", {})
        to_ground.append(c)
    grounded = _ground_best(to_ground)
    kept = [c for c in grounded["kept"] if not validation_errors(c)]
    pairs, seen = [], set()
    for c in kept:
        if c["id"] in seen or c["id"] not in alert_by_id:
            continue
        seen.add(c["id"])
        pairs.append((c, alert_by_id[c["id"]]))
    return {"grounded": pairs, "cost": mat.get("cost_usd")}


def _is_act(alert: dict) -> bool:
    return str(alert.get("severity", "")).strip().lower() == "act"


def _retire_feed_alerts(confirmed_ops: list, applied: list) -> list:
    """Build alert records for APPLIED retire ops so the left updates panel shows the removal (the
    router's feed_note), never a silent disappearance — the user's explicit ask ("even if it's in the
    updates section on the left"). Only ops that actually landed are surfaced."""
    applied_retire_sks = {a.get("subject_key") for a in applied if a.get("operation") == "retire"}
    now = datetime.now()
    out = []
    for op in confirmed_ops:
        if op.get("operation") != "retire":
            continue
        sk = op.get("subject_key") or op.get("target_subject_key")
        if sk not in applied_retire_sks:
            continue
        note = op.get("feed_note") or op.get("retired_reason") or "a play or objection was removed"
        out.append({
            "date": now.date().isoformat(),
            "detected_at": now.isoformat(timespec="seconds"),
            "subject_key": sk,
            "old_value": "on the card", "new_value": "removed",
            "headline": note,
            "so_what": note,
            "severity": "act",
            "source_url": None,
            "fingerprint": _fingerprint(str(sk), "retired:" + str(note)),
        })
    return out


def _competitor_arm(slug, meta, since, substantial, claims, result):
    """Competitor materiality (Opus) -> tier-ranked MULTI-SOURCE grounding -> bounded feedback
    retry -> material_grounded. Logic is UNCHANGED from the pre-my_company flow; extracted verbatim
    so check() can run it conditionally now that the my_company arm can fire on its own. Returns
    (material_grounded, grounded)."""
    mat = asyncio.run(_run_materiality(meta, since, substantial, claims))
    result["cost"]["materiality"] = mat.get("cost_usd")
    try:
        mdata = _extract_json(mat["text"])
    except Exception:
        mdata = {"material": []}
    alert_by_id, to_ground = {}, []
    for m in (mdata.get("material") or []):
        c = m.get("claim")
        if not isinstance(c, dict) or "subject_key" not in c:
            continue
        c["id"] = claim_id(slug, str(c["subject_key"]))
        c["verified"] = True
        cand = _candidate_variants(c)
        if cand and not c.get("source_url"):
            c.update(source_url=cand[0]["source_url"], source_tier=cand[0]["source_tier"],
                     evidence_excerpt=cand[0]["evidence_excerpt"])
        if pregrounding_errors({k: v for k, v in c.items() if k != "candidate_sources"}):
            continue
        alert_by_id[c["id"]] = m.get("alert", {})
        to_ground.append(c)

    grounded = _ground_best(to_ground)
    kept = [c for c in grounded["kept"] if not validation_errors(c)]
    failed = grounded.get("failed", [])
    if failed:
        rr = asyncio.run(_run_retry(_build_retry_payload(failed)))
        result["cost"]["materiality"] = (result["cost"]["materiality"] or 0) + (rr.get("cost_usd") or 0)
        try:
            rdata = _extract_json(rr["text"])
        except Exception:
            rdata = {"revised": []}
        revised = []
        for c in rdata.get("revised", []):
            if not isinstance(c, dict) or "subject_key" not in c:
                continue
            c["id"] = claim_id(slug, str(c["subject_key"]))
            c["verified"] = True
            if not pregrounding_errors({k: v for k, v in c.items() if k != "candidate_sources"}):
                revised.append(c)
        reground = _ground_best(revised) if revised else {"kept": []}
        kept += [c for c in reground["kept"] if not validation_errors(c)]

    seen_ids, material_grounded = set(), []
    for c in kept:
        if c["id"] in seen_ids or c["id"] not in alert_by_id:
            continue
        seen_ids.add(c["id"])
        material_grounded.append((c, alert_by_id[c["id"]]))
    return material_grounded, grounded, (mdata.get("immaterial") or [])


def check(slug: str, write: bool = False, since_override: str | None = None) -> dict:
    """One monitoring check. write=False measures without mutating the store (for cost runs).
    since_override forces the detection window (e.g. an old date to simulate a stale baseline)."""
    meta = store.load_meta(slug) or {}
    claims = store.load_claims(slug)
    # Detection window: a HELD `unresolved_since` (a prior substantial item we detected but
    # couldn't ground) takes precedence over last_checked, so we keep re-scanning from that date
    # until the item is captured or the retry bound is hit — last_checked still advances for the
    # due-gate, so this never causes same-window re-escalation storms.
    since = _since_date(since_override or meta.get("unresolved_since")
                        or meta.get("last_checked") or meta.get("baseline_date"))
    checked_at = datetime.now().isoformat(timespec="seconds")  # full timestamp, not just a date
    reset_log()

    # Stage 1: triage (cheap)
    triage = asyncio.run(_run_triage(meta, since, claims))
    try:
        tdata = _extract_json(triage["text"])
    except Exception:
        tdata = {"has_candidates": False, "candidates": []}
    candidates = tdata.get("candidates", []) if tdata.get("has_candidates") else []
    # Deterministic escalation floor: a candidate on a TRACKED subject escalates regardless of the
    # cheap triage grade (the 2026-07-01 Fable-lift miss — triage graded a status flip "minor").
    _escalation_floor(candidates, claims)
    # Strict gate: escalate to the expensive Opus judge ONLY when triage flagged a genuinely
    # SUBSTANTIAL development. Minor/routine candidates are surfaced for the record but do NOT
    # trigger the full pipeline — that's what keeps most checks cheap, triage-only.
    #
    # Dual-scope (step 2): triage now also surfaces my_company-side news. Those are DETECTION-ONLY
    # for now — recorded but NOT escalated to the current materiality/apply path (which only knows
    # how to write competitor-derived claims). The propose/judge propagation steps (spec §17) will
    # route my_company signals into objections/plays; until they land, this gate guarantees
    # dual-scope detection changes NOTHING that reaches a card.
    me = meta.get("my_company")
    comp_candidates = [c for c in candidates if not _is_mine(c, me)]
    substantial = [c for c in comp_candidates if c.get("substantial") is True]
    my_company_signals = [c for c in candidates if _is_mine(c, me)]

    my_substantial = [c for c in my_company_signals if c.get("substantial") is True]
    # The my_company arm is PART of propagation (spec §17): it runs only when propagation is enabled.
    # With PROPAGATE_MODE=off (the default, and production today) do_my is always False, the arm is
    # dead, and everything below is byte-identical to the competitor-only flow.
    do_my = config.PROPAGATE_MODE in ("shadow", "review", "live") and bool(my_substantial)

    result = {
        "slug": slug, "since": since, "no_change": not substantial and not my_substantial,
        "candidates": len(candidates), "substantial": len(substantial),
        "minor_skipped": len(comp_candidates) - len(substantial),
        "my_company_signals": my_company_signals, "my_substantial": len(my_substantial),
        "material": [], "alerts": [],
        "cost": {"triage": triage.get("cost_usd"), "materiality": 0.0},
        "last_checked": checked_at,
    }

    if not substantial and not do_my:
        # Quiet / minor-only window (neither arm has act-able work): triage-only, cheap. Advance
        # last_checked and stop. A held detection window is NOT cleared by one empty re-scan
        # (retrieval variance: the Fable lift was found 1-of-4 runs on the same window) — it stays
        # open, bounded, and abandons loudly at the retry bound.
        if write:
            meta["last_checked"] = checked_at
            if meta.get("unresolved_since"):
                _hold_window(meta, meta["unresolved_since"], result)
            store.write_baseline(slug, claims, meta, _current_md(slug))
        return result

    # COMPETITOR ARM: Opus materiality -> multi-source grounding -> bounded retry. Runs only when
    # competitor signals are substantial; otherwise the my_company arm is why we escalated.
    if substantial:
        material_grounded, grounded, immaterial = _competitor_arm(slug, meta, since, substantial, claims, result)
    else:
        material_grounded, grounded, immaterial = [], {"kept": [], "cut": [], "results": []}, []
    new_claims, new_alerts = _apply_updates(
        claims, material_grounded, meta.get("alerted_fingerprints", []))
    result["material"] = [
        {"subject_key": c["subject_key"], "alert": a} for c, a in material_grounded]

    # MY_COMPANY ARM (propagation §17): ground OUR developments into tracked_facts anchors. In LIVE,
    # persist the anchors (non-rendered) + alert, so the derived objections resolve their source and
    # the retire-cascade can track them; in SHADOW, in-memory only — the decision log is the record.
    my_grounded = []
    if do_my:
        try:
            myf = _my_company_facts(slug, meta, since, my_substantial, claims)
            result["cost"]["my_company"] = myf["cost"]
            my_grounded = myf["grounded"]
            result["my_company_facts"] = [
                {"subject_key": f["subject_key"], "alert": a} for f, a in my_grounded]
            if write and config.PROPAGATE_MODE == "live" and my_grounded:
                new_claims, my_alerts = _apply_updates(
                    new_claims, my_grounded, meta.get("alerted_fingerprints", []))
                new_alerts = new_alerts + my_alerts
        except Exception as e:  # NON-DISRUPTION: the new arm must never break the competitor monitor
            print(f"[monitor] my_company arm skipped ({type(e).__name__}: {e})", file=sys.stderr)
            my_grounded = []
            result["my_company_error"] = f"{type(e).__name__}: {e}"

    result["alerts"] = new_alerts

    # PROPAGATION (spec §17): an ACT-grade grounded change reshapes the rep-facing prose across EVERY
    # affected surface — the step that makes the card *living*. route (Opus, seeded with the materiality
    # verdict) -> author (Sonnet) -> floor -> judge, on act-grade survivors from BOTH arms. Runs AFTER
    # facts are patched into new_claims (so a reshaped play/objection can derive_from a fact now on the
    # card). off -> skip; shadow -> log only; review -> log + email proposals; live -> also apply.
    act_pairs = ([{"fact": c, "alert": a} for c, a in material_grounded if _is_act(a)]
                 + [{"fact": f, "alert": a} for f, a in my_grounded if _is_act(a)])
    if config.PROPAGATE_MODE in ("shadow", "review", "live") and act_pairs:
        try:
            today = checked_at[:10]
            # PIVOT FUEL: grounded my_company STANDING strengths, so a back-foot rebuttal has admissible
            # footing for its pivot (the catch-22 fix; decision-log §12). Deterministic re-grounding, no
            # spend. Passed separately from the change facts: pivot evidence, never a routing trigger.
            strength_facts = strengths.get(slug, meta, new_claims)
            prop = propagate(meta, act_pairs, strength_facts, new_claims, slug=slug,
                             source="monitor", persist=write)
            result["propagation"] = {
                "mode": config.PROPAGATE_MODE, "act_facts": len(act_pairs),
                "strengths": len(strength_facts),
                "surface_ops": len(prop["surface_ops"]), "ops": len(prop["ops"]),
                "confirmed": len(prop["confirmed"]), "no_surface": prop["no_surface"],
                "no_change": prop["no_change"], "decisions": prop["decisions"],
                "run_verdict": prop.get("run_verdict") or {},
                "cost": prop["cost_usd"], "applied": []}
            result["cost"]["propagation"] = sum(v or 0 for v in prop["cost_usd"].values())
            # JUDGE UNAVAILABLE is never silent: the drafts ride the proposals email (run_all), and
            # the run summary carries pipeline_health so a stale card can't look clean.
            unjudged_n = sum(1 for d in prop.get("decisions", [])
                             if d.get("judge_verdict") == "judge_unavailable")
            if unjudged_n:
                result["pipeline_health"] = (
                    f"judge unavailable on {slug}: {unjudged_n} drafted op(s) unverified — "
                    f"see the proposals email; approve manually with allow_unjudged if they hold up")
            # SHADOW-FIRST: only "live" mutates the card. "shadow"/"review" leave it untouched.
            if write and config.PROPAGATE_MODE == "live" and prop["confirmed"]:
                change_facts = [p["fact"] for p in act_pairs]
                # A FALLBACK-judged confirm gates the email only — it never writes the card
                # unattended (the fallback is a weaker model standing in during an outage).
                fb_keys = {(d.get("subject_key"), d.get("operation")) for d in prop["decisions"]
                           if str(d.get("judged_by") or "").startswith("fallback:")}
                auto_ops = [o for o in prop["confirmed"]
                            if (o.get("subject_key"), o.get("operation")) not in fb_keys]
                ap = apply_ops(new_claims, auto_ops, change_facts + strength_facts, slug, today)
                new_claims = ap["claims"]
                result["propagation"]["applied"] = ap["applied"]
                result["propagation"]["skipped"] = ap["skipped"]
                result["propagation"]["held"] = ap.get("held", [])
                # SURFACE RETIREMENTS in the updates feed: an applied retire writes its feed_note as an
                # alert so the left panel shows the removal, never a silent disappearance.
                new_alerts.extend(_retire_feed_alerts(prop["confirmed"], ap["applied"]))
        except Exception as e:  # NON-DISRUPTION: propagation failure must not drop the monitor update...
            print(f"[monitor] propagation FAILED ({type(e).__name__}: {e})", file=sys.stderr)
            result["propagation_error"] = f"{type(e).__name__}: {e}"
            # ...but it must NOT be silent: surface it loudly so a stale card can't look clean (the 7/1
            # miss). run_all folds pipeline_health into the digest email.
            result["pipeline_health"] = f"propagation FAILED on {slug}: {type(e).__name__}: {e}"

    # Shadow-eval observer (v3.5): on a real escalated check, record the champion grounding
    # decisions for offline challenger scoring. No-op unless SCOUT_SHADOW_EVAL=1; never raises.
    if write:
        shadow.capture(slug, "monitor", kept=grounded["kept"], cut=grounded["cut"],
                       grounding=grounded, competitor=meta.get("competitor"),
                       my_company=meta.get("my_company"), focus=meta.get("focus"))
        # DISMISSAL CAPTURE: what this run surfaced but did NOT alert on (triage candidates +
        # materiality immaterial verdicts + own-company signals), so the dismissals are auditable —
        # the silent-miss surface the eval's "never drop anything important" bar cares about.
        shadow.dismissal_capture(
            slug, run_ts=checked_at, candidates=candidates, immaterial=immaterial,
            became_material=[c["subject_key"] for c, _ in material_grounded],
            alerts=new_alerts, my_substantial=my_substantial,
            competitor=meta.get("competitor"), my_company=meta.get("my_company"))

    if write and new_alerts:
        meta["last_checked"] = checked_at
        # A landed alert resolves the held window ONLY if it matches a held subject — an unrelated
        # catch keeps the window open (the 7/1 miss: Copilot's alert erased the Fable window).
        _resolve_or_hold(meta, new_alerts, result)
        meta.setdefault("alerted_fingerprints", []).extend(a["fingerprint"] for a in new_alerts)
        # Regenerating the body from claims drops the Cut Log (it lives only in the
        # markdown, never in the claim store) — carry the existing one forward.
        body = claims_to_markdown(new_claims, _title(meta),
                                  my_company=meta.get("my_company"), competitor=meta.get("competitor"))
        cut_log = extract_cut_log(_current_md(slug))
        if cut_log:
            body = body.rstrip() + "\n\n" + cut_log
        current_md = format_report(clean_output(body))
        store.write_baseline(slug, new_claims, meta, current_md)
        _append_alerts(slug, new_alerts)
    elif write and (substantial or (do_my and not my_grounded)):
        # SUBSTANTIAL development detected on EITHER arm, but nothing landed (competitor: nothing
        # survived grounding+retry; my_company: the arm escalated and grounded nothing). Do NOT
        # lose it: always advance last_checked (keeps the due-gate honest / no same-window storm),
        # but HOLD the detection window open at `since` so the next check re-attempts it — bounded,
        # so a genuinely ungroundable item can't make us re-escalate the Opus judge forever.
        # Record WHICH subjects the window is held for, so only a matching later alert resolves it.
        meta["last_checked"] = checked_at
        failed = substantial + (my_substantial if (do_my and not my_grounded) else [])
        subs = {str(c.get("subject_key")) for c in failed if c.get("subject_key")}
        meta["unresolved_subjects"] = sorted(set(meta.get("unresolved_subjects") or []) | subs)
        _hold_window(meta, since, result)
        if "abandoned_window" in result:
            # Bound hit: gave up re-scanning, but SURFACE the abandonment (never silent).
            result["abandoned_substantial"] = [c.get("signal") for c in failed]
        store.write_baseline(slug, claims, meta, _current_md(slug))
    elif write:
        # Escalated for the my_company arm only (SHADOW grounds + proposes but writes no card change,
        # or LIVE produced no new alert). No competitor window to hold open: just advance the gate.
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
            f.write(f"- **[{a.get('severity', 'watch').upper()}] {a['date']} — {a['headline']}** "
                    f"({a['subject_key']}): "
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
    # Weekend skip: no card is due on a skipped weekday (default Sunday). Loses no coverage —
    # the next run scans since last_checked — only timing. force=True (run_all) bypasses this.
    if now.weekday() in config.MONITOR_SKIP_WEEKDAYS:
        return False
    raw = meta.get("last_checked") or meta.get("baseline_date")
    if not raw:
        return True
    try:
        last = datetime.fromisoformat(raw)
    except ValueError:
        return True
    anchor = _latest_passed_anchor(now)
    if anchor is not None:
        if last >= anchor:
            return False                              # already served the latest anchor
        cadence_days = meta.get("cadence_days") or 1
        if cadence_days <= 1:
            return True                               # daily (default): due at the first unserved anchor
        # Slower per-card cadence (e.g. Batman weekly): also require enough whole days elapsed.
        return (anchor.date() - last.date()).days >= cadence_days
    cadence_hours = meta.get("cadence_hours") or config.DEFAULT_CADENCE_HOURS
    return now >= last + timedelta(hours=cadence_hours)


COST_DIR = "costs"   # per-run cost records in the PRIVATE store, mirroring the propagation log layout


def _run_total(cost: dict) -> float:
    """Sum a per-card phase-cost dict (triage/materiality/my_company/propagation/strategy),
    tolerating None values and missing phases."""
    return round(sum(v or 0 for v in (cost or {}).values()), 6)


def _persist_run_cost(started, rows: list, write: bool) -> None:
    """Persist ONE cost record per monitor run to the private store, for later review of spend.
    One file per run (costs/<stamp>.json), like the propagation decision logs. Carries the full
    per-card phase breakdown plus a run total. Best-effort: only writes in live (write) runs so
    local/dry runs never pollute the ledger, and never raises into the monitor path."""
    if not write or not rows:
        return
    try:
        stamp = started.strftime("%Y%m%dT%H%M%S")
        doc = {
            "schema_version": 1,
            "run_ts": started.isoformat(timespec="seconds"),
            "mode": config.PROPAGATE_MODE,
            "run_total_usd": round(sum(r.get("total") or 0 for r in rows), 4),
            "cards": rows,
        }
        selfserve.write_data(
            f"{COST_DIR}/{stamp}.json",
            json.dumps(doc, indent=2, default=str, ensure_ascii=False),
            f"costs: monitor run {stamp} (${doc['run_total_usd']}, {len(rows)} cards)",
        )
        print(f"[cost] run {stamp}: ${doc['run_total_usd']} across {len(rows)} card(s) "
              f"-> {COST_DIR}/{stamp}.json")
    except Exception as e:   # the cost ledger must never break a live monitor run
        print(f"[cost] ledger write skipped ({type(e).__name__}: {e})", file=sys.stderr)


def run_all(write: bool = True, send: bool = True, email_dry_run: bool = True,
            force: bool = False) -> list[dict]:
    """Cron entrypoint: check every DUE battlecard, write per policy, email digests.

    Due-gate (_is_due): by default a card is only checked when it hasn't been checked
    since the most recent passed anchor (7am ET, Mon–Sat), so the Cloud Scheduler
    dispatch lands one check per day without drift. `force=True` ignores the gate
    (manual runs).

    Side-effects gated for safety: write=False computes without mutating the store; email
    is dry unless email_dry_run=False AND creds are configured. The Actions run is live
    (SCOUT_MONITOR_LIVE=1); git commit/push is done by the workflow, not here.
    """
    from scout.display import list_battlecards
    from scout import notify

    run_started = datetime.now()
    summary = []
    cost_rows = []
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
        try:
            res = check(slug, write=write)
        except Exception as first_err:
            # One bounded retry: a transient SDK/API hiccup (observed 2026-06-10: the agent
            # subprocess surfaced an error result mid-stream) must not kill the cron run.
            # check() writes the store only at its very end, so a failed attempt leaves the
            # card untouched and is safe to redo. Worst-case extra spend is one more check.
            print(f"check({slug}) failed ({type(first_err).__name__}: {first_err}) — retrying once")
            try:
                res = check(slug, write=write)
            except Exception as e:
                # Record the failure and move on: one bad card must not block the other
                # cards' checks (or the workflow committing their results). __main__ exits
                # non-zero when any card errored, so the Actions run still notifies.
                summary.append({"slug": slug, "error": f"{type(e).__name__}: {e}"})
                continue
        emailed = prop_emailed = None
        if send:
            meta = store.load_meta(slug) or {}
            if res["alerts"]:
                emailed = notify.send_digest(meta, res["alerts"], dry_run=email_dry_run)
            # REVIEW/LIVE: email each judge-confirmed propagation proposal awaiting approval. In
            # review the card is untouched (human approves in-session); in live it's already applied.
            # Rewrite-EXHAUSTED failures ride the same email — an act-grade edit that died in
            # authoring must reach the owner even when nothing was confirmed (never a silent drop).
            prop = res.get("propagation")
            if prop and config.PROPAGATE_MODE in ("review", "live"):
                decisions = prop.get("decisions", [])
                confirmed = [d for d in decisions if d.get("judge_verdict") == "confirm"]
                exhausted = [d for d in decisions if d.get("rewrite_exhausted")]
                unjudged = [d for d in decisions if d.get("judge_verdict") == "judge_unavailable"]
                if confirmed or exhausted or unjudged:
                    prop_emailed = notify.send_propagation_proposals(
                        slug, meta, confirmed, dry_run=email_dry_run, exhausted=exhausted,
                        unjudged=unjudged)
            # CONSEQUENTIALITY FILTER (shadow eval, docs/consequential-filter-spec.md): the router now
            # emits the consequential/routine run_verdict the old strategic pass used to (that pass is
            # ABSORBED into the router — the lead is just the executive_summary surface). Log the verdict
            # for the longitudinal eval. DOWNSTREAM of the grounding shadow.capture in check() (Fold A:
            # never alter what the v3.5 grounding eval sees). "shadow" mode changes NOTHING — the verdict
            # is validated over weeks before it can gate. No-op unless SHADOW_EVAL_ENABLED.
            if prop and config.CONSEQUENTIAL_FILTER != "off":
                rv = prop.get("run_verdict") or {}
                if rv:
                    shadow.filter_capture(
                        slug, run_ts=res.get("last_checked"), verdict=rv,
                        act_subject_keys=[m["subject_key"] for m in res.get("material", [])],
                        competitor=meta.get("competitor"), my_company=meta.get("my_company"),
                        mode=config.CONSEQUENTIAL_FILTER)
            # LOUD ON FAILURE: a swallowed propagation crash must never leave a stale card looking clean
            # (the 7/1 miss). Surface pipeline_health to the owner so a broken reshape is never invisible.
            if res.get("pipeline_health"):
                try:
                    notify._dispatch(f"Scout: pipeline health — {slug}", res["pipeline_health"],
                                     dry_run=email_dry_run)
                except Exception as e:
                    print(f"[monitor] pipeline-health alert skipped ({type(e).__name__}: {e})", file=sys.stderr)
        cost = res["cost"]
        summary.append({
            "slug": slug, "no_change": res["no_change"], "material": len(res["material"]),
            "alerts": len(res["alerts"]),
            "cost_usd": _run_total(cost),   # ALL phases (triage+materiality+my_company+propagation+strategy)
            "emailed": emailed, "propagation_emailed": prop_emailed,
        })
        cost_rows.append({
            "slug": slug, "no_change": res["no_change"], "material": len(res["material"]),
            "alerts": len(res["alerts"]),
            "phases": {k: round(v, 6) for k, v in (cost or {}).items() if v is not None},
            "total": _run_total(cost),
        })
    _persist_run_cost(run_started, cost_rows, write)
    # CONSEQ. TRACK: once enough shadow verdicts have accumulated, email a one-time "ready to review"
    # spot-check digest (so the owner knows when to evaluate the filter for production). Best-effort.
    if send:
        from scout import conseq
        conseq.maybe_notify_ready(send=not email_dry_run)
    return summary


def _print_check(res):
    cost = res["cost"]
    total = _run_total(cost)
    print(f"\n=== monitor.check({res['slug']}) since {res['since']} ===")
    print(f"no_change={res['no_change']}  candidates={res['candidates']}  "
          f"substantial={res.get('substantial', 0)}  escalated={res.get('substantial', 0) > 0}  "
          f"material={len(res['material'])}")
    print(f"cost: triage=${cost.get('triage')}  materiality=${cost.get('materiality')}  "
          f"my_company=${cost.get('my_company')}  propagation=${cost.get('propagation')}  "
          f"TOTAL=${total:.4f}")
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
    # Partial failure still exits 1 (after the full summary prints) so the Actions run
    # notifies — but only after every card had its chance to check and write.
    if any(r.get("error") for r in out):
        raise SystemExit(1)
