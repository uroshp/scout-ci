"""Surface router (spec §17, the routing brain).

On a grounded, act-grade change, decide EVERY brief surface the change bears on and how — across
ALL rendered sections, not just plays + objections. This REPLACES two narrower things that used to
run disconnected: propose's implicit (battlecard + objection_handling only) routing, and the
separate strategic-lead pass (the lead is just the `executive_summary` surface here). One Opus
judgment, SEEDED with the materiality verdict — the alert's `so_what` already states what decision a
change moves, so the routing intelligence the pipeline already produced is USED, not rediscovered
blind (the 7/1 miss: materiality wrote "the export-ban objection is now dead" and nothing consumed it).

The router ROUTES; it does not write prose. Per affected claim it emits
{section, zone, operation, change_kind, valence, target_subject_key, feed_note}. scout.propagate
then AUTHORS the prose for each routed op and gates it through the SAME deterministic floor +
adversarial Opus judge as before. Nothing here — or anywhere downstream of review — mutates a card
without human approval. Model-pass count is unchanged: route + author + judge replaces
propose + judge + strategic_lead (the strategy pass is absorbed, not added).

change_kind is the resilience contract: an exhaustive taxonomy so any change — a brand-new
development, a fact folded into an existing claim, a partial or full invalidation, a play
neutralized to a wash, the next beat of a fast-moving story, or a lead superseded — maps to exactly
one op, and a genuinely-new scenario extends this one enum rather than a scatter of special cases.
"""
import asyncio
import json

from claude_agent_sdk import ClaudeAgentOptions

from scout import config
from scout.generate import _drive, _extract_json
from scout.prompts import WRITING_STYLE
from scout.schema import ZONES

# The exhaustive change-kind taxonomy (docs: the router plan). Every routed op is one of these.
CHANGE_KINDS = [
    "new",                    # brand-new development -> add a claim in the routed section
    "update",                 # existing claim gains new facts -> revise in place, keep still-true points
    "partial_invalidation",   # part of a claim is now false -> revise, narrow to what still holds
    "full_invalidation",      # a claim is now false -> retire (kept for lineage), feed_note REQUIRED
    "neutralize",             # a winning play is neutralized to a wash -> retire
    "reconcile_beat",         # the next beat of a story the claim already encodes -> revise, fold in, keep prior beats
    "supersede_lead",         # LEGACY (retired 2026-08-12): NO LONGER in the router prompt — the lead
                              # election owns which verdict opens the brief. Kept in the taxonomy only so
                              # a replayed historical decision log still validates (never freshly emitted).
    "supersede_retire",       # SYNTHESIZED BY CODE (2026-07-25 sweep), never routed by the model: an
                              # active claim still cites an identifier a new fact supersedes -> retire,
                              # judged per-claim with the deal-moving lens
]

# Sections the router may route a change INTO. recent_moves + the raw fact record are maintained
# UPSTREAM by materiality's fact-patch, so the router never re-posts the fact there; it reshapes the
# INTERPRETIVE surfaces a change bears on. All eight rendered sections minus recent_moves.
ROUTABLE_SECTIONS = [
    "executive_summary", "snapshot", "positioning", "pricing",
    "battlecard", "sentiment", "objection_handling",
]

_ROUTE_SYSTEM = """You are the SURFACE ROUTER of a living competitive battlecard. You are handed one
or more GROUNDED, act-grade facts (already verified TRUE) about the competitor or about our own
company, each with the MATERIALITY VERDICT that escalated it (its `so_what`: the decision it changes),
plus the card's CURRENT claims across every section. Decide EVERY surface each fact reshapes, and how.

You ROUTE; you do NOT write the rep-facing prose. A later author pass writes each claim; an adversarial
judge then confirms or rejects it. Your job is to name, per affected claim: the section, the operation,
the change_kind, and (for a removal) the one-line note the change feed will show.

USE THE VERDICT, DON'T REDISCOVER IT. Each fact's `so_what` already says what it bears on ("the
export-ban objection is now dead"). Treat it as a strong prior: act on it. But you see the FULL card,
so you may find MORE affected surfaces than the verdict named, or conclude a named one does not
actually change. The verdict is the floor of your coverage, not the ceiling.

COVER EVERY SECTION IT TOUCHES — this is the whole point. A single fact can move the lead AND an
objection AND a positioning line AND pricing at once. Check each of these and route to ALL that apply:
- executive_summary — the brief's strategic verdicts. Route a change here (as a normal new/update/
  reconcile_beat/partial_invalidation) when it adds or changes one of these top-line verdicts. Do NOT
  try to decide WHICH verdict opens the brief ("Today's angle") — a downstream lead election owns that
  ordering, on deal impact. Your job is the verdict's CONTENT, not its rank. Most changes touch no
  executive_summary verdict at all.
- snapshot — the at-a-glance framing lines.
- positioning — how the two companies are positioned / differentiated.
- pricing — pricing and packaging. A price move, a new tier, a discount, a pricing-model change routes here.
- battlecard — the plays (zone: where_we_win | contested | where_they_win). zone REQUIRED here, null elsewhere.
- sentiment — market / customer / analyst sentiment.
- objection_handling — the objections a buyer raises and their rebuttals (zone null).
Do NOT route to recent_moves: the raw fact is already recorded there upstream. You reshape the
INTERPRETIVE surfaces above.

VALENCE ROUTES OP TYPE:
- back_foot (the competitor makes a strong move, OR our own stumble: a product pulled/restricted, an
  outage, a price hike, an incident) -> the buyer now raises an OBJECTION (objection_handling), or a
  play is NARROWED/NEUTRALIZED, or a positioning/pricing line worsens.
- front_foot (the competitor stumbles, OR our own win/ship) -> a PLAY (battlecard/where_we_win), or a
  positioning/pricing line improves, or an existing objection is WEAKENED or fully invalidated.
- neutral — a fact that keeps a section accurate without a clear competitive valence (some snapshot /
  positioning / sentiment updates).

change_kind — classify EACH routed op as exactly one (this is the resilience contract):
- new                  : a genuinely new play/objection/line the fact creates. operation=add, target null.
- update               : the fact adds detail to an existing claim. operation=revise, keep still-true content.
- partial_invalidation : part of the claim is now false. operation=revise, narrow to what still holds.
- full_invalidation    : the claim is now false / its premise is gone. operation=retire. feed_note REQUIRED.
- neutralize           : a winning play is neutralized to a wash. operation=retire. feed_note REQUIRED.
- reconcile_beat       : the next beat of a story the claim ALREADY encodes (a ban, then a reversal).
                         operation=revise, fold the new beat in and KEEP prior still-true beats.
An executive_summary verdict is routed with these SAME change_kinds (new/update/reconcile_beat/
partial_invalidation) like any other section — there is no special "make this the lead" kind; the
lead election ranks the verdicts downstream.

OPERATIONS: add (new, target_subject_key null, mint a fresh subject_key), revise (touch an existing
claim IN PLACE, reuse its EXACT subject_key), retire (a claim leaves the active card for the lineage
view, reuse its EXACT subject_key). Pick the LIGHTEST operation that is true: prefer revise over
retire when a play still wins narrowed; prefer revise over add when the subject already exists.

IDENTIFY TARGETS BY THE GIVEN subject_key. For revise/retire, copy the EXACT subject_key of the claim
from the list you are given. Never invent a target that is not on the card.

FACTS-ONLY, LITERAL SCOPE. Route only what the fact DIRECTLY licenses at exactly its stated scope.
"Restricted to foreign nationals" does not license retiring a whole play. If a fact licenses no
rep-facing reshaping, that is the COMMON, correct outcome: list it under no_surface with the reason.
Do not manufacture a surface to look responsive. The card stays lean.

feed_note: a plain, one-line "what changed" note the LEFT updates panel will show the user. REQUIRED
for every retire (full_invalidation / neutralize) so a removal is never silent, e.g. "Removed the
export-ban objection: Commerce fully lifted the Fable 5 and Mythos 5 controls on June 30." Optional
but welcome for adds/revises. Obey the writing style for every note.

superseded_terms (optional, per op): when the fact establishes that a NAMED IDENTIFIER — a product,
model, version, or title-holder — is REPLACED by a newer one (a new flagship ships, a product is
renamed, a price list supersedes the old one), list the now-superseded identifier strings on that op,
e.g. ["Opus 4.8", "Opus 4.7"]. RULES: list ONLY identifiers that literally appear in the grounded
fact's text or in the claim being revised; sibling/older versions may be listed ONLY if literally
present there too; never infer or guess identifiers. Code will sweep the card for OTHER active claims
still citing these terms and propose their retirement to a judge — so list terms only when the
replacement genuinely makes claims about the old identifier stop mattering in a deal. Omit the field
(or []) otherwise. Typical on: update, reconcile_beat, partial/full_invalidation.

RUN VERDICT — separately, judge whether this run's change(s) are CONSEQUENTIAL: do they change what the
rep DOES or the brief's thesis (consequential), or are they a routine accuracy update that keeps the
card current without changing the argument (routine)? An OpenAI IPO date, a daily box-office number, or
"the vendor's own employees can now access the model" are routine. A US government restriction on
frontier models, or a thaw that RESTORES customer access, is consequential. When unsure, mark it
consequential. This is a shadow-eval signal; it does not gate anything.

Return ONLY a single fenced ```json block:
{"surface_ops": [
  {"derived_from": "<id of the grounded fact this op descends from>",
   "section": "executive_summary|snapshot|positioning|pricing|battlecard|sentiment|objection_handling",
   "zone": "where_we_win|contested|where_they_win|null (battlecard only; null elsewhere)",
   "operation": "add|revise|retire",
   "change_kind": "new|update|partial_invalidation|full_invalidation|neutralize|reconcile_beat",
   "valence": "front_foot|back_foot|neutral",
   "target_subject_key": "<EXACT subject_key of the claim for revise|retire; null for add>",
   "subject_key": "<resulting subject_key: NEW for add; SAME as target for revise|retire>",
   "persona": "<eng_led|technical_evaluator|economic_buyer|security_regulated|exec_top_down|null>",
   "feed_note": "<one-line change-feed note; REQUIRED for retire>",
   "superseded_terms": ["<optional: identifier strings this fact supersedes, per the rules above>"],
   "why": "<one line: why this surface is affected, following DIRECTLY from the grounded fact>"}
 ],
 "no_surface": [ {"derived_from": "<fact id>", "why": "<why this fact moves no rep-facing prose>"} ],
 "run_verdict": {"consequential": <true if this run's change(s) change the rep's play or the thesis; false if routine>,
                 "consequence_rationale": "<one line: why it does or does not change the play>",
                 "headline": "<one line naming the single most consequential change this run, or null>"}}
If nothing is reshaped, return "surface_ops": [] with every fact explained in "no_surface"."""


def _facts_digest(facts_with_alerts: list[dict]) -> list[dict]:
    """The grounded facts + their materiality verdicts, as the router sees them. `so_what` is the
    routing seed. `standing_strength` facts (pivot fuel) carry no alert — they are admissible evidence
    for a rebuttal's pivot but never a routing trigger."""
    out = []
    for fa in facts_with_alerts:
        f = fa.get("fact") or {}
        a = fa.get("alert") or {}
        out.append({
            "id": f.get("id"),
            "subject_key": f.get("subject_key"),
            "claim": f.get("claim"),
            "about": f.get("about"),
            "valence": f.get("valence"),
            "as_of": f.get("as_of"),
            "standing_strength": bool(f.get("standing_strength")),
            "materiality_verdict": {
                "headline": a.get("headline"),
                "so_what": a.get("so_what"),
                "old_value": a.get("old_value"),
                "new_value": a.get("new_value"),
            } if a else None,
        })
    return out


def _card_digest(claims: list[dict]) -> list[dict]:
    """Every ACTIVE claim across ALL routable sections, full text (never truncated), so the router can
    judge invalidation / reconcile against what the claim actually encodes. This is the coverage fix:
    the router sees positioning / pricing / snapshot / sentiment / lead, not just plays + objections."""
    out = []
    for c in claims:
        if c.get("section") not in ROUTABLE_SECTIONS:
            continue
        if str(c.get("status", "active")) != "active":
            continue
        out.append({
            "subject_key": c.get("subject_key"),
            "section": c.get("section"),
            "zone": c.get("zone"),
            "claim": str(c.get("claim", "")),
        })
    return out


async def _run_route(meta: dict, facts_with_alerts: list[dict], claims: list[dict]) -> dict:
    comp, me = meta.get("competitor"), meta.get("my_company")
    focus = meta.get("focus") or meta.get("focus_area")
    user = (f"Competitor: {comp}" + (f"   We are: {me}" if me else "")
            + (f"   Focus: {focus}" if focus else "") + "\n\n"
            "GROUNDED ACT-GRADE FACTS + their materiality verdicts (route what these license):\n"
            + json.dumps(_facts_digest(facts_with_alerts), ensure_ascii=False, indent=2)
            + "\n\nCURRENT CARD CLAIMS across every section (target revise/retire by EXACT subject_key):\n"
            + json.dumps(_card_digest(claims), ensure_ascii=False, indent=2))
    options = ClaudeAgentOptions(
        model=config.ORCHESTRATOR_MODEL,                  # routing is judgment -> Opus (absorbs strategic_lead)
        # Plain-string system: tools are OFF, so the claude_code preset was pure input overhead
        # (2026-07-02 cost pass — same change on author/judge/rewrite).
        system_prompt=_ROUTE_SYSTEM + "\n\n" + WRITING_STYLE,
        mcp_servers={},
        allowed_tools=[],                                 # TOOLS-OFF: route only from the given facts + card
        disallowed_tools=["WebSearch", "WebFetch"],
        permission_mode="bypassPermissions",
        max_turns=config.ROUTE_MAX_TURNS,
        max_budget_usd=config.ROUTE_MAX_BUDGET_USD,
    )
    return await _drive(user, options, "route")


def _clean_ops(raw_ops) -> list[dict]:
    """Keep only well-shaped routing ops. Structural guard only — the deterministic floor + the judge
    are the real gates downstream. Drops anything without a valid operation or a routable section."""
    ops = []
    for o in raw_ops or []:
        if not isinstance(o, dict):
            continue
        if o.get("operation") not in ("add", "revise", "retire"):
            continue
        if o.get("section") not in ROUTABLE_SECTIONS:
            continue
        if o.get("change_kind") not in CHANGE_KINDS:
            continue
        # normalize zone: only battlecard carries one
        if o.get("section") != "battlecard":
            o["zone"] = None
        elif o.get("zone") not in ZONES:
            continue
        # superseded_terms (2026-07-25 supersede-retire): sanitize to a list of non-empty strings;
        # grounding is verified downstream (propagate.verified_superseded_terms), not here.
        terms = o.get("superseded_terms")
        o["superseded_terms"] = [t.strip() for t in terms
                                 if isinstance(t, str) and t.strip()] if isinstance(terms, list) else []
        ops.append(o)
    return ops


def route(meta: dict, facts_with_alerts: list[dict], claims: list[dict]) -> dict:
    """Run the surface router over grounded act-grade facts (each paired with its materiality alert).
    Returns {'surface_ops': [...], 'no_surface': [...], 'cost_usd': float}. Routes only; authoring +
    the floor + the judge are downstream. Never raises on a parse miss (returns empty routing)."""
    res = asyncio.run(_run_route(meta, facts_with_alerts, claims))
    try:
        data = _extract_json(res["text"])
    except Exception:
        data = {"surface_ops": [], "no_surface": [], "run_verdict": {}}
    rv = data.get("run_verdict")
    return {
        "surface_ops": _clean_ops(data.get("surface_ops")),
        "no_surface": data.get("no_surface") or [],
        "run_verdict": rv if isinstance(rv, dict) else {},   # shadow-eval consequentiality signal
        "cost_usd": res.get("cost_usd"),
        "raw": res.get("text", ""),
    }
