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
from scout import judgment

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

_ROUTE_SYSTEM = judgment.get("route._ROUTE_SYSTEM")


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
        disallowed_tools=["WebSearch", *config.MODEL_DISALLOWED_TOOLS],
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
