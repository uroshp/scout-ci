"""Propagation (spec §17): a grounded, deal-grade fact reshapes the rep-facing prose.

This is the model-driven AUTHORSHIP judgment, the second of Scout's two shadow-qualified judgments
(the first is verification — what survives grounding). It runs AFTER triage -> materiality ->
grounding, on facts that have survived as TRUE and act-grade, and BEFORE apply/render.

Two passes, extending v1's generate-then-verify one layer up:
  - propose() (this step, Sonnet, TOOLS-OFF) drafts add/revise/retire operations on plays +
    objections, each anchored to the grounded fact via derived_from.
  - judge() (step 4, Opus) confirms or rejects each op; a deterministic FLOOR (step 4/5) enforces
    derived_from-resolves, retire-points-at-a-falsified-fact, no model-minted facts, blast-radius.

FACTS ONLY is the governing rule: propose reasons solely from the grounded facts it is handed. It
has no tools, so it structurally cannot search, fetch, or pull in anything ungrounded — it can only
draft what the given facts directly license. An ungrounded reaction is never bridged by speculation;
it is left for a later pass to ground (the living model).
"""
import asyncio
import json

from claude_agent_sdk import ClaudeAgentOptions

from scout import config
from scout.generate import _drive, _extract_json
from scout.prompts import WRITING_STYLE


_PROPOSE_SYSTEM = """You are the PROPOSE pass of a living competitive battlecard's PROPAGATION step.
You are handed one or more GROUNDED, deal-grade (act-severity) facts about the competitor or about
our own company (my_company), plus the card's CURRENT plays (battlecard) and objections
(objection_handling). Draft the rep-facing prose changes those facts LICENSE, and only those, as a
list of add / revise / retire operations.

THE RULE ABOVE ALL OTHERS, FACTS ONLY. Work strictly from the grounded fact(s) given. Reason only
about DIRECT, near-certain consequences of what the source already STATES. Never infer, speculate,
or invent an implication that is not in the fact. "They pulled the model" licenses "a customer
building on it must migrate"; it does NOT license "this probably signals financial trouble". If a
fact does not clearly license a rep-facing change, propose nothing for it. You have no search or
fetch tools on purpose: you cannot go find new facts, only work from these. A downstream judge
rejects any op resting on something the fact does not state, so do not reach.

NO CHANGE IS THE COMMON OUTCOME. Most deal-grade facts still move no specific play or objection. An
empty ops list is correct and expected. Do NOT manufacture an objection or play for a fact just
because it is notable. Quality over volume; the card stays lean.

VALENCE ROUTES THE OUTPUT:
- BACK FOOT (a competitor's strong move, OR our own stumble: a product pulled or restricted, an
  outage, a price hike, a security incident) -> an OBJECTION the buyer will now raise, in
  objection_handling, with the rep's answer.
- FRONT FOOT (a competitor stumble, OR our own win or ship) -> a PLAY, in battlecard / where_we_win.

OPERATIONS (pick the lightest that is true; identity is the SUBJECT, not the text):
- add — the fact creates a genuinely new play or objection not already tracked.
- revise — the fact NARROWS but does not kill a still-winning play (update its wording to the
  smaller gap, keep it), or updates an existing objection's rebuttal. REUSE the existing subject_key
  so it updates in place and the lineage is preserved.
- retire — the fact NEUTRALIZES a play to a wash, OR INVALIDATES a claim (makes it false). The claim
  leaves the active card for the lineage view. Use this, never a soften: an undercut play is a weak
  play. Set retired_reason to "neutralized: ..." or "invalidated: ...".

BLAST-RADIUS CAP. You may only touch a claim the fact DIRECTLY creates, undercuts, or invalidates.
Do not reword, improve, or re-order anything else. One fact rewrites only what it has high impact on.

Every op is an INTERPRETATION (claim_type: interpretation) carrying derived_from = the id of the
grounded fact it descends from. Propagation never mints a new "fact". Obey WRITING_STYLE for all
prose, it is rep-facing.

Return ONLY a single fenced ```json block:
{"ops": [
  {"operation": "add|revise|retire",
   "section": "objection_handling|battlecard",
   "zone": "where_we_win|contested|where_they_win|null (battlecard only; null for objection_handling)",
   "valence": "front_foot|back_foot",
   "target_subject_key": "<EXACT subject_key of the existing play/objection for revise|retire; null for add>",
   "subject_key": "<resulting claim subject_key: NEW (entity|attribute|qualifier) for add; SAME as target for revise|retire>",
   "claim": "<the rep-facing prose to show (play + soundbite, or objection + rebuttal); null for retire>",
   "claim_type": "interpretation",
   "persona": "<eng_led|technical_evaluator|economic_buyer|security_regulated|exec_top_down|null>",
   "derived_from": "<id of the grounded fact this descends from>",
   "retired_reason": "<retire only: 'neutralized: ...' | 'invalidated: ...'; else null>",
   "rationale": "<one line: the op and the rep decision it changes, following DIRECTLY from the grounded fact>"}
 ],
 "no_change": ["<one line per fact that licenses no rep-facing change, and why>"]}
If no fact licenses a change, return "ops": [] with your reasons in "no_change". That is the common,
correct outcome."""


def _facts_digest(facts: list[dict]) -> list[dict]:
    """The grounded facts propose may draw from. id is the derived_from anchor each op must carry."""
    return [{
        "id": f.get("id"),
        "subject_key": f.get("subject_key"),
        "claim": f.get("claim"),
        "about": f.get("about"),
        "valence": f.get("valence"),
        "source_url": f.get("source_url"),
        "evidence_excerpt": f.get("evidence_excerpt"),
        "as_of": f.get("as_of"),
    } for f in facts]


def _targets_digest(claims: list[dict]) -> list[dict]:
    """The ACTIVE plays + objections propose may revise or retire (reuse the EXACT subject_key)."""
    out = []
    for c in claims:
        if c.get("section") not in ("battlecard", "objection_handling"):
            continue
        if str(c.get("status", "active")) != "active":
            continue
        out.append({"subject_key": c.get("subject_key"), "section": c.get("section"),
                    "zone": c.get("zone"), "claim": str(c.get("claim", ""))[:240]})
    return out


async def _run_propose(meta: dict, facts: list[dict], claims: list[dict]) -> dict:
    comp, me = meta.get("competitor"), meta.get("my_company")
    user = (f"Competitor: {comp}" + (f"   We are: {me}" if me else "") + "\n\n"
            "GROUNDED ACT-GRADE FACTS (draft only what these license; derived_from = each fact's id):\n"
            + json.dumps(_facts_digest(facts), ensure_ascii=False, indent=2)
            + "\n\nCURRENT PLAYS + OBJECTIONS (what you may revise or retire; reuse the EXACT subject_key):\n"
            + json.dumps(_targets_digest(claims), ensure_ascii=False, indent=2))
    options = ClaudeAgentOptions(
        model=config.SUBAGENT_MODEL,                      # propose on Sonnet (spec §17)
        system_prompt={"type": "preset", "preset": "claude_code",
                       "append": _PROPOSE_SYSTEM + "\n\n" + WRITING_STYLE},
        mcp_servers={},
        allowed_tools=[],                                 # TOOLS-OFF: reason only from the given facts
        disallowed_tools=["WebSearch", "WebFetch"],
        permission_mode="bypassPermissions",
        max_turns=config.PROPOSE_MAX_TURNS,
        max_budget_usd=config.PROPOSE_MAX_BUDGET_USD,
    )
    return await _drive(user, options, "propose")


def propose(meta: dict, facts: list[dict], claims: list[dict]) -> dict:
    """Run the propose pass over grounded act-grade facts. Returns
    {'ops': [...], 'no_change': [...], 'cost_usd': float}. Light structural guard only here; the
    deterministic FLOOR + Opus judge land in step 4 (nothing applies to a card until then)."""
    res = asyncio.run(_run_propose(meta, facts, claims))
    try:
        data = _extract_json(res["text"])
    except Exception:
        data = {"ops": [], "no_change": []}
    ops = [o for o in (data.get("ops") or [])
           if isinstance(o, dict) and o.get("operation") in ("add", "revise", "retire")]
    return {"ops": ops, "no_change": data.get("no_change") or [], "cost_usd": res.get("cost_usd")}
