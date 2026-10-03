"""Audience leads (Level 2 of audience mode, 2026-10-02): Today's angle written for one buyer.

Level 1 (page.py) re-cuts a brief from what the card already holds. For the angle itself, three of
five buyers on a typical card have nothing written for them. This module writes it, through the
same gates as every other edit: the author (Sonnet) drafts from the grounded facts behind the
current lead and the buyer's own plays and objections; the deterministic floor checks structure;
the judge (Opus) confirms or rejects; apply_ops stores the result as an executive_summary claim
tagged with the buyer, derived from the lead's anchor fact, so the Verification trail and the Cut
Log cover it. The claim's subject_key carries the id of the lead it was written for; when the lead
changes, the old audience lead is retired and a new one written. No fact is ever minted here.
"""
from __future__ import annotations

import sys

from scout import config, judgment
from scout.schema import PERSONAS

LABELS = {"eng_led": "engineering-led champion", "technical_evaluator": "technical evaluator",
          "economic_buyer": "economic buyer", "security_regulated": "security and regulated buyer",
          "exec_top_down": "executive, top-down buyer"}
SK_PREFIX = "audience-lead"


def _active(claims: list) -> list:
    return [c for c in claims if str(c.get("status", "active")) != "retired"]


def current_lead(claims: list) -> dict | None:
    leads = sorted((c for c in _active(claims) if c.get("section") == "executive_summary"
                    and not c.get("persona")), key=lambda c: c.get("order", 0))
    return leads[0] if leads else None


def audience_lead(claims: list, persona: str) -> dict | None:
    for c in _active(claims):
        if c.get("section") == "executive_summary" and c.get("persona") == persona \
                and str(c.get("subject_key", "")).startswith(SK_PREFIX):
            return c
    return None


def personas_present(claims: list) -> list:
    out = []
    for p in PERSONAS:
        n = sum(1 for c in _active(claims) if c.get("section") == "battlecard" and c.get("persona") == p)
        if n >= config.AUDIENCE_MIN_PLAYS:
            out.append(p)
    return out


def subject_key(persona: str, lead_id: str) -> str:
    return f"{SK_PREFIX} | {persona} | {lead_id}"


def plan(claims: list, limit: int | None = None) -> dict:
    """What this run would write: {'lead': <claim>|None, 'ops': [...], 'facts': [...], 'personas': [...]}.
    Pure. ops are in the router's op shape so propagate.author / floor_check / judge / apply_ops
    take them unchanged. Empty when the lead has no anchor fact on the card."""
    lead = current_lead(claims)
    if not lead or not lead.get("derived_from"):
        return {"lead": lead, "ops": [], "facts": [], "personas": []}
    by_id = {c.get("id"): c for c in claims if c.get("id")}
    anchor = by_id.get(lead["derived_from"])
    if not anchor or not anchor.get("source_url"):
        return {"lead": lead, "ops": [], "facts": [], "personas": []}
    brief = judgment.optional("audience._OP_BRIEF")
    if brief is None or judgment.optional("audience._AUTHOR_ADDENDUM") is None \
            or judgment.optional("audience._JUDGE_ADDENDUM") is None:
        return {"lead": lead, "ops": [], "facts": [], "personas": [],
                "skipped": "the pack lacks an audience block (audience._OP_BRIEF / _AUTHOR_ADDENDUM / _JUDGE_ADDENDUM)"}
    todo = []
    for p in personas_present(claims):
        have = audience_lead(claims, p)
        if have and have.get("subject_key") == subject_key(p, lead["id"]):
            continue                                       # written for this very lead already
        todo.append((p, have))
    if limit is not None:
        todo = todo[:limit]
    ops, fact_ids = [], {anchor["id"]}
    for p, have in todo:
        own = [c for c in _active(claims) if c.get("persona") == p
               and c.get("section") in ("battlecard", "objection_handling")]
        for c in own:
            if c.get("derived_from") in by_id and by_id[c["derived_from"]].get("source_url"):
                fact_ids.add(c["derived_from"])
        # The angle anchors on the buyer's strongest GROUNDED win, not on the general lead's fact:
        # a lead can legitimately open on a back-foot fact (a scar the buyer will raise), and an
        # audience angle built on that reads as opening the brief with our own weakness (the judge
        # rejected exactly that, 2026-10-02). The general lead stays in the brief as context.
        wins = sorted((c for c in own if c.get("section") == "battlecard" and c.get("zone") == "where_we_win"
                       and c.get("derived_from") in by_id and by_id[c["derived_from"]].get("source_url")),
                      key=lambda c: c.get("order", 0))
        if not wins:                                       # no grounded win for this buyer: nothing to lead with
            continue
        own_anchor = by_id[wins[0]["derived_from"]]
        own_valence = "front_foot"
        if have:
            ops.append({"operation": "retire", "section": "executive_summary", "zone": None,
                        "target_subject_key": have.get("subject_key"), "subject_key": have.get("subject_key"),
                        "change_kind": "supersede_retire", "derived_from": own_anchor["id"], "persona": p,
                        "feed_note": "the brief's lead changed; this audience lead is rewritten for the new one"})
        ops.append({"operation": "add", "section": "executive_summary", "zone": None,
                    "subject_key": subject_key(p, lead["id"]), "derived_from": own_anchor["id"],
                    "persona": p, "change_kind": "new", "valence": own_valence,
                    "why": judgment.render(brief, {"persona_label": LABELS.get(p, p),
                                                   "lead_text": lead.get("claim") or "",
                                                   "n_plays": len([c for c in own if c.get("section") == "battlecard"]),
                                                   "n_objections": len([c for c in own if c.get("section") == "objection_handling"])})})
    facts = [by_id[i] for i in fact_ids if i in by_id]
    return {"lead": lead, "ops": ops, "facts": facts, "personas": [p for p, _ in todo if any(o.get("persona") == p for o in ops)]}


def refresh(slug: str, meta: dict, claims: list, today: str, *, write: bool,
            author=None, floor=None, judge=None, apply=None, log=None) -> dict:
    """Author, floor, judge and (when write) apply the audience leads this card needs. Returns
    {'claims', 'applied', 'rejected', 'cost_usd', 'personas', 'skipped'}. Never raises past the
    caller's non-disruption guard; a judge outage leaves the card untouched."""
    from scout import propagate
    author = author or propagate.author
    floor = floor or propagate.floor_check
    judge = judge or propagate.judge
    apply = apply or propagate.apply_ops
    log = log or propagate.log_decisions
    out = {"claims": claims, "applied": [], "rejected": [], "cost_usd": 0.0, "personas": [], "skipped": None}
    pl = plan(claims, limit=config.AUDIENCE_MAX_PER_CARD_RUN)
    if pl.get("skipped"):
        out["skipped"] = pl["skipped"]
        print(f"[audience] {slug}: skipped ({pl['skipped']})", file=sys.stderr)
        return out
    if not pl["ops"]:
        return out
    out["personas"] = pl["personas"]
    facts = pl["facts"]
    authored = author(meta, pl["ops"], facts, claims,
                      addendum=judgment.optional("audience._AUTHOR_ADDENDUM"), role="audience_author")
    out["cost_usd"] += authored.get("cost_usd") or 0.0
    ops = authored["ops"]
    active_by_sk = propagate._active_targets(claims)
    surviving = {f.get("id") for f in facts}
    floor_results = [floor(op, surviving, active_by_sk) for op in ops]
    indexed = [(i, op) for i, (op, v) in enumerate(zip(ops, floor_results)) if not v]
    verdicts = {}
    if indexed:
        jr = judge(meta, facts, claims, indexed,
                   addendum=judgment.optional("audience._JUDGE_ADDENDUM"), role="audience_judge")
        out["cost_usd"] += jr.get("cost_usd") or 0.0
        verdicts = jr.get("verdicts") or {}
    confirmed = [ops[i] for i, _ in indexed
                 if (verdicts.get(i) or {}).get("verdict") == "confirm"
                 and not str((verdicts.get(i) or {}).get("judged_by") or "").startswith("fallback:")]
    out["rejected"] = [{"persona": ops[i].get("persona"), "operation": ops[i].get("operation"),
                        "reason": (verdicts.get(i) or {}).get("reason") or "; ".join(floor_results[i])}
                       for i in range(len(ops)) if ops[i] not in confirmed]
    try:
        facts_by_id = {f.get("id"): f for f in facts}
        records = propagate._decision_records(ops, floor_results, verdicts, facts_by_id, active_by_sk)
        if write:
            log(slug, records, source="audience", facts=facts)
    except Exception as e:                                 # the log is an audit trail, never a gate
        print(f"[audience] decision log failed ({type(e).__name__}: {e})", file=sys.stderr)
    if not confirmed:
        return out
    # a retire only lands together with its replacement: never leave a buyer without a lead
    adds = {op.get("persona") for op in confirmed if op.get("operation") == "add"}
    confirmed = [op for op in confirmed if op.get("operation") == "add" or op.get("persona") in adds]
    if write:
        ap = apply(claims, confirmed, facts, slug, today)
        out["claims"] = ap["claims"]
        out["applied"] = ap["applied"]
    else:
        out["applied"] = [{"operation": op.get("operation"), "subject_key": op.get("subject_key"), "dry_run": True}
                          for op in confirmed]
    return out
