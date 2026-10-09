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
from scout import sources_tool
from scout.generate import _drive, _extract_json, _build_retry_payload, _run_retry
from scout.propagate import propagate, apply_ops, promote_lead
from scout.grounding import CUT_ABSENT, ground_claims, is_excluded_source
from scout.prompts import WRITING_STYLE
from scout.render import claims_to_markdown, clean_output, extract_cut_log, format_report
from scout.schema import ANCHOR_SECTION, SECTIONS as SECTIONS_KNOWN, SOURCE_TIERS, ZONES, claim_id, normalize_claim, pregrounding_errors, resolve_subject_key, validation_errors
from scout import judgment

# Source-tier preference order for multi-source grounding (best first): a primary filing /
# company release beats reputable secondary reporting, which beats sentiment-only.
_TIER_RANK = {tier: i for i, tier in enumerate(SOURCE_TIERS)}
# Within a tier, the source CLASS decided by code from the host breaks the tie (2026-09-28, WS1):
# a filing beats the company's own page beats a job board; news beats an unknown host beats a forum.
_CLASS_RANK = {"filing": 0, "court": 0, "government": 0, "company_statement": 1, "job_posting": 2,
               "research": 3, "news": 3, "page_snapshot": 4, "unknown": 5, "review_site": 6, "forum": 6}


def _source_rank(url, tier) -> tuple:
    from scout.sources import classify as _classify
    return (_TIER_RANK.get(tier, 99), _CLASS_RANK.get(_classify.classify(url), 5))

MATERIAL_CATEGORIES = (
    judgment.get("monitor.MATERIAL_CATEGORIES")
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


def _norm_key(k) -> str:
    """Subject keys compared spacing- and case-insensitively (claim ids already are)."""
    from scout.schema import normalize_subject_key
    return normalize_subject_key(str(k or ""))


def _judged_immaterial_subjects(substantial: list[dict], immaterial: list[dict]) -> set:
    """Normalized subject keys of the substantial candidates the materiality judge explicitly ruled
    immaterial (the verdict echoes the candidate's signal text; keys are matched on the signal, exact
    or by prefix). A decided subject must not hold the window (2026-10-04: the $517B compute deal was
    ruled old news, yet the window stayed open for it, re-billed the next scan and was heading for an
    'abandoned' needs-you item)."""
    sigs = [str(i.get("signal") or "").strip().lower() for i in (immaterial or []) if isinstance(i, dict)]
    subst = [c for c in (substantial or []) if c.get("signal") and c.get("subject_key")]
    out = set()
    for c in subst:
        sig = str(c.get("signal") or "").strip().lower()
        if any(sig == x or sig[:80] == x[:80] or _same_story(sig, x) for x in sigs):
            out.add(_norm_key(c.get("subject_key")))
    return out


_STORY_STOP = {"the", "and", "for", "with", "from", "that", "this", "its", "into", "than", "over", "after", "before",
               "2024", "2025", "2026", "october", "september", "november", "oct", "sep", "nov", "new", "announces",
               "announced", "launches", "launched", "reported", "starting", "effective"}


def _story_tokens(s: str) -> set:
    return {w for w in re.findall(r"[a-z0-9][a-z0-9x$%.-]{2,}", (s or "").lower()) if w not in _STORY_STOP}


def _same_story(a: str, b: str) -> bool:
    """The judge echoes a candidate's signal in its own words (2026-10-09: 'Slack reworked its
    service-level agreement...' came back as 'Slack reworked its SLA to be less generous...', the
    exact-prefix match failed, the decided subject held the window for three mornings and was then
    'abandoned' in the owner's email). Two signals are the same story when they share at least four
    distinctive tokens or half of the shorter one's tokens."""
    ta, tb = _story_tokens(a), _story_tokens(b)
    if not ta or not tb:
        return False
    shared = len(ta & tb)
    return shared >= 4 or shared >= max(2, min(len(ta), len(tb)) // 2)


def _resolve_or_hold(meta: dict, new_alerts: list[dict], result: dict, decided: set | None = None) -> None:
    """WINDOW-CLOSE FIX (the 2026-07-01 permanent miss): an alert resolves a held window ONLY when
    it matches a subject the window was held FOR. An unrelated catch (the Copilot alert) must not
    erase a pending act-grade miss (the Fable lift) — unmatched and legacy windows (no stored
    subjects, so nothing can match) stay open, bounded as ever. A held subject the judge has since
    ruled immaterial (`decided`) counts as settled too; the window closes once every held subject is
    settled or alerted."""
    held = meta.get("unresolved_since")
    if not held:
        return
    held_subjects = {_norm_key(k) for k in (meta.get("unresolved_subjects") or [])}
    alerted = {_norm_key(a.get("subject_key")) for a in new_alerts}
    settled = alerted | set(decided or set())
    if held_subjects & alerted or (held_subjects and held_subjects <= settled):
        _clear_window(meta)
        result["unresolved_resolved"] = {"since": held, "by": "alert" if held_subjects & alerted else "judged immaterial"}
    else:
        _hold_window(meta, held, result)


# --- Stage 1: cheap triage gate ----------------------------------------------
_TRIAGE_SYSTEM = judgment.get("monitor._TRIAGE_SYSTEM", {'config.TRIAGE_MAX_SEARCHES': config.TRIAGE_MAX_SEARCHES, 'MATERIAL_CATEGORIES': MATERIAL_CATEGORIES})


def _supersede_hunt_targets(claims: list, today=None) -> list[dict]:
    """The replacement hunt (2026-07-25 supersede-retire): claims retired for citing a superseded
    identifier, within SUPERSEDE_HUNT_DAYS of retirement. Pure; derived from the claim store every
    run (no separate queue state) — the hunt starts when a supersede-retire lands and expires by
    date on its own. Triage gets these as active search targets so the REPLACEMENT value (the new
    version's benchmark/price/capability) arrives through the normal grounded path."""
    from datetime import date as _date
    if isinstance(today, str):
        today = _date.fromisoformat(today)
    today = today or _date.today()
    out = []
    for c in claims or []:
        if str(c.get("status", "active")) != "retired":
            continue
        reason = str(c.get("retired_reason") or "")
        if not reason.startswith("superseded:"):
            continue
        try:
            age = (today - _date.fromisoformat(str(c.get("retired_on")))).days
        except Exception:
            continue
        if 0 <= age <= config.SUPERSEDE_HUNT_DAYS:
            out.append({"subject_key": c.get("subject_key"), "section": c.get("section"),
                        "retired_reason": reason})
    return out


def _focus_of(meta: dict) -> str | None:
    """The card's focus area, or None for a general card ("General"/"None" count as none)."""
    f = str((meta or {}).get("focus") or "").strip()
    return None if not f or f.lower() in ("general", "none") else f


def _focus_note(meta: dict, focus_searches: int) -> str:
    """The two-scope framing for a focused card (private block monitor._FOCUS_NOTE), or "" for a
    general card. Rides the USER message of triage, the judge and the own-company step: an input
    the code owes the model, not a change to any instruction block."""
    focus = _focus_of(meta)
    if not focus:
        return ""
    note = judgment.optional("monitor._FOCUS_NOTE", {"focus": focus, "focus_searches": str(focus_searches)})
    return ("\n\n" + note) if note else f"\n\nFOCUS AREA: {focus}. Developments in this area and the companies' corporate developments are both material."


def _triage_budget(since: str | None, checked_at: str, focused: bool = False) -> tuple[int, int, float, int]:
    """(searches, max_turns, budget, focus_searches) for the detection window. Up to three days: the
    daily caps. A stale window earns two more searches per week of gap, up to
    TRIAGE_CATCHUP_MAX_SEARCHES. A FOCUSED card adds TRIAGE_FOCUS_SEARCHES on top, reserved for the
    focus area (Uroš 2026-10-03: no cap may cut off the area because corporate news was plentiful).
    Turns follow the searches (a search is a turn; four for reading and the answer). Code-owned."""
    try:
        days = (datetime.fromisoformat(checked_at[:19]) - datetime.fromisoformat(str(since)[:10])).days
    except Exception:
        days = 1
    if days <= 3:
        searches, budget = config.TRIAGE_MAX_SEARCHES, config.TRIAGE_MAX_BUDGET_USD
    else:
        searches = min(config.TRIAGE_CATCHUP_MAX_SEARCHES, config.TRIAGE_MAX_SEARCHES + 2 * ((days + 6) // 7))
        budget = max(config.TRIAGE_MAX_BUDGET_USD, config.TRIAGE_CATCHUP_BUDGET_USD)
    focus_searches = 0
    if focused:
        focus_searches = max(config.TRIAGE_FOCUS_SEARCHES, (searches + 1) // 2 if days > 3 else config.TRIAGE_FOCUS_SEARCHES)
        searches += config.TRIAGE_FOCUS_SEARCHES
        budget = max(budget, 0.75)
    if days <= 3 and not focused:
        return searches, config.TRIAGE_MAX_TURNS, budget, 0          # the daily caps, untouched
    return searches, searches + 4, budget, focus_searches


async def _run_triage(meta, since, claims, my_since=None, extra: str = "", searches: int | None = None,
                      max_turns: int | None = None, budget: float | None = None, focus_searches: int = 0):
    comp, me = meta.get("competitor"), meta.get("my_company")
    # the system block is the module constant on a normal day (its fingerprint is the triage eval
    # period); a catch-up window renders it with the larger search count, a separate period
    system_block = _TRIAGE_SYSTEM if not searches or searches == config.TRIAGE_MAX_SEARCHES else \
        judgment.text("monitor._TRIAGE_SYSTEM", {'config.TRIAGE_MAX_SEARCHES': searches, 'MATERIAL_CATEGORIES': MATERIAL_CATEGORIES})
    scope = (f"Scan BOTH sides and tag each candidate's 'about': the competitor {comp} AND your own "
             f"company {me}." if me else f"Scan the competitor {comp}.")
    # The own-side cutoff is the LAST SUCCESSFUL CHECK (~24h in normal daily operation; wider only
    # after a skipped day) — never a held competitor re-scan window, so an old own-co story can't
    # be re-surfaced and re-grounded day after day (the John Jumper double-grounding, 2026-07-02).
    my_cut = (f"\nCUTOFF (my_company): {my_since} — the last check, normally about 24 hours ago. "
              f"Surface ONLY own-company developments dated on/after {my_since}; anything older was "
              f"already scanned by a prior run." if me and my_since else "")
    hunts = _supersede_hunt_targets(claims)
    hunt_block = ("\n\nSUPERSEDED SUBJECTS — REPLACEMENT HUNT: these claims were recently retired "
                  "because they cited a now-superseded product/model/version. Spend part of the "
                  "search budget actively looking for the CURRENT replacement's value on the same "
                  "subject (the new version's benchmark result, price, or capability) and surface "
                  "any dated finding as a candidate — the card is missing this coverage until you "
                  "do. These hunt targets are EXEMPT from the date cutoffs above (the replacement "
                  "value may have been published before the cutoff; it is still missing from the "
                  "card):\n" + json.dumps(hunts, ensure_ascii=False, indent=1)) if hunts else ""
    user = (f"Competitor: {comp}" + (f" (we are {me})" if me else "") +
            f"\n{scope}"
            f"\nCUTOFF (competitor): {since}. Surface ONLY competitor developments dated on/after "
            f"{since} that are NOT already reflected in the tracked subjects below (apply both "
            f"strict filters)."
            + my_cut + _focus_note(meta, focus_searches or config.TRIAGE_FOCUS_SEARCHES) + "\n\n"
            f"TRACKED SUBJECTS (subject_key — current value already known):\n"
            + _tracked_digest(claims) + hunt_block + (extra or ""))
    options = ClaudeAgentOptions(
        model=config.FAST_MODEL,
        system_prompt={"type": "preset", "preset": "claude_code", "append": system_block + sources_tool.note("triage")},
        mcp_servers={"scoutfetch": FETCH_SERVER, **sources_tool.servers()},
        allowed_tools=["WebSearch", FETCH_TOOL_NAME, *sources_tool.names("triage")],
        disallowed_tools=config.MODEL_DISALLOWED_TOOLS,
        permission_mode="bypassPermissions",
        # Triage-specific tight caps (lever B): few turns structurally bound the number of
        # searches, and a sub-dollar budget hard-stops the routine check at pennies. A catch-up
        # window (see _triage_budget) raises all three together.
        max_turns=max_turns or config.TRIAGE_MAX_TURNS,
        max_budget_usd=budget or config.TRIAGE_MAX_BUDGET_USD,
    )
    return await _drive(user, options, "triage")


# --- Stage 2: materiality judgment (Opus) ------------------------------------
_MATERIALITY_SYSTEM = judgment.get("monitor._MATERIALITY_SYSTEM")


def _signals_block(slug: str) -> tuple[str, list[dict]]:
    """(prompt block, open signals) for this check: the open structured signals in front of triage
    (a new filing, a brand-new hiring department) plus the hiring CONTEXT line. Empty when signals
    are off or the store has nothing. Never raises: signals are an aid, never a dependency."""
    if not config.SIGNALS_ENABLED:
        return "", []
    try:
        from scout import signals
        opened = signals.open_signals(slug)
        block = ""
        if opened:
            print(f"[monitor] signals: {len(opened)} open for {slug}: " + "; ".join(str(s.get('summary'))[:60] for s in opened[:3]))
            rows = [{"kind": s.get("kind"), "summary": s.get("summary"), "source_url": s.get("source_url"),
                     "source_class": s.get("source_class"), "filed": s.get("filed"), "detected_at": s.get("detected_at")} for s in opened[:8]]
            block += (judgment.text("monitor._SIGNALS_NOTE", {'json.dumps(rows, ensure_ascii=False, indent=1)': json.dumps(rows, ensure_ascii=False, indent=1)}))
        return block, opened
    except Exception as e:
        print(f"[monitor] signals skipped ({type(e).__name__}: {e})", file=sys.stderr)
        return "", []


def _hiring_context(slug: str) -> str:
    """The hiring CONTEXT line for materiality and the own-side pass only (never triage: a headcount
    delta must not become a candidate on its own)."""
    if not config.SIGNALS_ENABLED:
        return ""
    try:
        from scout import signals
        return signals.context_block(slug)
    except Exception:
        return ""


def _evidence_in_hand(candidates: list) -> tuple[str, int]:
    """(the evidence note for the user prompt, the turn cap) when EVERY candidate carries the page text
    code read (Part 3 #4, gate mode); otherwise ("", the full cap). The note is a private pack block;
    without it the cap still applies but no framing is added. The capture context flags the cell."""
    from scout import calllog as _calllog
    with_ev = bool(candidates) and all(isinstance(c, dict) and c.get("evidence") for c in candidates)
    _calllog.set_context(evidence_attached=True if with_ev else None)
    if not with_ev:
        return "", config.MAX_TURNS
    note = judgment.optional("sensors._EVIDENCE_NOTE") or ""
    return ("\n\n" + note if note else ""), config.EVIDENCE_MAX_TURNS


async def _run_materiality(meta, since, candidates, claims, extra: str = "", role: str = "materiality"):
    comp, me = meta.get("competitor"), meta.get("my_company")
    ev_note, max_turns = _evidence_in_hand(candidates)
    user = (f"Competitor: {comp}" + (f" (we are {me})" if me else "") +
            f"\nChanges SINCE {since}." + _focus_note(meta, 0) + "\n\nTRACKED SUBJECTS (subject_key — current value):\n"
            + _tracked_digest(claims) +
            "\n\nCANDIDATE SIGNALS FROM TRIAGE:\n" + json.dumps(candidates, ensure_ascii=False) + ev_note + (extra or ""))
    options = ClaudeAgentOptions(
        model=config.ORCHESTRATOR_MODEL,
        system_prompt={"type": "preset", "preset": "claude_code",
                       "append": _MATERIALITY_SYSTEM + sources_tool.note() + "\n\n" + WRITING_STYLE},
        mcp_servers={"scoutfetch": FETCH_SERVER, **sources_tool.servers()},
        allowed_tools=["WebSearch", FETCH_TOOL_NAME, *sources_tool.names()],
        disallowed_tools=config.MODEL_DISALLOWED_TOOLS,
        permission_mode="bypassPermissions",
        max_turns=max_turns,
        max_budget_usd=config.MAX_BUDGET_USD,
    )
    try:
        return await _drive(user, options, role)
    finally:
        from scout import calllog as _calllog
        _calllog.set_context(evidence_attached=None)


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
    clean.sort(key=lambda s: _source_rank(s["source_url"], s["source_tier"]))  # primary first, then class
    return clean[:3]


def _ground_best(claims: list[dict]) -> dict:
    """Multi-source grounding (lever 4): for each claim, independently re-fetch its tier-ranked
    candidate sources and KEEP THE HIGHEST-TIER ONE THAT GROUNDS, demoting the rest to
    corroboration. Falls back to single-anchor behavior when the judge supplied no candidates.
    Same {kept, failed, cut} contract as grounding.ground_claims so check()'s retry round can
    consume it unchanged. A claim only enters `failed` when EVERY candidate failed to ground."""
    kept, failed, cut, results = [], [], [], []
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
        results += list(g.get("results") or [])        # per-claim instrumentation: the shadow capture reads it (10/4)
        if g["kept"]:
            best = min(g["kept"], key=lambda c: _source_rank(c.get("source_url"), c.get("source_tier")))
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
    return {"kept": kept, "failed": failed, "cut": cut, "results": results}


def _apply_updates(claims, material_grounded, alerted_fingerprints, updated_ids: set | None = None):
    """In-place update claims.json content + build new alert records (deduped).
    material_grounded: list of (claim_dict, alert_dict). Returns (new_claims, new_alerts).
    `updated_ids` (optional, shared across the two arms of one run): a claim already revised this
    run is not revised or alerted again (2026-10-04: the competitor arm and the own-company arm both
    landed the DevDay @ChatGPT launch on the same claim id, two alerts for one development). A
    revision keeps the stored key spelling; a new key takes the cards' `a | b | c` convention."""
    from scout.schema import canonical_subject_key
    by_id = {c["id"]: c for c in claims}
    new_alerts = []
    seen = set(alerted_fingerprints)
    done = updated_ids if updated_ids is not None else set()
    now = datetime.now()
    for claim, alert in material_grounded:
        if claim["id"] in done:
            continue  # the other arm already landed this development this run
        if claim["id"] in by_id:
            claim["subject_key"] = by_id[claim["id"]]["subject_key"]
        else:
            claim["subject_key"] = canonical_subject_key(claim["subject_key"])
        fp = _fingerprint(claim["subject_key"], str(alert.get("new_value", claim.get("claim", ""))))
        if fp in seen:
            continue  # already alerted this exact subject->value transition
        seen.add(fp)
        done.add(claim["id"])
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
_MY_FACTS_SYSTEM = judgment.get("monitor._MY_FACTS_SYSTEM", {'ANCHOR_SECTION': ANCHOR_SECTION})


async def _run_my_facts(meta, since, candidates, claims):
    comp, me = meta.get("competitor"), meta.get("my_company")
    ev_note, max_turns = _evidence_in_hand(candidates)
    user = (f"We are {me} (competing against {comp}).\nOUR developments SINCE {since}." + _focus_note(meta, 0) + "\n\n"
            "TRACKED SUBJECTS (subject_key — current value):\n" + _tracked_digest(claims) +
            "\n\nCANDIDATE OWN-SIDE SIGNALS FROM TRIAGE:\n" + json.dumps(candidates, ensure_ascii=False) + ev_note)
    options = ClaudeAgentOptions(
        # Sourcing work (search/fetch/verbatim excerpts), not judgment: SUBAGENT tier (2026-07-02
        # cost pass). Grounding verification stays deterministic; the Opus router/judge still gate
        # everything downstream, and a human approves in review mode.
        model=config.SUBAGENT_MODEL,
        system_prompt={"type": "preset", "preset": "claude_code",
                       "append": _MY_FACTS_SYSTEM + sources_tool.note() + "\n\n" + WRITING_STYLE},
        mcp_servers={"scoutfetch": FETCH_SERVER, **sources_tool.servers()},
        allowed_tools=["WebSearch", FETCH_TOOL_NAME, *sources_tool.names()],
        disallowed_tools=config.MODEL_DISALLOWED_TOOLS,
        permission_mode="bypassPermissions",
        max_turns=max_turns,
        max_budget_usd=config.MY_FACTS_MAX_BUDGET_USD,
    )
    try:
        return await _drive(user, options, "my_facts")
    finally:
        from scout import calllog as _calllog
        _calllog.set_context(evidence_attached=None)


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
    alert_by_id, to_ground, rejected = {}, [], []
    for m in (mdata.get("facts") or []):
        c = m.get("claim")
        if not isinstance(c, dict) or "subject_key" not in c:
            continue
        c["section"], c["zone"] = ANCHOR_SECTION, None      # force the anchor home in code
        c["claim_type"] = "fact"
        c["id"] = claim_id(slug, str(c["subject_key"]))
        c["verified"] = True
        normalize_claim(c)                                   # representation is code's job (10/4)
        cand = _candidate_variants(c)
        if cand and not c.get("source_url"):
            c.update(source_url=cand[0]["source_url"], source_tier=cand[0]["source_tier"],
                     evidence_excerpt=cand[0]["evidence_excerpt"])
        errs = pregrounding_errors({k: v for k, v in c.items() if k != "candidate_sources"})
        if errs:
            rejected.append(f"{c.get('subject_key')}: {errs[0]}")
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
    return {"grounded": pairs, "cost": mat.get("cost_usd"), "emitted": len(mdata.get("facts") or []),
            "schema_rejected": rejected, "ungrounded": len(grounded.get("failed") or []),
            "grounding": grounded}                        # kept / failed / cut / results for the eval mirror (10/4)


def _is_act(alert: dict) -> bool:
    return str(alert.get("severity", "")).strip().lower() == "act"


def _applied_feed_alerts(confirmed_ops: list, applied: list, operations=("add", "revise", "retire"),
                         when: datetime | None = None) -> list:
    """Build alert records for APPLIED propagation ops so the left updates panel (and the viewers'
    last-updated ordering, which keys on the feed) reflects EVERY change that lands on a card —
    never a silent edit (2026-07-02: approved revises were invisible: no feed row, no reorder).
    Only ops that actually landed are surfaced. Every entry carries all six keys the alerts.md
    writer hard-indexes (date/headline/subject_key/old_value/new_value/so_what)."""
    _OLD_NEW = {"retire": ("on the card", "removed"),
                "revise": ("prior version", "updated"),
                "add": (None, "new")}
    applied_by_sk = {(a.get("subject_key"), a.get("operation")) for a in applied}
    now = when or datetime.now()
    out = []
    for op in confirmed_ops:
        operation = op.get("operation")
        if operation not in operations:
            continue
        sk = op.get("subject_key") or op.get("target_subject_key")
        if (sk, operation) not in applied_by_sk:
            continue
        note = (op.get("feed_note") or op.get("retired_reason")
                or f"a play or objection was {'removed' if operation == 'retire' else 'updated'}")
        old_v, new_v = _OLD_NEW[operation]
        out.append({
            "date": now.date().isoformat(),
            "detected_at": now.isoformat(timespec="seconds"),
            "subject_key": sk,
            "old_value": old_v, "new_value": new_v,
            "headline": note,
            "so_what": note,
            "severity": "act",
            "source_url": None,
            "fingerprint": _fingerprint(str(sk), f"applied:{operation}:" + str(note)),
        })
    return out


def _retire_feed_alerts(confirmed_ops: list, applied: list) -> list:
    """Retire-only view of _applied_feed_alerts — the LIVE auto-apply path keeps its original
    behavior (fact alerts already cover live revises; review-mode approvals surface everything)."""
    return _applied_feed_alerts(confirmed_ops, applied, operations=("retire",))


def _lead_election_alert(election: dict, when: datetime | None = None) -> dict:
    """Feed alert for an AUTO-APPLIED lead election (2026-08-08): the 'Today's angle' changed, so the
    updates panel shows it (never a silent reorder). Carries the six keys the alerts.md writer
    hard-indexes, like every other feed row."""
    now = when or datetime.now()
    sk = election.get("winner_key")
    note = election.get("feed_note") or "Today's angle changed"
    return {
        "date": now.date().isoformat(),
        "detected_at": now.isoformat(timespec="seconds"),
        "subject_key": sk,
        "old_value": "prior angle", "new_value": "now leading",
        "headline": note,
        "so_what": election.get("rationale") or note,
        "severity": "act",
        "source_url": None,
        "fingerprint": _fingerprint(str(sk), "lead_election:" + str(election.get("incumbent_key"))),
    }


def _audience_alert(aud: dict, meta: dict, when: datetime | None = None) -> dict:
    """Feed alert for audience leads that landed (2026-10-02): visible in the updates panel, and
    the alert that carries the write (a card is only written when a run produced an alert). Same
    six hard-indexed keys as every other feed row."""
    from scout import audience as _aud
    now = when or datetime.now()
    who = ", ".join(_aud.LABELS.get(p, p) for p in sorted({str(a.get("subject_key", "")).split(" | ")[1]
                                                            for a in aud["applied"] if " | " in str(a.get("subject_key", ""))}))
    note = f"Today's angle written for the {who}" if who else "Audience leads updated"
    return {
        "date": now.date().isoformat(),
        "detected_at": now.isoformat(timespec="seconds"),
        "subject_key": "audience-lead",
        "old_value": "general angle only", "new_value": f"angle for: {who}" if who else "angle per audience",
        "headline": note,
        "so_what": "Pick that audience on the brief to read its angle.",
        "severity": "watch",
        "source_url": None,
        "fingerprint": _fingerprint("audience-lead", f"{now.date().isoformat()}:{who}"),
    }


def _competitor_arm(slug, meta, since, substantial, claims, result, sig_block: str = ""):
    """Competitor materiality (Opus) -> tier-ranked MULTI-SOURCE grounding -> bounded feedback
    retry -> material_grounded. Logic is UNCHANGED from the pre-my_company flow; extracted verbatim
    so check() can run it conditionally now that the my_company arm can fire on its own. Returns
    (material_grounded, grounded)."""
    mat = asyncio.run(_run_materiality(meta, since, substantial, claims, extra=sig_block))
    result["cost"]["materiality"] = mat.get("cost_usd")
    try:
        mdata = _extract_json(mat["text"])
    except Exception:
        mdata = {"material": []}
    alert_by_id, to_ground, rejected = {}, [], []
    for m in (mdata.get("material") or []):
        c = m.get("claim")
        if not isinstance(c, dict) or "subject_key" not in c:
            continue
        normalize_claim(c)                                   # representation is code's job (10/4)
        _adopt_home(c, claims)                               # a revision keeps the stored key and home (10/5)
        c["id"] = claim_id(slug, str(c["subject_key"]))
        c["verified"] = True
        cand = _candidate_variants(c)
        if cand and not c.get("source_url"):
            c.update(source_url=cand[0]["source_url"], source_tier=cand[0]["source_tier"],
                     evidence_excerpt=cand[0]["evidence_excerpt"])
        errs = pregrounding_errors({k: v for k, v in c.items() if k != "candidate_sources"})
        if errs:
            rejected.append(f"{c.get('subject_key')}: {errs[0]}")
            continue
        alert_by_id[c["id"]] = m.get("alert", {})
        to_ground.append(c)
    result["materiality_emitted"] = len(mdata.get("material") or [])
    result["materiality_schema_rejected"] = rejected

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
            normalize_claim(c)
            _adopt_home(c, claims)
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


# --- Sensors (Release 2, 2026-10-04) -------------------------------------------------------------
# The daily pass runs ONCE per monitor run (run_all, before the card loop) for every entity on a due
# card and leaves its summary here; each check reads its entities' open findings, runs the screen,
# and (shadow) writes the compare record. Everything is behind config.SENSORS_MODE; off = untouched.
_SENSORS: dict = {"summary": {}, "today": None}


def _sensor_entities(slugs: list) -> dict:
    """{entity_key: {name, names, cards}} for the cards about to be checked."""
    from scout.sensors import registry as _reg
    out: dict = {}
    for slug in slugs:
        meta = store.load_meta(slug) or {}
        for e in _reg.entities_for(meta):
            rec = out.setdefault(e["key"], {"name": e["name"], "names": [e["name"]], "cards": []})
            rec["cards"].append(slug)
    return out


def _sensor_pass(slugs: list, write: bool, today: str) -> dict:
    """Run the pass for the due cards' entities; never raises (a failure marks every entity unavailable)."""
    if config.SENSORS_MODE == "off" or not slugs:
        _SENSORS.update({"summary": {}, "today": today})
        return {}
    try:
        from scout.sensors import collect
        ents = _sensor_entities(slugs)
        days = {}
        for slug in slugs:
            meta = store.load_meta(slug) or {}
            lc = _since_date(meta.get("last_checked") or meta.get("baseline_date"))
            try:
                gap = (datetime.fromisoformat(today) - datetime.fromisoformat(lc)).days if lc else 2
            except Exception:
                gap = 2
            for e in _sensor_entities([slug]):
                days[e] = max(days.get(e, 1), min(14, max(1, gap + 1)))
        # the pass on a daemon thread with a deadline: a hang inside it (a browser call that never
        # returns, a feed that never closes) abandons the sensors for the day instead of the morning
        import threading
        box: dict = {}
        t = threading.Thread(target=lambda: box.update(collect.run_pass(ents, write=write, today=today, days_by_entity=days)), daemon=True)
        t.start()
        t.join(timeout=config.SENSOR_PASS_TIMEOUT_S)
        if t.is_alive():
            raise TimeoutError(f"sensor pass exceeded {config.SENSOR_PASS_TIMEOUT_S}s and was abandoned for today; every card takes the model path")
        summary = dict(box)
        _SENSORS.update({"summary": summary, "entities": ents, "today": today})
        found = sum(v.get("findings", 0) for v in summary.values())
        print(f"[sensors] pass: {len(ents)} entit{'y' if len(ents) == 1 else 'ies'}, {found} finding(s), "
              f"{sum(1 for v in summary.values() if v.get('unavailable'))} unavailable")
        return summary
    except Exception as e:
        print(f"[sensors] pass FAILED ({type(e).__name__}: {e})", file=sys.stderr)
        _SENSORS.update({"summary": {}, "today": today, "error": f"{type(e).__name__}: {e}"})
        return {}


def _sweep_day(slug: str, when: str) -> bool:
    """Deterministic weekly sweep day per card (sha256 of the slug over the run weekdays; Python's
    hash() is salted per process and would move every run)."""
    days = sorted(d for d in range(7) if d not in config.MONITOR_SKIP_WEEKDAYS) or [0]
    wd = days[int(hashlib.sha256(slug.encode()).hexdigest(), 16) % len(days)]
    try:
        return datetime.fromisoformat(when[:10]).weekday() == wd
    except Exception:
        return False


def _sensor_screen(slug: str, meta: dict, claims: list, since: str | None, sig_block: str, steps: list,
                   check_reason: str) -> dict | None:
    """The card's view of this morning's sensors: open findings, the screen's candidates, and whether
    the model-triage path must run anyway. None when sensors are off."""
    if config.SENSORS_MODE == "off":
        return None
    from scout.sensors import events, registry as _reg, screen as _screen
    ents = _reg.entities_for(meta)
    summary = _SENSORS.get("summary") or {}
    sens = {"mode": config.SENSORS_MODE, "entities": [e["key"] for e in ents], "findings": [], "candidates": [],
            "unavailable": False, "no_registry": False, "status": "skipped", "detail": "", "cost_usd": 0.0,
            "by_entity": {}, "names": [e["name"] for e in ents], "watched_hosts": set(),
            "sweep": False, "use_triage": True, "pass_error": _SENSORS.get("error")}
    for e in ents:
        sm = summary.get(e["key"]) or {}
        if not sm:
            if sens["pass_error"]:
                sens["unavailable"] = True    # the pass died or ran out of time: a failure, named in the step row
            else:
                sens["no_registry"] = True    # the pass never saw this entity (no registry)
        if sm.get("unavailable"):
            sens["unavailable"] = True
        if sm.get("no_registry"):
            sens["no_registry"] = True
        try:
            reg = _reg.load(e["key"]) or {}
            for src in reg.get("sources") or []:
                from scout.sensors import feeds as _feeds
                sens["watched_hosts"].add(_feeds.host_of(src.get("url")))
        except Exception:
            pass
        try:
            opened = events.open_for(e["key"], slug)
        except Exception as ex:
            opened = []
            sens["detail"] += f"{e['key']}: findings unreadable ({type(ex).__name__}); "
        for f in opened:
            f = dict(f, role=e["role"])
            sens["findings"].append(f)
        sens["by_entity"][e["key"]] = {"open": len(opened), **{k: sm.get(k) for k in ("pages_checked", "pages_changed", "pages_failed", "feed_items", "news_hits", "findings", "unavailable", "no_registry", "baselined_on")}}
    _step(steps, "sensors", "failed" if sens["unavailable"] else ("skipped" if sens["no_registry"] else "ran"),
          (f"{len(sens['findings'])} open finding(s) across {len(ents)} entit{'y' if len(ents) == 1 else 'ies'}: "
           + "; ".join(f"{k}: {v.get('pages_checked') or 0} pages ({v.get('pages_changed') or 0} changed), {v.get('feed_items') or 0} feed, {v.get('news_hits') or 0} news"
                       for k, v in sens["by_entity"].items()))
          + ((f" | pass failed: {sens['pass_error']}; model triage today" if sens["pass_error"] else " | news channel down: model triage today") if sens["unavailable"] else "")
          + (" | no registry for an entity: model triage today" if sens["no_registry"] else ""))
    if sens["findings"] and not sens["unavailable"]:
        r = _screen.run(meta, claims, sens["findings"], since or "", sig_block)
        sens.update({"candidates": r["candidates"], "status": r["status"], "detail": r["detail"], "cost_usd": r.get("cost_usd") or 0.0})
        _step(steps, "screen", "ran" if r["status"] == "ok" else r["status"], r["detail"], r.get("cost_usd"))
    else:
        _step(steps, "screen", "skipped", "no open findings" if not sens["unavailable"] else "sensors unavailable")
    # gate: the screen's candidates are the candidates, except where the model path must run anyway
    sens["sweep"] = config.SENSORS_MODE == "gate" and config.SENSOR_SWEEP and _sweep_day(slug, _SENSORS.get("today") or datetime.now().isoformat())
    sens["use_triage"] = (config.SENSORS_MODE != "gate" or sens["unavailable"] or sens["no_registry"]
                          or sens["status"] == "failed" or bool(meta.get("unresolved_since"))
                          or check_reason == "signal" or sens["sweep"])
    return sens


def _sensor_consume(slug: str, sens: dict | None, run_ts: str) -> None:
    if not sens or not sens.get("findings"):
        return
    from scout.sensors import events
    by_ent: dict = {}
    for f in sens["findings"]:
        by_ent.setdefault(f.get("entity"), []).append(f.get("fingerprint"))
    for ent, fps in by_ent.items():
        try:
            events.consume(ent, slug, run_ts, [x for x in fps if x])
        except Exception as e:
            print(f"[sensors] consume skipped for {ent} ({type(e).__name__}: {e})", file=sys.stderr)


def _sensor_compare(slug: str, meta: dict, sens: dict | None, candidates: list, material_grounded: list,
                    steps: list, write: bool, checked_at: str, triage_ran: bool) -> dict | None:
    """Shadow (and gate sweep days): the compare record for this card. Returns the summary row."""
    if not sens:
        return None
    try:
        from scout.sensors import compare, events
        today = checked_at[:10]
        recent = []
        for e in sens["entities"]:
            try:
                recent += events.recent(e)
            except Exception:
                pass
        dates = [(datetime.fromisoformat(checked_at) - timedelta(days=d)).date().isoformat() for d in (1, 2, 3)]
        screen_window = list(sens["candidates"]) + compare.recent_screen_candidates(slug, dates)
        baselines = [str(v.get("baselined_on")) for v in sens["by_entity"].values() if (v or {}).get("baselined_on")]
        baseline = max(baselines) if baselines else today      # the youngest entity bounds what the sensors could have seen
        la = compare.level_a(material_grounded, recent, screen_window, set(sens.get("watched_hosts") or set()),
                             names=sens.get("names") or [], baseline=baseline) if triage_ran else []
        lb = compare.level_b(candidates if triage_ran else [], sens["candidates"], recent, sens.get("names") or []) if triage_ran else {"matched": [], "misses": [], "screen_only": []}
        misses_a = [r for r in la if r.get("miss")]
        pre_baseline = [r for r in la if r.get("covered_by") == "pre_baseline"]
        payload = {"mode": sens["mode"], "triage_ran": triage_ran, "entities": sens["entities"], "by_entity": sens["by_entity"],
                   "findings_open": len(sens["findings"]), "findings_recent": len(recent),
                   "screen": {"status": sens["status"], "cost_usd": sens["cost_usd"], "candidates": sens["candidates"]},
                   "triage": {"candidates": [{k: c.get(k) for k in ("signal", "subject_key", "about", "substantial", "source_hint")} for c in candidates]} if triage_ran else None,
                   "landed": la, "misses_a": misses_a, "pre_baseline": len(pre_baseline), "baseline": baseline,
                   "level_b": lb, "pass_error": sens.get("pass_error")}
        doc = compare.record(slug, today, payload, write)
        row = {"slug": slug, "misses_a": len(misses_a), "misses": misses_a, "level_b_misses": lb["misses"],
               "screen_only": lb["screen_only"], "errors": sum(int((v or {}).get("pages_failed") or 0) for v in sens["by_entity"].values()),
               "sources": sum(int((v or {}).get("pages_checked") or 0) + int(bool((v or {}).get("news_hits") is not None)) for v in sens["by_entity"].values()),
               "screen_cost": sens["cost_usd"], "screen_subst": sum(1 for c in sens["candidates"] if c.get("substantial")),
               "triage_subst": sum(1 for c in candidates if c.get("substantial") is True) if triage_ran else 0,
               "findings": len(sens["findings"]), "findings_recent": len(recent), "unavailable": sens["unavailable"], "by_entity": sens["by_entity"],
               "write_error": doc.get("write_error")}
        _step(steps, "compare", "ran", f"{len(la)} landed alert(s) checked, {len(misses_a)} miss(es)"
              + (f", {len(pre_baseline)} pre-baseline (events before {baseline}, not comparable)" if pre_baseline else "") + "; "
              f"level B: {len(lb['matched'])} matched, {len(lb['misses'])} unmatched, {len(lb['screen_only'])} screen-only")
        return row
    except Exception as e:
        print(f"[sensors] compare FAILED ({type(e).__name__}: {e})", file=sys.stderr)
        _step(steps, "compare", "failed", f"{type(e).__name__}: {e}")
        return {"slug": slug, "misses_a": 0, "error": f"{type(e).__name__}: {e}"}


def _adopt_home(c: dict, claims: list) -> dict:
    """A model-emitted claim keyed like a stored claim is a REVISION of it: take the stored key spelling
    (a field-wise prefix resolves to the one stored key it names) and, when the emitted section or
    zone is not one the schema knows, keep the stored home instead of losing the fact (2026-10-05).
    A valid, different zone is the judge's call and stays."""
    existing = {str(x.get("subject_key")): x for x in (claims or []) if x.get("subject_key")}
    key = resolve_subject_key(c.get("subject_key"), list(existing))
    if key != c.get("subject_key"):
        c["subject_key"] = key
    old = existing.get(key)
    if old:
        if c.get("section") not in SECTIONS_KNOWN:
            c["section"] = old.get("section")
        if c.get("section") == "battlecard" and c.get("zone") not in ZONES:
            c["zone"] = old.get("zone")
    # A FACT aimed at a block section (a play, the summary, an objection) is mis-homed: those sections
    # hold interpretations with a render contract (soundbite, persona). The fact lands in recent_moves,
    # the fact section for developments, under its own key when the stored claim it named is an
    # interpretation; propagation then derives the play's revision from it (its job, not the judge's).
    if c.get("claim_type") == "fact" and c.get("section") in _BLOCK_SECTIONS:
        c["section"], c["zone"], c["persona"] = "recent_moves", None, None
        if old and old.get("claim_type") != "fact":
            c["subject_key"] = f"{key} | fact"
    if c.get("section") != "battlecard" and c.get("zone") is not None:
        c["zone"] = None
    return c


_BLOCK_SECTIONS = ("battlecard", "executive_summary", "objection_handling")


def _screen_sample(slug: str, meta: dict, since: str, claims: list, sens: dict, row: dict, steps: list,
                   write: bool, checked_at: str) -> dict | None:
    """SHADOW precision sample (2026-10-07): up to SCREEN_SAMPLE_PER_CARD screen-only competitor-side
    candidates of this card go through the materiality judge, bounded per run by SCREEN_SAMPLE_PER_RUN
    and SCREEN_SAMPLE_BUDGET_USD and overall by SCREEN_SAMPLE_MAX_TOTAL. Nothing lands: the verdicts
    are recorded (sensors/_sample/<date>/<slug>.json) and tallied into the streak row, so the
    screen's precision is a measured number before any cutover. Never raises."""
    if config.SENSORS_MODE != "shadow" or not write or config.SCREEN_SAMPLE_PER_RUN <= 0:
        return None
    tally = _SENSORS.setdefault("sample", {"judged": 0, "material": 0, "cost": 0.0, "cards": 0})
    if tally["judged"] >= config.SCREEN_SAMPLE_PER_RUN or tally["cost"] >= config.SCREEN_SAMPLE_BUDGET_USD:
        _step(steps, "screen_sample", "skipped", "run sample cap reached")
        return None
    try:
        from scout.sensors import compare as _compare
        if _compare.sample_total() >= config.SCREEN_SAMPLE_MAX_TOTAL:
            _step(steps, "screen_sample", "skipped", f"sample complete ({config.SCREEN_SAMPLE_MAX_TOTAL} judged); set SCOUT_SCREEN_SAMPLE_MAX_TOTAL to continue")
            return None
    except Exception:
        pass
    only = [c for c in (sens.get("candidates") or []) if c.get("substantial") is True
            and str(c.get("about") or "").lower() != "my_company"
            and any((c.get("signal") or "")[:160] == so.get("signal") for so in (row.get("screen_only") or []))]
    room = min(config.SCREEN_SAMPLE_PER_CARD, config.SCREEN_SAMPLE_PER_RUN - tally["judged"])
    picked = only[:room]
    if not picked:
        _step(steps, "screen_sample", "skipped", "no screen-only competitor candidate")
        return None
    try:
        mat = asyncio.run(_run_materiality(meta, since, picked, claims, role="screen_sample"))
        cost = float(mat.get("cost_usd") or 0)
        try:
            d = _extract_json(mat["text"])
        except Exception:
            d = {}
        material = [m.get("claim", {}).get("subject_key") if isinstance(m.get("claim"), dict) else None for m in (d.get("material") or [])]
        immaterial = [{"signal": str(i.get("signal") or "")[:160], "why_not": str(i.get("why_not") or "")[:200]} for i in (d.get("immaterial") or []) if isinstance(i, dict)]
        judged = len(picked)
        n_material = min(judged, len([m for m in (d.get("material") or []) if isinstance(m, dict)]))
        tally["judged"] += judged; tally["material"] += n_material; tally["cost"] += cost; tally["cards"] += 1
        rec = {"slug": slug, "run_ts": checked_at, "sampled": [{k: c.get(k) for k in ("signal", "subject_key", "about", "source_hint")} for c in picked],
               "material": material, "immaterial": immaterial, "judged": judged, "n_material": n_material, "cost_usd": round(cost, 4)}
        try:
            selfserve.write_data(f"sensors/_sample/{checked_at[:10]}/{slug}.json", json.dumps(rec, indent=1, ensure_ascii=False, default=str),
                                 f"sensors: screen sample {checked_at[:10]} {slug}")
        except Exception as e:
            rec["write_error"] = f"{type(e).__name__}: {e}"
        _step(steps, "screen_sample", "ran", f"{n_material} material of {judged} screen-only candidate(s) judged (shadow, nothing lands)", cost)
        return rec
    except Exception as e:
        print(f"[sensors] screen sample FAILED ({type(e).__name__}: {e})", file=sys.stderr)
        _step(steps, "screen_sample", "failed", f"{type(e).__name__}: {e}", getattr(e, "scout_cost_usd", None))
        return None


def _arm_status(grounded: int, candidates: int, what: str, emitted: int, rejected: list) -> tuple[str, str]:
    """(status, detail) for a materiality / own-company step row. Facts the model emitted that the
    schema rejected BEFORE grounding are named (2026-10-04: Sonnet 5.5 wrote confidence as 0.9, every
    own-side fact was dropped and the row said "0 grounded of 4", as if grounding had failed). When
    the model emitted facts and the schema rejected every one, the step FAILED: it reaches the
    needs-you email and the canary instead of reading like a quiet morning."""
    detail = f"{grounded} {what} of {candidates} candidate(s)"
    if rejected:
        detail += f"; {len(rejected)} of {emitted} emitted fact(s) rejected by the schema before grounding: " + "; ".join(rejected[:3])
        if grounded == 0 and emitted and len(rejected) >= emitted:
            return "failed", detail
    return "ran", detail


def _step(steps: list, name: str, status: str, detail=None, cost=None) -> None:
    """One row of the run's step table (2026-10-03): `ran | skipped | failed`, the reason, the spend.
    The rows ride the check result, the cost ledger, the FYI footer and the needs-you email, so a
    step that dies can never fail into stderr alone again (the 10/3 audience crash: four cards
    failed, the needs-you email said three items on two cards, the canary was green)."""
    row = {"step": name, "status": status}
    if detail:
        row["detail"] = str(detail)[:400]
    if cost:
        row["cost"] = round(float(cost), 4)
    steps.append(row)
    # the live log says where the run is (2026-10-08: two hung runs left no trace of their progress)
    print(f"[step] {name}: {status}" + (f" ({str(detail)[:160]})" if detail else "") + (f" ${float(cost):.3f}" if cost else ""), flush=True)


def _merge_candidates(base: list, extra: list) -> list:
    """Carried-over candidates joined to today's, deduped on the signal text (a story triage found
    again today is not presented twice)."""
    seen = {str(c.get("signal") or "")[:80].strip().lower() for c in base}
    out = list(base)
    for c in extra:
        key = str(c.get("signal") or "")[:80].strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(c)
    return out


def check(slug: str, write: bool = False, since_override: str | None = None, escalate: bool = True) -> dict:
    """One monitoring check. write=False measures without mutating the store (for cost runs).
    since_override forces the detection window (e.g. an old date to simulate a stale baseline).
    escalate=False is the run ceiling (2026-10-03): triage only; substantial candidates are carried
    to the next run instead of running the paid steps today. A crash carries the step rows recorded
    so far on the exception (`scout_steps`) so run_all can report them."""
    steps: list = []
    try:
        return _check(slug, write, since_override, escalate, steps)
    except BaseException as e:
        try:
            e.scout_steps = steps
        except Exception:
            pass
        raise


def _check(slug: str, write: bool, since_override: str | None, escalate: bool, steps: list) -> dict:
    meta = store.load_meta(slug) or {}
    claims = store.load_claims(slug)
    # Detection window: a HELD `unresolved_since` (a prior substantial item we detected but
    # couldn't ground) takes precedence over last_checked, so we keep re-scanning from that date
    # until the item is captured or the retry bound is hit — last_checked still advances for the
    # due-gate, so this never causes same-window re-escalation storms.
    since = _since_date(since_override or meta.get("unresolved_since")
                        or meta.get("last_checked") or meta.get("baseline_date"))
    # OWN-SIDE cutoff: the last successful check (~24h in normal daily ops; wider only after a
    # skipped day) — deliberately NOT the held unresolved_since window, which exists for COMPETITOR
    # grounding retries. Keeps the my_company arm from re-scanning (and re-grounding) old own-co
    # stories every day a window stays open (2026-07-02 cost pass; Uroš's design).
    my_since = _since_date(since_override or meta.get("last_checked") or meta.get("baseline_date"))
    checked_at = datetime.now().isoformat(timespec="seconds")  # full timestamp, not just a date
    print(f"[monitor] checking {slug} at {checked_at}", flush=True)
    # a signal-dispatched run counts as the day's check for the due gate (WS3): the reason rides meta
    check_reason = "signal" if os.environ.get("SCOUT_MONITOR_REASON", "").strip() else "scheduled"
    from scout import calllog
    calllog.set_context(slug=slug, phase="monitor")
    reset_log()

    # Structured signals (WS3): open filings / new-department events in front of triage, and the
    # hiring context line; `sig_open` is consumed (marked) once this check has written.
    sig_block, sig_open = _signals_block(slug)
    ctx_block = _hiring_context(slug)
    _step(steps, "signals", "ran" if sig_open else "skipped",
          f"{len(sig_open)} open signal(s) in front of triage" if sig_open
          else ("signals off" if not config.SIGNALS_ENABLED else "none open"))
    # FAILED-ARM CARRY-OVER (2026-10-03): candidates a paid step died on (its budget, a crash) or
    # could not ground, and candidates a run ceiling deferred, are re-presented to that step today
    # without a second detection, with the cutoffs they came from. Bounded by
    # MONITOR_MAX_UNRESOLVED_RETRIES, then abandoned LOUDLY (needs-you), never silently.
    pending = dict(meta.get("pending_candidates") or {}) if isinstance(meta.get("pending_candidates"), dict) else {}
    carried_comp = [c for c in (pending.get("competitor") or []) if isinstance(c, dict)]
    carried_my = [c for c in (pending.get("my_company") or []) if isinstance(c, dict)]
    # SENSORS (Release 2): the card's open findings and the screen's candidates. In shadow the
    # unchanged triage still decides; in gate the screen decides unless the model path must run
    # (sweep day, held window, dispatched run, sensors unavailable). A sweep day widens the cutoff
    # to the last sweep so the audit covers the week, and the two candidate lists are unioned.
    sens = _sensor_screen(slug, meta, claims, since, sig_block, steps, check_reason)
    triage_ran = True
    if sens and sens.get("sweep"):
        last_sweep = _since_date(meta.get("last_sweep")) or (datetime.fromisoformat(checked_at) - timedelta(days=config.SENSOR_SWEEP_DAYS)).date().isoformat()
        since = min(since or last_sweep, last_sweep)
    if sens and not sens["use_triage"]:
        triage_ran = False
        triage = {"cost_usd": 0.0, "text": ""}
        candidates = list(sens["candidates"])
        _step(steps, "triage", "skipped", "gate: the screen's candidates stand in; no model search today")
    else:
        # Stage 1: triage (cheap on a normal day; a stale window earns a catch-up budget)
        t_searches, t_turns, t_budget, t_focus = _triage_budget(since, checked_at, focused=bool(_focus_of(meta)))
        if t_searches != config.TRIAGE_MAX_SEARCHES:
            _step(steps, "triage_budget", "ran", f"window since {since}" + (f", focus area '{_focus_of(meta)}' ({t_focus} searches reserved)" if t_focus else "")
                  + f": {t_searches} searches, {t_turns} turns, ${t_budget:.2f}")
        # extra kwargs only when the budget differs from the daily caps (the test fakes keep their shape)
        t_kw = {} if t_searches == config.TRIAGE_MAX_SEARCHES else {"searches": t_searches, "max_turns": t_turns, "budget": t_budget, "focus_searches": t_focus}
        try:
            triage = asyncio.run(_run_triage(meta, since, claims, my_since=my_since, extra=sig_block, **t_kw))
        except BaseException as e:
            _step(steps, "triage", "failed", f"{type(e).__name__}: {e}", getattr(e, "scout_cost_usd", None))
            raise
        try:
            tdata = _extract_json(triage["text"])
        except Exception:
            tdata = {"has_candidates": False, "candidates": []}
        candidates = tdata.get("candidates", []) if tdata.get("has_candidates") else []
        if sens and sens.get("sweep"):
            # the sweep's catches already handled by sensors this week are not re-escalated
            seen_sk = {str(c.get("subject_key")) for c in sens["candidates"] if c.get("subject_key") and c.get("subject_key") != "NEW"}
            candidates = _merge_candidates([c for c in candidates if str(c.get("subject_key")) not in seen_sk], sens["candidates"])
    # Deterministic escalation floor: a candidate on a TRACKED subject escalates regardless of the
    # cheap triage grade (the 2026-07-01 Fable-lift miss — triage graded a status flip "minor").
    _escalation_floor(candidates, claims)
    if triage_ran:
        _step(steps, "triage", "ran",
              f"{len(candidates)} candidate(s), {sum(1 for c in candidates if c.get('substantial') is True)} substantial"
              + (" (sweep day: unioned with the screen)" if sens and sens.get("sweep") else ""),
              triage.get("cost_usd"))
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
    if carried_comp or carried_my:
        substantial = _merge_candidates(substantial, carried_comp)
        my_substantial = _merge_candidates(my_substantial, carried_my)
        if carried_my and pending.get("my_since"):
            my_since = min(my_since or pending["my_since"], str(pending["my_since"]))
        _step(steps, "carry_over", "ran",
              f"{len(carried_comp)} competitor + {len(carried_my)} own-side candidate(s) carried from "
              f"{pending.get('since')} ({pending.get('reason') or 'failed step'}, attempt {int(pending.get('attempts') or 0) + 1})")
    # The my_company arm is PART of propagation (spec §17): it runs only when propagation is enabled.
    # With PROPAGATE_MODE=off (the default, and production today) do_my is always False, the arm is
    # dead, and everything below is byte-identical to the competitor-only flow.
    do_my = config.PROPAGATE_MODE in ("shadow", "review", "live") and bool(my_substantial)

    result = {
        "slug": slug, "since": since, "no_change": not substantial and not my_substantial,
        "candidates": len(candidates), "substantial": len(substantial),
        "minor_skipped": len(comp_candidates) - len([c for c in comp_candidates if c.get("substantial") is True]),
        "my_company_signals": my_company_signals, "my_substantial": len(my_substantial),
        "material": [], "alerts": [],
        "cost": {"triage": triage.get("cost_usd"), "materiality": 0.0},
        "last_checked": checked_at,
        "last_check_reason": check_reason,
        "steps": steps,
    }
    if sens and sens.get("cost_usd"):
        result["cost"]["screen"] = sens["cost_usd"]
    if sens and sens.get("sweep") and write:
        meta["last_sweep"] = checked_at[:10]

    if not substantial and not do_my:
        # Quiet / minor-only window (neither arm has act-able work): triage-only, cheap. Advance
        # last_checked and stop. A held detection window is NOT cleared by one empty re-scan
        # (retrieval variance: the Fable lift was found 1-of-4 runs on the same window) — it stays
        # open, bounded, and abandons loudly at the retry bound.
        for name in ("materiality", "own_company", "propagation", "audience"):
            _step(steps, name, "skipped", "quiet: no substantial candidate")
        if write:
            meta["last_checked"] = checked_at; meta["last_check_reason"] = check_reason
            if meta.get("unresolved_since"):
                _hold_window(meta, meta["unresolved_since"], result)
            if pending:
                meta.pop("pending_candidates", None)     # carried candidates were re-triaged quiet
            store.write_baseline(slug, claims, meta, _current_md(slug))
            _step(steps, "write", "ran", "heartbeat" + (", held window kept open" if result.get("unresolved_held") else "")
                  + (", held window abandoned" if result.get("abandoned_window") else ""))
            # every written check consumes what it was shown (2026-10-03: the quiet path used to
            # return before this, so an unescalated signal was shown again every run for 7 days)
            if sig_open:
                _consume_signals(slug, sig_open, checked_at)
                _step(steps, "signals_consume", "ran", f"{len(sig_open)} consumed")
            _sensor_consume(slug, sens, checked_at)
        if sens:
            result["sensors"] = _sensor_compare(slug, meta, sens, candidates, [], steps, write, checked_at, triage_ran)
        return result

    if not escalate:
        # RUN CEILING (2026-10-03): the run's total crossed SCOUT_RUN_MAX_USD before this card. No
        # paid step today; the candidates are carried to the next run (nothing is lost, nothing is
        # re-detected) and the needs-you email names the card.
        _step(steps, "ceiling", "skipped",
              f"run ceiling ${config.RUN_MAX_USD:.0f} reached: {len(substantial)} competitor + "
              f"{len(my_substantial)} own-side substantial candidate(s) carried to the next run")
        for name in ("materiality", "own_company", "propagation", "audience"):
            _step(steps, name, "skipped", "run ceiling")
        result["ceiling_deferred"] = [str(c.get("signal") or "")[:120] for c in substantial + my_substantial]
        if write:
            meta["last_checked"] = checked_at; meta["last_check_reason"] = check_reason
            meta["pending_candidates"] = {"competitor": substantial, "my_company": my_substantial,
                                          "since": since, "my_since": my_since, "reason": "run ceiling",
                                          "attempts": int(pending.get("attempts") or 0)}
            store.write_baseline(slug, claims, meta, _current_md(slug))
            _step(steps, "write", "ran", "heartbeat, candidates carried")
            if sig_open:
                _consume_signals(slug, sig_open, checked_at)
            _sensor_consume(slug, sens, checked_at)
        if sens:
            result["sensors"] = _sensor_compare(slug, meta, sens, candidates, [], steps, write, checked_at, triage_ran)
        return result

    # COMPETITOR ARM: Opus materiality -> multi-source grounding -> bounded retry. Runs only when
    # competitor signals are substantial; otherwise the my_company arm is why we escalated. A crash
    # here (a budget hit, an SDK error) no longer takes the check down or triggers a re-run that
    # re-bills the detection: the candidates are carried to the next run and the spend is recorded.
    comp_failed = my_failed = False
    if substantial:
        try:
            material_grounded, grounded, immaterial = _competitor_arm(slug, meta, since, substantial, claims, result, sig_block=sig_block + ctx_block)
            _step(steps, "materiality", *_arm_status(len(material_grounded), len(substantial), "grounded",
                                                      result.get("materiality_emitted", 0), result.get("materiality_schema_rejected") or []),
                  result["cost"].get("materiality"))
        except Exception as e:
            comp_failed = True
            spent = getattr(e, "scout_cost_usd", None)
            result["cost"]["materiality"] = (result["cost"].get("materiality") or 0) + (spent or 0)
            result["materiality_error"] = f"{type(e).__name__}: {e}"
            print(f"[monitor] competitor arm FAILED ({type(e).__name__}: {e}); candidates carried", file=sys.stderr)
            _step(steps, "materiality", "failed", f"{type(e).__name__}: {e}; {len(substantial)} candidate(s) carried to the next run", spent)
            material_grounded, grounded, immaterial = [], {"kept": [], "cut": [], "results": []}, []
    else:
        _step(steps, "materiality", "skipped", "no substantial competitor candidate")
        material_grounded, grounded, immaterial = [], {"kept": [], "cut": [], "results": []}, []
    updated_ids: set = set()
    new_claims, new_alerts = _apply_updates(
        claims, material_grounded, meta.get("alerted_fingerprints", []), updated_ids)
    result["material"] = [
        {"subject_key": c["subject_key"], "alert": a} for c, a in material_grounded]

    # MY_COMPANY ARM (propagation §17): ground OUR developments into tracked_facts anchors. In LIVE,
    # persist the anchors (non-rendered) + alert, so the derived objections resolve their source and
    # the retire-cascade can track them; in SHADOW, in-memory only — the decision log is the record.
    my_grounded = []
    my_grounding: dict = {"kept": [], "cut": [], "results": []}
    if do_my:
        try:
            myf = _my_company_facts(slug, meta, my_since, my_substantial, claims)
            my_grounding = myf.get("grounding") or my_grounding
            result["cost"]["my_company"] = myf["cost"]
            my_grounded = myf["grounded"]
            result["my_company_facts"] = [
                {"subject_key": f["subject_key"], "alert": a} for f, a in my_grounded]
            if write and config.PROPAGATE_MODE == "live" and my_grounded:
                new_claims, my_alerts = _apply_updates(
                    new_claims, my_grounded, meta.get("alerted_fingerprints", []), updated_ids)
                new_alerts = new_alerts + my_alerts
            _step(steps, "own_company", *_arm_status(len(my_grounded), len(my_substantial), "anchor fact(s) grounded",
                                                     myf.get("emitted", 0), myf.get("schema_rejected") or []),
                  myf.get("cost"))
        except Exception as e:  # NON-DISRUPTION: the new arm must never break the competitor monitor
            my_failed = True
            spent = getattr(e, "scout_cost_usd", None)
            result["cost"]["my_company"] = (result["cost"].get("my_company") or 0) + (spent or 0)
            print(f"[monitor] my_company arm skipped ({type(e).__name__}: {e})", file=sys.stderr)
            my_grounded = []
            result["my_company_error"] = f"{type(e).__name__}: {e}"
            _step(steps, "own_company", "failed", f"{type(e).__name__}: {e}; {len(my_substantial)} candidate(s) carried to the next run", spent)
    else:
        _step(steps, "own_company", "skipped",
              "no own-side substantial candidate" if config.PROPAGATE_MODE in ("shadow", "review", "live") else "propagation off")

    result["alerts"] = new_alerts

    # CARRY-OVER BOOKKEEPING: a failed arm re-presents its candidates next run. A competitor arm that
    # RAN and grounded nothing keeps its held window below (as before). An own-side arm that ran and
    # grounded nothing gets ONE more attempt next run (parity with the competitor side's re-scans; it
    # used to hold the COMPETITOR window, which re-scanned the wrong side: the 10/3 ChatGPT Pro
    # usage-limit cut on the OpenAI vs Anthropic card was found, not grounded, and would have been
    # lost until the sweep). Bounded, then abandoned loudly.
    carry = {}
    if comp_failed:
        carry["competitor"] = substantial
    if do_my and (my_failed or (not my_grounded and int(pending.get("attempts") or 0) < 1)):
        carry["my_company"] = my_substantial
    if carry:
        attempts = int(pending.get("attempts") or 0) + 1
        if attempts < config.MONITOR_MAX_UNRESOLVED_RETRIES:
            meta["pending_candidates"] = {**carry, "since": since, "my_since": my_since, "attempts": attempts,
                                          "reason": "failed step" if (comp_failed or my_failed) else "own-side candidates did not ground"}
            result["carry_over"] = {k: len(v) for k, v in carry.items()}
        else:
            meta.pop("pending_candidates", None)
            result["abandoned_carry_over"] = [str(c.get("signal") or "")[:120] for v in carry.values() for c in v]
            _step(steps, "carry_over", "failed",
                  f"abandoned after {attempts} attempts: " + "; ".join(result["abandoned_carry_over"])[:300])
    elif pending:
        meta.pop("pending_candidates", None)

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
                "gated": prop.get("gated"),               # "routine" = the conseq gate deferred this set
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
                # The same protections the review path runs on approval (2026-09-29, the live
                # flip): refresh the anchor facts the ops derive from, apply, then the PROVENANCE
                # GATE. If citations regressed or disagree with what the judge confirmed, nothing
                # from this card's propagation is written and the ops go to the "needs you" email.
                from scout.review import provenance_issues, refresh_anchor_facts
                facts_all = change_facts + strength_facts
                fb = {f.get("id"): f for f in facts_all if f.get("id")}
                pre_claims = new_claims
                claims_in = refresh_anchor_facts(pre_claims, [fb[f] for f in {o.get("derived_from") for o in auto_ops} if f in fb])
                ap = apply_ops(claims_in, auto_ops, facts_all, slug, today)
                landed = {(a.get("subject_key"), a.get("operation")) for a in ap["applied"]}
                issues = provenance_issues(claims_in, ap["claims"],
                                           [o for o in auto_ops if (o.get("subject_key"), o.get("operation")) in landed], facts_all)
                if issues:
                    for i in issues:
                        print(f"[monitor] PROVENANCE GATE ({slug}): {i}", file=sys.stderr)
                    result["propagation"]["applied"] = []
                    result["propagation"]["skipped"] = ap["skipped"]
                    result["propagation"]["held"] = ap.get("held", [])
                    result["propagation"]["provenance_issues"] = issues
                    new_claims = pre_claims                     # the card keeps its pre-propagation state
                else:
                    new_claims = ap["claims"]
                    result["propagation"]["applied"] = ap["applied"]
                    result["propagation"]["skipped"] = ap["skipped"]
                    result["propagation"]["held"] = ap.get("held", [])
                    # SURFACE RETIREMENTS in the updates feed: an applied retire writes its feed_note as an
                    # alert so the left panel shows the removal, never a silent disappearance.
                    new_alerts.extend(_retire_feed_alerts(prop["confirmed"], ap["applied"]))
            # LEAD ELECTION auto-apply (2026-08-08): a promoted angle reorders the exec-summary lead
            # in review AND live (owner: auto-apply + monitor, no approval gate; shadow never applies).
            # The reorder rides the existing write path below (regenerates current.md + writes the
            # baseline); its feed alert makes the angle change visible in the updates panel, and the
            # per-card cooldown state on meta enforces the anti-churn lock on future runs.
            el = prop.get("election")
            if write and config.PROPAGATE_MODE in ("review", "live") and el and el.get("promoted"):
                new_claims = promote_lead(new_claims, el["winner_key"])
                meta.setdefault("lead_election", {}).update(
                    {"last_promoted_on": today, "lead_subject_key": el["winner_key"]})
                new_alerts.append(_lead_election_alert(el))
                result["propagation"]["lead_election"] = {
                    "promoted": True, "winner_key": el["winner_key"],
                    "incumbent_key": el["incumbent_key"], "margin": el["margin"]}
            _p = result["propagation"]
            _step(steps, "propagation", "ran",
                  f"{_p['ops']} op(s), {_p['confirmed']} confirmed, {len(_p.get('applied') or [])} applied"
                  + (f", {len(_p['provenance_issues'])} provenance issue(s): nothing written" if _p.get("provenance_issues") else "")
                  + (", deferred by the consequentiality gate" if _p.get("gated") == "routine" else "")
                  + (", angle promoted" if _p.get("lead_election") else ""),
                  result["cost"].get("propagation"))
        except Exception as e:  # NON-DISRUPTION: propagation failure must not drop the monitor update...
            print(f"[monitor] propagation FAILED ({type(e).__name__}: {e})", file=sys.stderr)
            result["propagation_error"] = f"{type(e).__name__}: {e}"
            # ...but it must NOT be silent: surface it loudly so a stale card can't look clean (the 7/1
            # miss). run_all folds pipeline_health into the digest email.
            result["pipeline_health"] = f"propagation FAILED on {slug}: {type(e).__name__}: {e}"
            _step(steps, "propagation", "failed", f"{type(e).__name__}: {e}", getattr(e, "scout_cost_usd", None))
    else:
        _step(steps, "propagation", "skipped",
              "no act-grade grounded fact" if config.PROPAGATE_MODE in ("shadow", "review", "live") else "propagation off")

    # AUDIENCE LEADS (Level 2, 2026-10-02): Today's angle written for each buyer present on the
    # card, through author -> floor -> judge -> apply like any edit, on every written check. Zero
    # spend when every buyer's lead already matches the current lead. Own non-disruption guard: a
    # failure here can never touch what propagation already landed.
    if write and config.PROPAGATE_MODE == "live" and config.AUDIENCE_LEADS:
        try:
            from scout import audience
            aud = audience.refresh(slug, meta, new_claims, checked_at[:10], write=True)   # `today` only exists inside the propagation block
            if aud.get("cost_usd"):
                result["cost"]["audience"] = aud["cost_usd"]
            if aud["applied"]:
                new_claims = aud["claims"]
                new_alerts.append(_audience_alert(aud, meta))   # rides the write path below
            if aud["personas"] or aud.get("skipped"):
                result["audience"] = {"personas": aud["personas"], "applied": aud["applied"],
                                      "rejected": aud["rejected"], "skipped": aud.get("skipped")}
            if aud.get("skipped") and not aud["personas"]:
                _step(steps, "audience", "skipped", aud["skipped"])
            else:
                _step(steps, "audience", "ran",
                      f"{len(aud['applied'])} lead(s) written, {len(aud['rejected'])} rejected, {len(aud['personas'])} buyer(s) present"
                      + (", every buyer's lead current" if not aud["applied"] and not aud["rejected"] and aud["personas"] else ""),
                      aud.get("cost_usd"))
        except Exception as e:
            print(f"[monitor] audience leads FAILED ({type(e).__name__}: {e})", file=sys.stderr)
            result["audience_error"] = f"{type(e).__name__}: {e}"
            _step(steps, "audience", "failed", f"{type(e).__name__}: {e}", getattr(e, "scout_cost_usd", None))
    else:
        _step(steps, "audience", "skipped",
              "dry run" if not write else ("audience leads off" if not config.AUDIENCE_LEADS else f"propagation mode {config.PROPAGATE_MODE}"))

    # Shadow-eval observer (v3.5): on a real escalated check, record the champion grounding
    # decisions for offline challenger scoring. No-op unless SCOUT_SHADOW_EVAL=1; never raises.
    if write:
        # BOTH arms in one grounding record (2026-10-04: the own-side arm's grounding was never captured,
        # and _ground_best dropped the per-claim results, so the eval lanes saw neither)
        both = {"kept": list(grounded.get("kept") or []) + list(my_grounding.get("kept") or []),
                "cut": list(grounded.get("cut") or []) + list(my_grounding.get("cut") or []),
                "results": list(grounded.get("results") or []) + list(my_grounding.get("results") or [])}
        shadow.capture(slug, "monitor", kept=both["kept"], cut=both["cut"],
                       grounding=both, competitor=meta.get("competitor"),
                       my_company=meta.get("my_company"), focus=meta.get("focus"))
        # DERIVED CAPTURE (2026-09-28): today's new/revised interpretations WITH their parent facts,
        # so the challenger judges support on plays/objections/summaries too (live-mode applies land
        # here; review-mode applies are captured in review.apply). No-op when nothing derived changed.
        shadow.capture_derived(slug, "monitor", new_claims, checked_at[:10],
                               competitor=meta.get("competitor"),
                               my_company=meta.get("my_company"), focus=meta.get("focus"))
        # DISMISSAL CAPTURE: what this run surfaced but did NOT alert on (triage candidates +
        # materiality immaterial verdicts + own-company signals), so the dismissals are auditable —
        # the silent-miss surface the eval's "never drop anything important" bar cares about.
        shadow.dismissal_capture(
            slug, run_ts=checked_at, candidates=candidates, immaterial=immaterial,
            became_material=[c["subject_key"] for c, _ in material_grounded],
            alerts=new_alerts, my_substantial=my_substantial,
            competitor=meta.get("competitor"), my_company=meta.get("my_company"))
        _step(steps, "shadow_capture", "ran" if config.SHADOW_EVAL_ENABLED else "skipped",
              "grounding, derived and dismissal records" if config.SHADOW_EVAL_ENABLED else "shadow eval off")

    if write and new_alerts:
        meta["last_checked"] = checked_at; meta["last_check_reason"] = check_reason
        # A landed alert resolves the held window ONLY if it matches a held subject — an unrelated
        # catch keeps the window open (the 7/1 miss: Copilot's alert erased the Fable window).
        had_window = bool(meta.get("unresolved_since"))
        _resolve_or_hold(meta, new_alerts, result, decided=_judged_immaterial_subjects(substantial, immaterial))
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
        _append_alerts(slug, _stamp_triggers(new_alerts, sig_open))
        _step(steps, "write", "ran", f"{len(new_alerts)} alert(s) written, card regenerated"
              + (", held window resolved" if had_window and not meta.get("unresolved_since") else "")
              + (", held window kept open" if result.get("unresolved_held") else ""))
    elif write and substantial and not comp_failed:
        # SUBSTANTIAL competitor development detected, the arm ran, nothing landed (nothing survived
        # grounding+retry). Do NOT lose it: always advance last_checked (keeps the due-gate honest /
        # no same-window storm), but HOLD the detection window open at `since` so the next check
        # re-attempts it — bounded, so a genuinely ungroundable item can't make us re-escalate the
        # Opus judge forever. Record WHICH subjects the window is held for, so only a matching later
        # alert resolves it. (An own-side arm that grounded nothing no longer holds this window: it
        # is the competitor cutoff, and re-scanning it never re-found an own-side story.)
        meta["last_checked"] = checked_at; meta["last_check_reason"] = check_reason
        decided = _judged_immaterial_subjects(substantial, immaterial)
        if immaterial and len(immaterial) + len(material_grounded) >= len(substantial):
            decided |= {_norm_key(c.get("subject_key")) for c in substantial if c.get("subject_key")}   # the judge ruled on every candidate
        # a subject the judge has decided never holds; "NEW" cannot be matched by key and never holds either
        open_subs = [c for c in substantial if c.get("subject_key") and str(c.get("subject_key")).upper() != "NEW"
                     and _norm_key(c.get("subject_key")) not in decided]
        subs = {str(c.get("subject_key")) for c in open_subs}
        # hold ONLY for subjects still undecided: a verdict of immaterial is a decision, not a miss
        if subs:
            meta["unresolved_subjects"] = sorted(set(meta.get("unresolved_subjects") or []) | subs)
            meta["unresolved_reasons"] = {**(meta.get("unresolved_reasons") or {}),
                                          **{str(c.get("subject_key")): "found but not verified against a source" for c in open_subs}}
            _hold_window(meta, since, result)
            if "abandoned_window" in result:
                # Bound hit: gave up re-scanning, but SURFACE the abandonment (never silent).
                result["abandoned_substantial"] = [c.get("signal") for c in open_subs]
            hold_note = "held window " + ("abandoned (bound reached)" if "abandoned_window" in result else f"kept open since {since}")
        elif meta.get("unresolved_since"):
            # an older window: closes when every subject it waits for has now been decided
            _resolve_or_hold(meta, [], result, decided=decided)
            hold_note = ("held window resolved (every held subject judged immaterial)" if result.get("unresolved_resolved")
                         else "held window abandoned (bound reached)" if "abandoned_window" in result
                         else f"held window kept open since {since}")
        else:
            hold_note = "nothing to hold"
        store.write_baseline(slug, claims, meta, _current_md(slug))
        _step(steps, "write", "ran", "nothing landed; "
              + (f"{len(decided)} candidate(s) judged immaterial; " if decided else "") + hold_note)
    elif write:
        # Escalated but nothing to write (a failed arm carried its candidates; the own-side arm ran
        # and grounded nothing; SHADOW/REVIEW propose without writing). Advance the gate.
        meta["last_checked"] = checked_at; meta["last_check_reason"] = check_reason
        store.write_baseline(slug, claims, meta, _current_md(slug))
        _step(steps, "write", "ran", "heartbeat" + (", candidates carried to the next run" if result.get("carry_over") else ""))
    else:
        _step(steps, "write", "skipped", "dry run")
    if write and sig_open:                       # every written check consumes what it was shown
        _consume_signals(slug, sig_open, checked_at)
        _step(steps, "signals_consume", "ran", f"{len(sig_open)} consumed")
    if write:
        _sensor_consume(slug, sens, checked_at)
    if sens:
        # BOTH arms' landed facts are compared (2026-10-05: own-side alerts were never checked against
        # the sensors, so a Level A miss on our own news could not be counted)
        result["sensors"] = _sensor_compare(slug, meta, sens, candidates, list(material_grounded) + list(my_grounded),
                                            steps, write, checked_at, triage_ran)
        if result["sensors"] and not result["sensors"].get("error"):
            sample = _screen_sample(slug, meta, since, claims, sens, result["sensors"], steps, write, checked_at)
            if sample:
                result["sensors"]["sample"] = {"judged": sample["judged"], "material": sample["n_material"], "cost": sample["cost_usd"]}
    return result


def _current_md(slug):
    path = os.path.join(store.battlecard_dir(slug), "current.md")
    return open(path).read() if os.path.exists(path) else ""


def _stamp_triggers(alerts: list, opened: list) -> list:
    """An alert whose source is a signal's document (the filing's accession in the URL, or the job
    board) carries `triggered_by`, so the viewer can show "Triggered by: new 8-K". Deterministic."""
    if not opened:
        return alerts
    for a in alerts or []:
        url = str(a.get("source_url") or "")
        for sg in opened:
            acc = str(sg.get("accession") or "").replace("-", "")
            if (acc and acc in url.replace("-", "")) or (sg.get("source_url") and url.startswith(str(sg["source_url"]))):
                a["triggered_by"] = {"kind": sg.get("kind"), "summary": sg.get("summary"), "fingerprint": sg.get("fingerprint")}
                break
    return alerts


def _consume_signals(slug: str, opened: list, run_ts: str) -> None:
    if not opened:
        return
    try:
        from scout import signals
        signals.consume(slug, run_ts, [s.get("fingerprint") for s in opened])
    except Exception as e:
        print(f"[monitor] signals consume skipped ({type(e).__name__}: {e})", file=sys.stderr)


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
    # A signal-dispatched check inside the last MONITOR_SIGNAL_GAP_H hours serves the next anchor
    # (WS3): the trigger was the day's check, not an extra one.
    if meta.get("last_check_reason") == "signal" and (now - last) < timedelta(hours=config.MONITOR_SIGNAL_GAP_H):
        return False
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
        from scout import generate
        run_total = round(sum(r.get("total") or 0 for r in rows), 4)
        # $/claim (schema v2): the run's efficiency headline. Claims per card = direct material
        # patches + judge-confirmed proposals (see run_all); None when the run shipped nothing —
        # a quiet day has a cost but no meaningful per-claim rate.
        run_claims = sum(r.get("claims") or 0 for r in rows)
        doc = {
            "schema_version": 2,
            "run_ts": started.isoformat(timespec="seconds"),
            "mode": config.PROPAGATE_MODE,
            "run_total_usd": run_total,
            "run_claims": run_claims,
            "run_usd_per_claim": round(run_total / run_claims, 4) if run_claims else None,
            "cards": rows,
            # Run-level token totals per role (input/output/cache_read/cache_creation/messages):
            # makes cache behavior and per-phase input weight measurable across runs.
            "by_role": dict(generate.ROLE_TOTALS),
        }
        selfserve.write_data(
            f"{COST_DIR}/{stamp}.json",
            json.dumps(doc, indent=2, default=str, ensure_ascii=False),
            f"costs: monitor run {stamp} (${doc['run_total_usd']}, {len(rows)} cards)",
        )
        per_claim = (f", {run_claims} claim(s), ${doc['run_usd_per_claim']}/claim" if run_claims
                     else ", 0 claims")
        print(f"[cost] run {stamp}: ${doc['run_total_usd']} across {len(rows)} card(s){per_claim} "
              f"-> {COST_DIR}/{stamp}.json")
    except Exception as e:   # the cost ledger must never break a live monitor run
        print(f"[cost] ledger write skipped ({type(e).__name__}: {e})", file=sys.stderr)


def run_all(write: bool = True, send: bool = True, email_dry_run: bool = True,
            force: bool = False, slugs: list | None = None, quiet: bool = False,
            since_override: str | None = None) -> list[dict]:
    """Thin wrapper (2026-09-28): opens the call-capture run and guarantees it is flushed even when a
    run crashes (a crashed run still captured billable calls). The body is _run_all_impl, unchanged.
    `slugs` (2026-09-28, WS1/WS3): check only these cards (a dry test run, a signal-triggered run).
    `since_override` (2026-10-03, SCOUT_MONITOR_SINCE): force every checked card's detection cutoff
    (a rehearsal that must exercise escalation)."""
    from scout import calllog
    calllog.begin_run("monitor")          # no-op unless SCOUT_CALL_CAPTURE=1
    try:
        return _run_all_impl(write=write, send=send, email_dry_run=email_dry_run, force=force, slugs=slugs, quiet=quiet,
                             since_override=since_override)
    finally:
        if not write and os.environ.get("SCOUT_MONITOR_TRACE") == "1":
            print(calllog.trace_summary(), flush=True)      # dry test runs: show the tool calls
        calllog.flush_run(write)


def _run_all_impl(write: bool = True, send: bool = True, email_dry_run: bool = True,
            force: bool = False, slugs: list | None = None, quiet: bool = False,
            since_override: str | None = None) -> list[dict]:
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

    from scout import generate
    generate.reset_role_totals()      # run-level token accumulator for the cost ledger
    run_started = datetime.now()
    summary = []
    cost_rows = []
    fyi_cards, issue_cards = [], []          # LIVE mode: one FYI + one "needs you" per run
    health_rows = []                         # every card's step table (2026-10-03): the FYI footer + the ledger
    sensor_rows = []                         # per-card compare rows (Release 2): the streak + the FYI block
    run_spend = 0.0                          # the running total the run ceiling is measured against
    wanted = set(slugs) if slugs else None
    today = run_started.date().isoformat()
    if config.SENSORS_MODE != "off":
        # SENSORS: one pass for every entity on a card this run will check, before the loop
        due_slugs = []
        for slug in list_battlecards():
            if wanted is not None and slug not in wanted:
                continue
            meta = store.load_meta(slug) or {}
            if meta.get("monitored") is False or (not force and not _is_due(meta)):
                continue
            due_slugs.append(slug)
        _sensor_pass(due_slugs, write, today)
    for slug in list_battlecards():
        if wanted is not None and slug not in wanted:
            summary.append({"slug": slug, "skipped": "not selected"})
            continue
        meta = store.load_meta(slug) or {}
        # Showcase cards can opt out of monitoring (e.g. the Batman vs Superman
        # stress-test card): it still renders in the viewer but never burns a check.
        if meta.get("monitored") is False:
            summary.append({"slug": slug, "skipped": "not monitored"})
            health_rows.append({"slug": slug, "meta": meta, "skipped": "not monitored"})
            continue
        if not force and not _is_due(meta):
            summary.append({"slug": slug, "skipped": "not due",
                            "cadence_hours": meta.get("cadence_hours") or config.DEFAULT_CADENCE_HOURS,
                            "last_checked": meta.get("last_checked")})
            continue
        # RUN CEILING (2026-10-03): once the run's total crosses SCOUT_RUN_MAX_USD, the remaining
        # cards get triage only and carry their candidates to the next run (named in needs-you).
        # Extra kwargs are passed only when they apply, so the test fakes keep their call shape.
        kw = {}
        if since_override:
            kw["since_override"] = since_override
        if config.RUN_MAX_USD and run_spend >= config.RUN_MAX_USD:
            kw["escalate"] = False
        try:
            res = check(slug, write=write, **kw)
        except Exception as first_err:
            # One bounded retry: a transient SDK/API hiccup (observed 2026-06-10: the agent
            # subprocess surfaced an error result mid-stream) must not kill the cron run.
            # check() writes the store only at its very end, so a failed attempt leaves the
            # card untouched and is safe to redo. Worst-case extra spend is one more check.
            # (Since 2026-10-03 a failed PAID arm no longer raises: it carries its candidates, so
            # this retry only ever re-runs the cheap part.)
            print(f"check({slug}) failed ({type(first_err).__name__}: {first_err}) — retrying once")
            try:
                res = check(slug, write=write, **kw)
            except Exception as e:
                # Record the failure and move on: one bad card must not block the other
                # cards' checks (or the workflow committing their results). __main__ exits
                # non-zero when any card errored, so the Actions run still notifies.
                summary.append({"slug": slug, "error": f"{type(e).__name__}: {e}"})
                steps_so_far = list(getattr(e, "scout_steps", None) or [])
                health_rows.append({"slug": slug, "meta": store.load_meta(slug) or {}, "steps": steps_so_far,
                                    "error": f"{type(e).__name__}: {e}"})
                if send and config.PROPAGATE_MODE == "live":     # a failed card is a "needs you" item
                    errs = [f"check failed twice: {type(e).__name__}: {e}"]
                    errs += [f"{r['step']}: {r.get('detail') or 'failed'}" for r in steps_so_far if r.get("status") == "failed"]
                    issue_cards.append({"slug": slug, "meta": store.load_meta(slug) or {}, "errors": errs})
                continue
        # $/claim (2026-07-08, his metric): claims this run = direct material patches + judge-
        # CONFIRMED proposals (confirmed = produced and sent for approval; a later human decline
        # doesn't refund the production cost, so confirmed is the honest denominator). Computed
        # once here and surfaced everywhere the run reports itself: both emails + the cost ledger.
        _prop_all = res.get("propagation") or {}
        claims_n = len(res["material"]) + sum(1 for d in _prop_all.get("decisions", [])
                                              if d.get("judge_verdict") == "confirm")
        card_cost = _run_total(res["cost"])
        run_spend += card_cost
        health_rows.append({"slug": slug, "meta": store.load_meta(slug) or {}, "steps": list(res.get("steps") or []),
                            "cost": card_cost, "alerts": len(res.get("alerts") or [])})
        if res.get("sensors"):
            sensor_rows.append(dict(res["sensors"], meta=store.load_meta(slug) or {}))
        # STEP FAILURES ARE NEEDS-YOU ITEMS (2026-10-03): every failed step row, an abandoned held
        # window, an abandoned carry-over and a ceiling deferral reach the owner, in live mode.
        step_errors = [f"{r['step']}: {r.get('detail') or 'failed'}" for r in (res.get("steps") or []) if r.get("status") == "failed"]
        if res.get("abandoned_window"):
            aw = res["abandoned_window"]
            subjects = [x for x in (aw.get("subjects") or []) if str(x).upper() != "NEW"]
            if subjects:
                step_errors.append(f"not verified after {config.MONITOR_MAX_UNRESOLVED_RETRIES} mornings of re-checks since {aw.get('since')}, "
                                   "no longer re-checked (a source Scout could not match the claim against): " + ", ".join(subjects))
        if res.get("abandoned_carry_over"):
            step_errors.append("carried candidates abandoned: " + "; ".join(res["abandoned_carry_over"])[:300])
        if res.get("ceiling_deferred"):
            step_errors.append(f"run ceiling ${config.RUN_MAX_USD:.0f} reached before this card: "
                               f"{len(res['ceiling_deferred'])} candidate(s) carried to the next run: "
                               + "; ".join(res["ceiling_deferred"])[:300])
        cost_note = (f"Run cost: ${card_cost:.2f} — {claims_n} claim{'s' if claims_n != 1 else ''}, "
                     f"${card_cost / claims_n:.2f}/claim" if claims_n
                     else f"Run cost: ${card_cost:.2f} — no claims shipped")
        emailed = prop_emailed = None
        live_batch = send and config.PROPAGATE_MODE == "live"
        if live_batch:
            # LIVE (2026-09-29): nothing per card. Collect what happened and what needs him; the
            # run sends one cumulative FYI and, only when needed, one "needs you" email (below).
            meta = store.load_meta(slug) or {}
            prop0 = res.get("propagation") or {}
            decisions = prop0.get("decisions", [])
            applied_keys = {(a.get("subject_key"), a.get("operation")) for a in prop0.get("applied", [])}
            applied = [d for d in decisions if (d.get("subject_key"), d.get("operation")) in applied_keys]
            gated_n = sum(1 for d in decisions if d.get("judge_verdict") == "gated_routine") if prop0.get("gated") == "routine" else 0
            el_rec = next((d for d in decisions if d.get("judge_verdict") == "lead_election" and d.get("lead_promoted")), None)
            if res["alerts"] or applied or gated_n or el_rec:
                fyi_cards.append({"slug": slug, "meta": meta, "alerts": res["alerts"], "applied": applied,
                                  "deferred_n": gated_n, "election": el_rec if config.LEAD_ELECTION else None})
            held = [d for d in decisions if d.get("held_for_format")]
            confirmed_not_applied = [d for d in decisions if d.get("judge_verdict") == "confirm" and not d.get("held_for_format")
                                     and (d.get("subject_key"), d.get("operation")) not in applied_keys
                                     and str(d.get("judged_by") or "").startswith("fallback:")]
            errors = list(step_errors)
            if res.get("my_company_error") and not any(e_.startswith("own_company:") for e_ in errors):
                errors.append(res["my_company_error"])
            issue = {"slug": slug, "meta": meta, "held": held + [dict(d, held_reason="confirmed by the fallback judge only; not applied unattended") for d in confirmed_not_applied],
                     "unjudged": [d for d in decisions if d.get("judge_verdict") == "judge_unavailable"],
                     "exhausted": [d for d in decisions if d.get("rewrite_exhausted")],
                     "provenance_issues": prop0.get("provenance_issues") or [],
                     "pipeline_health": res.get("pipeline_health"),
                     "errors": errors}
            if any(issue[k] for k in ("held", "unjudged", "exhausted", "provenance_issues", "pipeline_health", "errors")):
                issue_cards.append(issue)
            urgent = [d for d in decisions if d.get("material_uncured")]
            if urgent and config.PROPAGATE_URGENT_EMAIL and not quiet:
                try:
                    notify.send_urgent_material(slug, meta, urgent, dry_run=email_dry_run)
                except Exception as e:
                    print(f"[monitor] urgent-material alert skipped ({type(e).__name__}: {e})", file=sys.stderr)
            elif urgent and quiet:
                issue["exhausted"] = list(issue["exhausted"]) + [dict(d, held_reason="urgent, from a dispatched run") for d in urgent
                                                                 if d not in issue["exhausted"]]
                if issue not in issue_cards:
                    issue_cards.append(issue)
            if write and prop0 and config.CONSEQUENTIAL_FILTER != "off" and prop0.get("run_verdict"):   # a dry run writes nothing (2026-10-03)
                shadow.filter_capture(slug, run_ts=res.get("last_checked"), verdict=prop0["run_verdict"],
                                      act_subject_keys=[m["subject_key"] for m in res.get("material", [])],
                                      competitor=meta.get("competitor"), my_company=meta.get("my_company"),
                                      mode=config.CONSEQUENTIAL_FILTER)
            emailed = {"batched": True}
        if send and not live_batch:
            meta = store.load_meta(slug) or {}
            # Consequentiality-gate audit line (a deferral is never silent): rides the digest when
            # one goes out; a routine set with no alerts gets its own one-liner below.
            prop0 = res.get("propagation") or {}
            gated_n = sum(1 for d in prop0.get("decisions", [])
                          if d.get("judge_verdict") == "gated_routine")
            deferred_note = (f"Routine run: {gated_n} routed update(s) deferred by the "
                             f"consequentiality gate (each is in the decision log)."
                             if prop0.get("gated") == "routine" and gated_n else None)
            if res["alerts"]:
                emailed = notify.send_digest(meta, res["alerts"], dry_run=email_dry_run,
                                             deferred_note=deferred_note, cost_note=cost_note)
            elif deferred_note:
                try:
                    notify._dispatch(f"Scout: routine run — {gated_n} update(s) deferred — "
                                     f"{meta.get('my_company') or ''} vs "
                                     f"{meta.get('competitor') or slug}".strip(),
                                     deferred_note, dry_run=email_dry_run)
                except Exception as e:
                    print(f"[monitor] deferred-note alert skipped ({type(e).__name__}: {e})",
                          file=sys.stderr)
            # REVIEW/LIVE: email each judge-confirmed propagation proposal awaiting approval. In
            # review the card is untouched (human approves in-session); in live it's already applied.
            # Rewrite-EXHAUSTED failures ride the same email — an act-grade edit that died in
            # authoring must reach the owner even when nothing was confirmed (never a silent drop).
            prop = res.get("propagation")
            if prop and config.PROPAGATE_MODE in ("review", "live"):
                decisions = prop.get("decisions", [])
                # held_for_format: judge-confirmed but stopped by the pre-email render gate — rides
                # the SAME email as a NEEDS-CURING callout (never a silent hold, never a 2nd email).
                held = [d for d in decisions if d.get("held_for_format")]
                confirmed = [d for d in decisions if d.get("judge_verdict") == "confirm"
                             and not d.get("held_for_format")]
                exhausted = [d for d in decisions if d.get("rewrite_exhausted")]
                unjudged = [d for d in decisions if d.get("judge_verdict") == "judge_unavailable"]
                if confirmed or exhausted or unjudged or held:
                    prop_emailed = notify.send_propagation_proposals(
                        slug, meta, confirmed, dry_run=email_dry_run, exhausted=exhausted,
                        unjudged=unjudged, held=held, cost_note=cost_note)
                # URGENT MATERIAL (2026-07-31): a DEAL-MOVING point that could not be authored
                # (cure exhausted, or judge ruled material but cure:none) gets a SEPARATE urgent
                # email so it is never lost. Superset of `exhausted` (also the never-drafted
                # cure:none case). Belt-and-suspenders: the proposals email keeps its AUTHORING-
                # FAILED section too. Guarded like the pipeline-health alert below — never breaks
                # the run.
                urgent = [d for d in decisions if d.get("material_uncured")]
                if urgent and config.PROPAGATE_URGENT_EMAIL:
                    try:
                        notify.send_urgent_material(slug, meta, urgent, dry_run=email_dry_run)
                    except Exception as e:
                        print(f"[monitor] urgent-material alert skipped ({type(e).__name__}: {e})",
                              file=sys.stderr)
                # LEAD ELECTION FYI (2026-08-08): an auto-applied angle change is never silent — a
                # separate FYI email (the owner's "we monitor it" push surface, distinct from the
                # feed row and the proposals email). Guarded; never breaks the run.
                el_rec = next((d for d in decisions if d.get("judge_verdict") == "lead_election"
                               and d.get("lead_promoted")), None)
                if el_rec and config.LEAD_ELECTION:
                    try:
                        notify.send_lead_election_fyi(slug, meta, el_rec, dry_run=email_dry_run)
                    except Exception as e:
                        print(f"[monitor] lead-election FYI skipped ({type(e).__name__}: {e})",
                              file=sys.stderr)
            # CONSEQUENTIALITY FILTER (shadow eval, docs/consequential-filter-spec.md): the router now
            # emits the consequential/routine run_verdict the old strategic pass used to (that pass is
            # ABSORBED into the router — the lead is just the executive_summary surface). Log the verdict
            # for the longitudinal eval. DOWNSTREAM of the grounding shadow.capture in check() (Fold A:
            # never alter what the v3.5 grounding eval sees). "shadow" mode changes NOTHING — the verdict
            # is validated over weeks before it can gate. No-op unless SHADOW_EVAL_ENABLED.
            if write and prop and config.CONSEQUENTIAL_FILTER != "off":   # a dry run writes nothing (2026-10-03)
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
            "claims": claims_n,   # direct material patches + judge-confirmed proposals this run
            "usd_per_claim": round(card_cost / claims_n, 4) if claims_n else None,
            "phases": {k: round(v, 6) for k, v in (cost or {}).items() if v is not None},
            "total": _run_total(cost),
            "steps": list(res.get("steps") or []),   # the step table rides the ledger: the canary reads it
        })
    # LIFECYCLE AUDIT (Uroš 2026-10-04): every finding of this run followed from the searches to the
    # card and to the eval lanes, against the promise's rules; RED reaches the needs-you email, the
    # FYI footer and the canary. Reads what the run already wrote; never raises; $0.
    lifecycle_doc = None
    if write:
        try:
            from scout import calllog as _cl, lifecycle as _lc
            lifecycle_doc = _lc.audit(run_started.strftime("%Y%m%dT%H%M%S"), rows=cost_rows, calls=_cl.current_calls(), write=True)
            print(f"[lifecycle] {lifecycle_doc['verdict']}: {lifecycle_doc['summary']}")
            for c in lifecycle_doc.get("cards") or []:
                if c.get("failed"):
                    errs = [f"lifecycle {i['id']}: {i['rule']} ({i['evidence'][:160]})" for i in c["invariants"] if i["status"] == "fail"]
                    issue_cards.append({"slug": c["slug"], "meta": store.load_meta(c["slug"]) or {}, "errors": errs})
        except Exception as e:
            print(f"[lifecycle] audit skipped ({type(e).__name__}: {e})", file=sys.stderr)
    if send and config.PROPAGATE_MODE == "live":
        run_cost = sum(float(r.get("cost_usd") or _run_total(r.get("phases") or {}) or 0) for r in cost_rows)
        if quiet:
            # a DISPATCHED run never emails on its own (Uroš 2026-09-29): what it found waits in the
            # store and rides the next scheduled run's FYI / needs-you email
            if write:
                _stash_pending_fyi(run_started, fyi_cards, issue_cards, run_cost)
            print(f"[monitor] quiet run: {len(fyi_cards)} FYI card(s), {len(issue_cards)} issue card(s) stashed for the next FYI")
        else:
            prior = _take_pending_fyi() if write else []
            for p_ in prior:
                fyi_cards = [dict(c, meta=store.load_meta(c["slug"]) or {}) for c in p_.get("fyi_cards", [])] + fyi_cards
                issue_cards = [dict(c, meta=store.load_meta(c["slug"]) or {}) for c in p_.get("issue_cards", [])] + issue_cards
                run_cost += float(p_.get("cost_usd") or 0)
            sensors_block = None
            if config.SENSORS_MODE != "off" and sensor_rows:
                try:
                    from scout.sensors import compare as _compare
                    streak = _compare.update_streak(today, sensor_rows, gate_runs=config.SENSOR_GATE_RUNS, write=write)
                    sensors_block = {"mode": config.SENSORS_MODE, "rows": sensor_rows, "streak": streak,
                                     "pass_error": _SENSORS.get("error")}
                except Exception as e:
                    print(f"[sensors] streak update skipped ({type(e).__name__}: {e})", file=sys.stderr)
                    sensors_block = {"mode": config.SENSORS_MODE, "rows": sensor_rows, "streak": None, "error": f"{type(e).__name__}: {e}"}
            try:
                fyi = notify.send_run_fyi(fyi_cards, run_cost, dry_run=email_dry_run, health=health_rows, sensors=sensors_block,
                                          lifecycle=lifecycle_doc)
                print(f"[monitor] run FYI: {fyi}")
            except Exception as e:
                print(f"[monitor] run FYI skipped ({type(e).__name__}: {e})", file=sys.stderr)
            try:
                iss = notify.send_run_issues(issue_cards, dry_run=email_dry_run)
                print(f"[monitor] run issues email: {iss}")
            except Exception as e:
                print(f"[monitor] run issues email skipped ({type(e).__name__}: {e})", file=sys.stderr)
    _persist_run_cost(run_started, cost_rows, write)
    # CONSEQ. TRACK: once enough shadow verdicts have accumulated, email a one-time "ready to review"
    # spot-check digest (so the owner knows when to evaluate the filter for production). Best-effort.
    if send and not config.REHEARSAL:            # a rehearsal never speaks for the production eval tracks
        from scout import conseq
        conseq.maybe_notify_ready(send=not email_dry_run)
    return summary


PENDING_FYI = "signals/_pending_fyi.json"


def _strip_meta(cards: list) -> list:
    return [{k: v for k, v in c.items() if k != "meta"} for c in cards]


def _stash_pending_fyi(run_started, fyi_cards: list, issue_cards: list, cost_usd: float) -> None:
    """A dispatched run's would-be emails, appended to the store for the next scheduled run."""
    if not fyi_cards and not issue_cards:
        return
    from scout import selfserve
    entry = {"run_ts": run_started.isoformat(timespec="seconds"), "reason": os.environ.get("SCOUT_MONITOR_REASON", "")[:200],
             "fyi_cards": _strip_meta(fyi_cards), "issue_cards": _strip_meta(issue_cards), "cost_usd": round(cost_usd, 4)}
    def tx(cur):
        try:
            arr = json.loads(cur) if cur else []
        except Exception:
            arr = []
        arr = [a for a in arr if isinstance(a, dict)][-20:] + [entry]
        return json.dumps(arr, indent=1, ensure_ascii=False, default=str)
    try:
        selfserve.update_data(PENDING_FYI, tx, "monitor: pending FYI from a dispatched run")
    except Exception as e:
        print(f"[monitor] pending FYI stash failed ({type(e).__name__}: {e})", file=sys.stderr)


def _take_pending_fyi() -> list:
    """The stashed entries (oldest first), cleared once taken; [] when none or the store is down."""
    from scout import selfserve
    out: list = []
    def tx(cur):
        try:
            arr = json.loads(cur) if cur else []
        except Exception:
            arr = []
        out.extend(a for a in arr if isinstance(a, dict))
        return "[]"
    try:
        if selfserve.read_data(PENDING_FYI):
            selfserve.update_data(PENDING_FYI, tx, "monitor: pending FYI taken by the scheduled run")
    except Exception as e:
        print(f"[monitor] pending FYI read failed ({type(e).__name__}: {e})", file=sys.stderr)
        return []
    return out


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


def preflight(env: dict | None = None) -> str | None:
    """The run's own precondition check, BEFORE any write (2026-09-30). Returns None when the run
    may proceed, else the one-line reason it must not. Today: the store prefix must match the
    ref this run executes from: main writes to the production paths (empty prefix), rc writes
    under rc/. The 2026-09-30 4 AM run wrote 14 files under rc/ from main because a workflow
    expression resolved wrong; this check turns that class of mistake into a refusal + an email
    instead of a silent misfiled run. GITHUB_REF_NAME is unset outside Actions -> no opinion."""
    env = os.environ if env is None else env
    ref = (env.get("GITHUB_REF_NAME") or "").strip()
    prefix = (env.get("SCOUT_SELFSERVE_DATA_PREFIX") or "").strip().strip("/")
    # REHEARSAL (2026-10-03) rules first: the real write path on a retired card, legal on ANY ref,
    # but only with its own private-store prefix AND a card store outside the checkout. Either
    # missing would let a rehearsal write production's paths or the committed cards.
    if (env.get("SCOUT_REHEARSAL") or "").strip() == "1":
        if prefix != "rehearsal":
            return f"rehearsal with store prefix {prefix!r} (rehearsals write under rehearsal/ only)"
        if not (env.get("SCOUT_STORE_ROOT") or "").strip():
            return "rehearsal without SCOUT_STORE_ROOT (it would write the checkout's battlecards/)"
        return _cli_too_old(env)
    if prefix == "rehearsal":
        return "store prefix 'rehearsal' without SCOUT_REHEARSAL=1"
    if ref == "main" and prefix:
        return f"store prefix {prefix!r} on main (production runs write to the root paths)"
    if ref == "rc" and prefix != "rc":
        return f"store prefix {prefix!r} on rc (rc runs write under rc/ only)"
    return _cli_too_old(env)


def _cli_too_old(env) -> str | None:
    """LOCKSTEP (2026-10-03): the bundled Claude Code binary must be new enough for the configured
    models, or the run refuses before any spend (every Opus call would 400 otherwise). Only on a
    runner (GITHUB_REF_NAME set) or when asked (SCOUT_CHECK_CLI=1): a unit test must not shell out."""
    if not ((env.get("GITHUB_REF_NAME") or "").strip() or (env.get("SCOUT_CHECK_CLI") or "") == "1"):
        return None
    try:
        from scout import sdkcheck
        return sdkcheck.too_old()
    except Exception:
        return None


if __name__ == "__main__":
    import json as _json
    _refusal = preflight()
    if _refusal:
        from scout import notify as _notify
        print(f"[monitor] REFUSED: {_refusal}", file=sys.stderr)
        _notify.send_could_not_run("monitor", _refusal,
                                   f"ref={os.environ.get('GITHUB_REF_NAME')!r} prefix={os.environ.get('SCOUT_SELFSERVE_DATA_PREFIX')!r}",
                                   dry_run=os.environ.get("SCOUT_MONITOR_LIVE") != "1")
        raise SystemExit(2)
    # LIVE only when the cron sets SCOUT_MONITOR_LIVE=1: then write the store and send real
    # email. Default (any other context) is fully dry: compute, no writes, no email sent.
    live = os.environ.get("SCOUT_MONITOR_LIVE") == "1"
    # Run controls (2026-09-28): SCOUT_MONITOR_SLUGS (comma list) checks only those cards;
    # SCOUT_MONITOR_FORCE=1 ignores the due gate. Both empty on the scheduled run.
    slugs = [x.strip() for x in os.environ.get("SCOUT_MONITOR_SLUGS", "").split(",") if x.strip()] or None
    force = os.environ.get("SCOUT_MONITOR_FORCE") == "1"
    reason = os.environ.get("SCOUT_MONITOR_REASON", "").strip()
    if reason:   # a signal poller's hint only; the run reads the open signals from the store; QUIET
        print(f"[monitor] dispatched: {reason} (quiet: no emails of its own; findings ride the next FYI)")
    if not live:
        print(f"[monitor] DRY run: no writes, no email (slugs={slugs or 'all'}, force={force})")
    since_override = os.environ.get("SCOUT_MONITOR_SINCE", "").strip() or None
    if since_override:
        from datetime import date as _date
        _date.fromisoformat(since_override)       # YYYY-MM-DD or refuse before any spend
        print(f"[monitor] detection cutoff forced to {since_override} for every checked card")
    if config.REHEARSAL:
        print(f"[monitor] REHEARSAL: store root {store.STORE_ROOT}, private-store prefix {config.SELFSERVE_DATA_PREFIX!r}, emails prefixed [rehearsal]")
    out = run_all(write=live, send=True, email_dry_run=not live, force=force, slugs=slugs, quiet=bool(reason),
                  since_override=since_override)
    print(_json.dumps(out, indent=2, default=str))
    # Partial failure still exits 1 (after the full summary prints) so the Actions run
    # notifies — but only after every card had its chance to check and write.
    if any(r.get("error") for r in out):
        raise SystemExit(1)
