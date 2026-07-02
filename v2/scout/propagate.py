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
import re
import sys
from datetime import datetime

from claude_agent_sdk import ClaudeAgentOptions

from scout import config, selfserve
from scout.generate import _drive, _extract_json
from scout.prompts import WRITING_STYLE
from scout.route import route, ROUTABLE_SECTIONS, CHANGE_KINDS
from scout.schema import ZONES, claim_id, normalize_subject_key, validation_errors

# change_kind -> the ONLY operation that kind may carry (the resilience contract, enforced by the
# floor). A router op is rejected if its change_kind and operation disagree; kinds absent from this
# map (or a legacy op with no change_kind) skip the check, so direct apply/test paths are unaffected.
_KIND_OP = {
    "new": "add", "update": "revise", "partial_invalidation": "revise",
    "reconcile_beat": "revise", "supersede_lead": "revise",
    "full_invalidation": "retire", "neutralize": "retire",
}


_AUTHOR_SYSTEM = """You are the AUTHOR pass of a living competitive battlecard's PROPAGATION step. The
ROUTING is already decided by an upstream router: you are handed a WORKLIST of routed ops, each naming
its section, its operation (add|revise), its change_kind, the target claim's CURRENT text (for a
revise), and the grounded fact it derives from, plus the pool of grounded facts. WRITE the rep-facing
prose for each op, and ONLY that. Do NOT re-route, do NOT change the section/operation/target/valence,
do NOT invent new ops, and do NOT author retires (a retire removes a claim and needs no prose). The
sections below tell you HOW each surface is shaped; the worklist tells you WHICH to write.

THE RULE ABOVE ALL OTHERS, FACTS ONLY. Work strictly from the grounded fact(s) given. Reason only
about DIRECT, near-certain consequences of what the source already STATES. Never infer, speculate,
or invent an implication that is not in the fact. "They pulled the model" licenses "a customer
building on it must migrate"; it does NOT license "this probably signals financial trouble". If a
fact does not clearly license a rep-facing change, propose nothing for it. You have no search or
fetch tools on purpose: you cannot go find new facts, only work from these. A downstream judge
rejects any op resting on something the fact does not state, so do not reach.

NEVER INVENT THE MECHANISM OR REASON. State a grounded CONSEQUENCE in the source's own terms; do NOT
supply WHY or HOW it happened if the source does not. If the fact is "the order's net effect is that
the model is disabled for all customers", write exactly that — do NOT add "because they cannot filter
users by nationality" or any operational reason the source omits, EVEN IF it is plausibly true. A
grounded conclusion does not license an ungrounded explanation of it. When the source gives you its
own framing for a consequence ("the net effect of this order is..."), use that framing, not your own.

NO CHANGE IS THE COMMON OUTCOME. Most deal-grade facts still move no specific play or objection. An
empty ops list is correct and expected. Do NOT manufacture an objection or play for a fact just
because it is notable. Quality over volume; the card stays lean.

VALENCE ROUTES THE OUTPUT:
- BACK FOOT (a competitor's strong move, OR our own stumble: a product pulled or restricted, an
  outage, a price hike, a security incident) -> an OBJECTION the buyer will now raise, in
  objection_handling. An objection is NOT complete until its rebuttal PIVOTS to a genuine, currently-
  true strength and hands the rep a CONCRETE alternative or next move — never merely "confirm",
  "check", or "verify eligibility". State the constraint honestly, then redirect to a real capability
  the buyer can act on TODAY (e.g. a generally-available default to standardize on while the issue is
  worked). The strength you pivot to MUST itself be grounded — drawn from the given facts, INCLUDING the
  standing-strength my_company facts provided for exactly this (see below); never invented — and do NOT
  speculate about if/when the constraint resolves unless a fact states it. A rebuttal that only restates
  the problem is a FAIL; rep-facing prose must leave the rep with a move.
  THE MOVE MUST BE THE REP'S, AND IT MUST KEEP THE REP IN CONTROL. The concrete next step is something
  the REP does or offers TODAY (e.g. "standardize on the GA model now"), never something the BUYER is
  coached to go extract from us. A rebuttal FAILS if it: (a) tells the buyer to demand a concession,
  guarantee, escalation, or written commitment ("get your account team to confirm a restoration
  timeline in writing", "ask them for a date"); (b) commits the rep or our company to a future action
  outside the rep's authority (restoration dates, written guarantees, anything we cannot promise on the
  call); or (c) concedes the buyer's switching/migration framing as warranted ("before committing to a
  migration plan"). Each of these hands the rep a losing script: it puts our own side on defense or
  validates the fear the objection exists to defuse. Pivot to what is true and ours to offer right now,
  delivered with a straight back.
- FRONT FOOT (a competitor stumble, OR our own win or ship) -> a PLAY, in battlecard / where_we_win.

STANDING-STRENGTH FACTS (pivot fuel). Some given facts are marked "standing_strength": true. These are
grounded, currently-true my_company strengths (e.g. multi-cloud availability / SLAs, security posture, a
GA model to standardize on) supplied so a BACK-FOOT rebuttal has a grounded strength to pivot to. Use
them ONLY to ground a pivot. They are NOT new developments: NEVER author an add/revise/retire triggered
by a standing-strength fact, and NEVER set derived_from to one (derived_from is always the STUMBLE that
raises the objection — the outage, the restriction). A rebuttal's pivot MAY cite a standing-strength
fact even though it differs from the op's derived_from trigger: that sibling pivot is fully grounded, not
an invented capability. If no given fact (trigger or standing-strength) grounds a real pivot, propose no
objection — leave the development as a tracked fact.

REQUIRED PROSE FORMAT (the viewer renders these markers into the structured card; OMIT them and the
claim renders as one unbroken blob and is REJECTED — this is not optional):
- An OBJECTION (objection_handling) claim is written as: a bold question line (**"..."**), then the
  rebuttal body, then a final block that begins literally with **So what:** stating the rep's concrete
  move in one or two sentences. The move you were told to hand the rep above GOES in the So-what block.
- A PLAY (battlecard win/lose zone) claim is written as: a bold one-line headline, then the body, then a
  final block that begins literally with **Soundbite:** giving one rep-ready sentence. A CONTESTED
  battlecard entry is a neutral framing: no Soundbite, no persona.
- THE LEAD (executive_summary, change_kind supersede_lead) is written as: a bold one-line headline, ONE
  short proof sentence, then a **Soundbite:** "one line to say out loud", then a final **So what:** block
  with the rep's concrete move. Keep it short and scannable — a rep skims it in ten seconds.
- POSITIONING, PRICING, SNAPSHOT, SENTIMENT claims are tight plain prose (one or two sentences), NO
  required block and NO persona. State what is TRUE NOW; for a pricing op, name the exact number or tier
  the fact states. Do not force a So-what or Soundbite where the section does not use one.
Every objection, win/lose play, and lead you emit must end with its **So what:** or **Soundbite:** block —
when you revise in place, keep that block. A claim without it will be rejected and re-asked. Every
objection and win/lose play must ALSO carry a `persona` — the single best-fit buyer (an enum value,
NEVER null) who raises the objection or that the play targets; it renders the per-claim buyer badge.

SUPERSESSION — CONDENSE RESOLVED HISTORY (the reader is a rep with 30 seconds): when the new beat
RESOLVES earlier beats a claim carries (a saga that ended, an interim ruling now superseded), lead
with the CURRENT state and compress the resolved history to AT MOST one sentence of arc. Preserving
still-true content means preserving the facts that still matter — NOT retaining every prior beat
verbatim; a reconcile that just appends is as wrong as one that erases. Keep an objection or play
body under ~120 words; anything over the render cap (~170) is rejected outright.

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

RECONCILE FAST-MOVING FOLLOW-UPS — DO NOT REWRITE FROM SCRATCH. Markets move in beats: a story already
on the card gets a follow-up (a ban, then a directive, then a reversal; a competitor's metric, then our
counter). When the new fact is the latest beat of a development an existing claim ALREADY reflects (you
are given each target claim's FULL current text), REVISE that claim SURGICALLY: fold in the new beat and
KEEP every prior point that is still true, plus the existing rep move and its required **So what:** /
**Soundbite:** block. A later beat does not erase the earlier ones: a reversal does not unmake the prior
event (an administration softening on a vendor does NOT delete the earlier ban — state both, reconciled).
Do NOT rewrite the claim from scratch, do NOT drop still-true prior content, do NOT lose the required
block. Replacing the whole claim and shedding still-valid content is the wholesale-rewrite error the
judge rejects on blast-radius grounds — the fast-moving story is exactly where the card must stay both
FRESH and COMPLETE.

Every op is an INTERPRETATION (claim_type: interpretation) carrying derived_from = the id of the
grounded fact it descends from. Propagation never mints a new "fact". Obey WRITING_STYLE for all
prose, it is rep-facing.

FINAL STEP, A REQUIRED SECOND PASS ON YOUR OWN OUTPUT (do this every time, before you emit anything):
re-read every `claim` string you wrote, character by character, and confirm each one contains NO em
dash or en dash used as punctuation (— –) and no other WRITING_STYLE violation. If you find even one,
rewrite that string to remove it with clean punctuation (period, comma, colon, or parentheses) BEFORE
returning. Do not emit the JSON until every claim string passes this check. A single em dash is a
failed output.

Return ONLY a single fenced ```json block, ONE entry per add/revise op in the worklist (omit retires):
{"authored": [
  {"op_index": <int — the op's index in the worklist you were given>,
   "claim": "<the rep-facing prose for that op, in its section's required format>",
   "persona": "<eng_led|technical_evaluator|economic_buyer|security_regulated|exec_top_down|null — required for an objection or a win/lose play, null elsewhere>"}
]}
Write exactly one entry per add/revise op, keyed by its op_index. Author nothing for a retire."""


def _facts_digest(facts: list[dict]) -> list[dict]:
    """The grounded facts propose may draw from. id is the derived_from anchor each op must carry.
    `standing_strength` flags a my_company STANDING strength supplied as pivot fuel: admissible
    evidence a back-foot rebuttal may pivot to, but NEVER a trigger for an op (the floor enforces this
    — see _trigger_fact_ids)."""
    return [{
        "id": f.get("id"),
        "subject_key": f.get("subject_key"),
        "claim": f.get("claim"),
        "about": f.get("about"),
        "valence": f.get("valence"),
        "source_url": f.get("source_url"),
        "evidence_excerpt": f.get("evidence_excerpt"),
        "as_of": f.get("as_of"),
        "standing_strength": bool(f.get("standing_strength")),
    } for f in facts]


def _trigger_fact_ids(facts: list[dict]) -> set:
    """The facts an op may DERIVE FROM (its trigger). Standing-strength my_company facts are excluded:
    they are pivot evidence only, never the development that licenses a new op, so the floor rejects
    any op whose derived_from is one of them (an op must be triggered by a real change)."""
    return {f.get("id") for f in facts if f.get("id") and not f.get("standing_strength")}


def _targets_digest(claims: list[dict]) -> list[dict]:
    """The ACTIVE plays + objections propose may revise or retire (reuse the EXACT subject_key). The
    claim text is sent IN FULL (never truncated): to reconcile a fast-moving follow-up into a layered
    claim, propose AND judge must see every prior beat the claim already encodes — a clipped view is
    what made the proposer rewrite from scratch and erase still-true content."""
    out = []
    for c in claims:
        if c.get("section") not in ROUTABLE_SECTIONS:
            continue
        if str(c.get("status", "active")) != "active":
            continue
        out.append({"subject_key": c.get("subject_key"), "section": c.get("section"),
                    "zone": c.get("zone"), "claim": str(c.get("claim", ""))})
    return out


def _author_worklist(surface_ops: list[dict], active_by_sk: dict) -> list[dict]:
    """The add/revise routed ops the author must write prose for, each tagged with its worklist index
    and the target claim's CURRENT full text (so a reconcile/partial-invalidation revise folds the new
    beat in without erasing still-true prior content). Retires are excluded — they need no prose."""
    work = []
    for i, op in enumerate(surface_ops):
        if op.get("operation") not in ("add", "revise"):
            continue
        tgt = op.get("target_subject_key")
        current = active_by_sk.get(normalize_subject_key(str(tgt))) if tgt else None
        work.append({
            "op_index": i,
            "section": op.get("section"),
            "zone": op.get("zone"),
            "operation": op.get("operation"),
            "change_kind": op.get("change_kind"),
            "valence": op.get("valence"),
            "target_subject_key": tgt,
            "current_text": (current.get("claim") if isinstance(current, dict) else None),
            "derived_from": op.get("derived_from"),
            "persona_hint": op.get("persona"),
            "why": op.get("why"),
        })
    return work


def _finalize_op(op: dict, authored: dict | None) -> dict:
    """Merge the router's routing decision (authoritative) with the author's prose into the op shape
    the floor/judge/apply consume. Retire ops carry claim=None and a floor-shaped retired_reason
    derived from their change_kind; add/revise ops take the authored prose + persona."""
    operation = op.get("operation")
    out = {
        "operation": operation,
        "section": op.get("section"),
        "zone": op.get("zone"),
        "valence": op.get("valence"),
        "change_kind": op.get("change_kind"),
        "target_subject_key": op.get("target_subject_key"),
        "subject_key": op.get("subject_key"),
        "claim_type": "interpretation",
        "derived_from": op.get("derived_from"),
        "feed_note": op.get("feed_note"),
        "persona": op.get("persona"),
    }
    if operation == "retire":
        out["claim"] = None
        note = (op.get("feed_note") or op.get("why") or "the anchoring fact changed").strip()
        prefix = "neutralized" if op.get("change_kind") == "neutralize" else "invalidated"
        out["retired_reason"] = op.get("retired_reason") or f"{prefix}: {note}"
    else:
        a = authored or {}
        out["claim"] = a.get("claim")
        if a.get("persona"):
            out["persona"] = a["persona"]
    return out


async def _run_author(meta: dict, surface_ops: list[dict], facts: list[dict], active_by_sk: dict) -> dict:
    comp, me = meta.get("competitor"), meta.get("my_company")
    user = (f"Competitor: {comp}" + (f"   We are: {me}" if me else "") + "\n\n"
            "GROUNDED FACTS (the ONLY admissible evidence; each op's derived_from points into these):\n"
            + json.dumps(_facts_digest(facts), ensure_ascii=False, indent=2)
            + "\n\nWORKLIST — write the prose for each op; key your output by op_index:\n"
            + json.dumps(_author_worklist(surface_ops, active_by_sk), ensure_ascii=False, indent=2))
    options = ClaudeAgentOptions(
        model=config.SUBAGENT_MODEL,                      # author prose on Sonnet (routing was Opus)
        system_prompt={"type": "preset", "preset": "claude_code",
                       "append": _AUTHOR_SYSTEM + "\n\n" + WRITING_STYLE},
        mcp_servers={},
        allowed_tools=[],                                 # TOOLS-OFF: write only from the given facts
        disallowed_tools=["WebSearch", "WebFetch"],
        permission_mode="bypassPermissions",
        max_turns=config.PROPOSE_MAX_TURNS,
        max_budget_usd=config.PROPOSE_MAX_BUDGET_USD,
    )
    return await _drive(user, options, "author")


def author(meta: dict, surface_ops: list[dict], facts: list[dict], claims: list[dict]) -> dict:
    """Author the rep-facing prose for the router's add/revise ops. Returns {'ops': [...], 'cost_usd'}
    where ops are the ROUTER ops (routing authoritative) with prose merged in; retires pass through
    with claim=None. The deterministic FLOOR + adversarial Opus judge gate them next (nothing applies
    to a card until then)."""
    add_revise = [op for op in surface_ops if op.get("operation") in ("add", "revise")]
    if not add_revise:                                    # only retires -> no authoring call needed
        return {"ops": [_finalize_op(op, None) for op in surface_ops], "cost_usd": None}
    active_by_sk = _active_targets(claims)
    res = asyncio.run(_run_author(meta, surface_ops, facts, active_by_sk))
    try:
        authored = {a.get("op_index"): a for a in (_extract_json(res["text"]).get("authored") or [])
                    if isinstance(a, dict) and isinstance(a.get("op_index"), int)}
    except Exception:
        authored = {}
    return {"ops": [_finalize_op(op, authored.get(i)) for i, op in enumerate(surface_ops)],
            "cost_usd": res.get("cost_usd")}


# === Step 4: the deterministic FLOOR + the adversarial JUDGE + the decision log ================
#
# Propagation is the FIRST user-facing content gated by a model with no model-free authority behind
# it: an interpretation can't be re-fetched and string-matched the way a fact is grounded. To keep
# it inside Scout's load-bearing thesis (NO ungrounded model judgment in the seat of final
# authority), every authored op passes TWO gates before it could ever reach a card:
#   1. floor_check() — a MODEL-FREE grader. Mechanical, runs first, rejects before any Opus spend.
#      It is the model-free authority: structure the model cannot talk its way past.
#   2. judge() — an independent adversarial Opus pass (the §17 "judge", extending v1 generate->verify
#      one layer up to AUTHORSHIP). It may reject everything.
# Every proposed op — floor-rejected, judge-rejected, or confirmed — is written to a decision log:
# one artifact serving as both the prose-edit AUDIT TRAIL and the judge's TRAINING CORPUS for the
# authorship shadow trial (spec §17). Nothing here APPLIES an op to a card; apply + the retire-
# cascade are step 5, and this ships in shadow first (capture, don't yet mutate).


_FACT_ID_RE = re.compile(r"^c_[0-9a-f]{12}$")


def _active_targets(claims: list[dict]) -> dict:
    """Map normalized subject_key -> the ACTIVE battlecard/objection claim it names. The floor
    resolves every revise/retire target against this: you cannot revise or retire a claim that is
    not actually on the live card (blast radius must be REAL, not invented)."""
    out = {}
    for c in claims:
        if c.get("section") not in ROUTABLE_SECTIONS:
            continue
        if str(c.get("status", "active")) != "active":
            continue
        sk = c.get("subject_key")
        if sk:
            out[normalize_subject_key(str(sk))] = c
    return out


def floor_check(op: dict, surviving_fact_ids: set, active_by_sk: dict) -> list:
    """The deterministic FLOOR (spec §17 thesis-governance (b)). Model-free, runs BEFORE the judge.
    Returns a list of violation strings ([] = passes). It enforces, mechanically, exactly the
    invariants a model must not be trusted to self-police:
      - NO model-minted facts: every propagated claim is claim_type == 'interpretation'.
      - PROVENANCE: derived_from resolves to a surviving grounded fact (the interpretation's anchor).
      - REAL blast radius: revise/retire point at a claim that is actually active on the card; add
        does not silently overwrite an existing active claim.
      - WELL-FORMED: section/zone shape, and the retire contract (claim cleared, reason tagged).
    It deliberately does NOT judge whether the implication is WARRANTED — that semantic call is the
    Opus judge's. Floor = structure; judge = warrant."""
    operation = op.get("operation")
    if operation not in ("add", "revise", "retire"):
        return [f"unknown operation {operation!r}"]
    v = []

    section = op.get("section")
    if section not in ROUTABLE_SECTIONS:
        v.append(f"section {section!r} not in {ROUTABLE_SECTIONS}")
    zone = op.get("zone")
    if section == "battlecard" and zone not in ZONES:
        v.append(f"battlecard op needs zone in {ZONES}, got {zone!r}")
    if section != "battlecard" and zone is not None:
        v.append(f"{section} op must have zone=null, got {zone!r}")

    # change_kind must agree with the operation it carries (the resilience contract). Tolerant: an op
    # with no change_kind (a legacy/direct-apply op, e.g. review.apply or a unit test) skips this.
    ck = op.get("change_kind")
    if ck is not None:
        if ck not in CHANGE_KINDS:
            v.append(f"unknown change_kind {ck!r}")
        elif _KIND_OP.get(ck) != operation:
            v.append(f"change_kind {ck!r} requires operation {_KIND_OP.get(ck)!r}, got {operation!r}")
        elif ck == "supersede_lead" and section != "executive_summary":
            v.append("change_kind 'supersede_lead' is executive_summary only")

    # No model-minted facts — propagation only ever authors interpretations.
    if op.get("claim_type") != "interpretation":
        v.append(f"claim_type must be 'interpretation' (no model-minted facts), got {op.get('claim_type')!r}")

    # Provenance anchor: derived_from must resolve to a surviving grounded fact.
    df = op.get("derived_from")
    if not (isinstance(df, str) and _FACT_ID_RE.match(df)):
        v.append(f"derived_from missing/malformed: {df!r}")
    elif df not in surviving_fact_ids:
        v.append(f"derived_from {df!r} does not resolve to a surviving grounded fact")

    tgt, sk, claim = op.get("target_subject_key"), op.get("subject_key"), op.get("claim")
    has_prose = isinstance(claim, str) and bool(claim.strip())

    if operation == "add":
        if tgt:
            v.append(f"add must have target_subject_key=null, got {tgt!r}")
        if not sk:
            v.append("add must carry a new subject_key")
        elif normalize_subject_key(str(sk)) in active_by_sk:
            v.append(f"add would overwrite active claim {sk!r} (use revise/retire instead)")
        if not has_prose:
            v.append("add must carry non-empty claim prose")
    else:  # revise | retire — both touch an existing active claim, in place
        if not tgt:
            v.append(f"{operation} must name the target_subject_key it touches")
        elif normalize_subject_key(str(tgt)) not in active_by_sk:
            v.append(f"{operation} target {tgt!r} is not an active battlecard/objection claim")
        if sk and tgt and normalize_subject_key(str(sk)) != normalize_subject_key(str(tgt)):
            v.append(f"{operation} must reuse the subject_key in place ({sk!r} != {tgt!r})")
        if operation == "revise":
            if not has_prose:
                v.append("revise must carry the updated claim prose")
        else:  # retire — the claim LEAVES the active card; no new prose, reason is tagged
            if has_prose:
                v.append("retire must have claim=null (the claim leaves the active card for lineage)")
            rr = op.get("retired_reason")
            if not (isinstance(rr, str) and (rr.startswith("neutralized:") or rr.startswith("invalidated:"))):
                v.append(f"retire needs retired_reason 'neutralized: ...' | 'invalidated: ...', got {rr!r}")
    return v


_JUDGE_SYSTEM = """You are the JUDGE pass of a living competitive battlecard's PROPAGATION step: the
independent adversarial check on the PROPOSE pass. This is Scout's generate-then-verify discipline
applied one layer up, to AUTHORSHIP. A proposer drafted add / revise / retire edits to the rep-facing
prose (any rep-facing section: the lead, plays, objections, positioning, pricing, snapshot, sentiment)
from grounded facts. Confirm or reject EACH, and DEFAULT TO REJECT when not
convinced. Rejecting every op and returning no rep-facing change is a correct, common outcome — most
deal-grade facts still move no specific play or objection.

You are handed: the GROUNDED FACTS (already verified TRUE — the ONLY admissible evidence), the CURRENT
active plays + objections, and the PROPOSED OPS. You have no search or fetch tools, on purpose: judge
ONLY against the grounded facts given, exactly as the proposer was constrained to. You cannot go find
new support for a weak op.

STANDING-STRENGTH FACTS (marked "standing_strength": true) are grounded my_company strengths supplied as
PIVOT FUEL, and they ARE admissible evidence. A back-foot rebuttal whose pivot is grounded by a
standing-strength fact is GROUNDED, not an invented capability — confirm it on that basis even though the
pivot fact differs from the op's derived_from trigger (derived_from anchors the STUMBLE that raises the
objection; the pivot may cite a sibling strength fact). But a standing-strength fact is NEVER a trigger:
reject any op whose derived_from is a standing-strength fact (an op must be licensed by a real change;
the deterministic floor already rejects these). Only reject a pivot as invented when it rests on NO
admissible fact at all.

REJECT an op if ANY of these holds:
- FACTS-ONLY VIOLATION (the cardinal sin): it rests on something the grounded fact does NOT state — an
  inferred motive, a speculated downstream effect, an "effective impact" broader than the fact's stated
  scope. The fact's LITERAL scope governs. "Restricted to foreign nationals" does NOT license "pulled
  for everyone"; a scoped restriction does NOT license retiring a whole play. If the prose reaches past
  what the fact says, reject it.
- INVENTED MECHANISM/REASON: the prose explains WHY or HOW a fact happened — a causal mechanism, an
  operational reason — that the source does not state, EVEN IF the conclusion itself is grounded and the
  explanation is plausibly true. "The model is disabled for all customers" can be grounded while
  "because they cannot filter users by nationality" is invented; reject the op (or it must be revised to
  drop the unstated reason). A grounded conclusion never licenses an ungrounded explanation of it.
- WRONG VALENCE: a back-foot fact (competitor strong, or WE stumble) routed to a play; or a front-foot
  fact (competitor stumbles, or WE ship) routed to an objection.
- WRONG OPERATION (not the lightest TRUE one): a retire where the fact only NARROWS a still-winning
  play (should be revise); an add duplicating a play/objection already on the card (should be revise);
  a revise where the fact actually INVALIDATES the play (should be retire).
- INVENTED: an objection no real buyer would raise from this fact, or a play asserting a competitive
  differential the fact does not actually contain. Manufactured prose to look responsive is the GenAI
  tic this product bans — kill it.
- BLAST RADIUS: it touches a claim the fact does not DIRECTLY create, undercut, or invalidate.
- WEAK RETIRE: a retire whose killing fact does not truly neutralize-to-a-wash or invalidate the
  target play. The bar to pull a play off the active card is HIGH.
- HOLLOW REBUTTAL: a back-foot objection whose answer only RESTATES the constraint ("confirm
  eligibility", "verify availability", "check with us") without pivoting to a genuine, grounded
  strength and a concrete move the rep can make. An honest objection-handler redirects to a real,
  currently-true capability; one that just names the problem leaves the rep worse off than silence.
  (The pivot's strength must be grounded — in a trigger fact OR a provided standing-strength my_company
  fact; a standing-strength-grounded pivot IS grounded and passes. Reject only if the rebuttal INVENTS a
  capability no admissible fact supports, or speculates about when the constraint lifts.)
- BLOATED / BURIED ANSWER: the op is rightly routed and factually grounded, but the prose buries the
  rep-usable answer under accreted history — e.g. a body over ~150 words, or paragraph-per-news-beat
  accretion where the CURRENT state should lead and resolved history should be one sentence of arc.
  Preserving still-true content does NOT mean retaining every prior beat verbatim: a compressed
  supersession is the CORRECT reconcile; verbatim accretion is a defect. (This defect is rewritable.)
- SELF-INCRIMINATING / NOT-REP-OWNABLE: a back-foot rebuttal that DOES pivot to a concrete move, but
  the move weakens the rep instead of the objection. HOLLOW REBUTTAL kills rebuttals that say nothing;
  this kills rebuttals that say something self-defeating. Reject if the answer: (a) coaches the BUYER to
  demand a concession, guarantee, escalation, or written commitment from us ("get your account team to
  confirm a restoration timeline in writing"); (b) commits the rep or our company to a future action
  outside the rep's authority (restoration dates, written guarantees, anything unpromisable on the
  call); or (c) concedes the buyer's switching/migration framing as warranted ("before committing to a
  migration plan"). A real objection-handler's concrete move is the REP's move, offered today, keeping
  the rep in control — not the buyer's move against us. "Concrete" is necessary but not sufficient; a
  concrete step in the wrong direction still fails.

CONFIRM an op ONLY when the grounded fact DIRECTLY and near-certainly licenses exactly that change, at
exactly that scope, routed by the correct valence, as the lightest true operation. For a back-foot
objection, "lightest true" still REQUIRES a grounded pivot — an honest constraint plus a real next move.
RECONCILING A FOLLOW-UP is a CORRECT, expected revise: when a new beat updates a claim that already
encodes earlier beats, the right op folds the new beat in while PRESERVING the still-true prior content
and the required block — confirm that. PRESERVING means keeping the facts that still matter, not
retaining every beat verbatim: when the new beat RESOLVES earlier ones, the correct reconcile leads
with the current state and compresses the resolved history to a sentence. What you reject on
blast-radius is the opposite: a revise that ERASES still-true prior content or rewrites the claim from
scratch (e.g. a reversal that deletes the earlier event instead of reconciling with it).

REWRITABLE: on a REJECT, also set "rewritable". Set true ONLY when the op is RIGHTLY ROUTED —
correct section, operation, valence, and target, at a scope the fact licenses — and the defect lives
in the PROSE alone, such that a rewrite guided by your reason could pass: an invented number,
mechanism, or reason; erased still-true prior content or a rewrite-from-scratch (that flavor of
blast-radius IS rewritable — the routing is right, the prose is wrong); a dropped required
**So what:** / **Soundbite:** block; a bloated/buried answer; a hollow or self-incriminating rebuttal. Set false when the op
should not exist at all: wrong valence, wrong operation, an invented objection or play, a weak
retire, or blast-radius because the fact does not license touching that claim. A retire carries no
prose: rewritable is always false for a retire. When rewritable is true, your reason must name
EXACTLY what must change — it is handed verbatim to the rewriter as its only feedback.

Return ONLY a single fenced ```json block:
{"verdicts": [
  {"op_index": <int — the op's given index>,
   "verdict": "confirm|reject",
   "rewritable": <bool — per the REWRITABLE rule; false on a confirm or a retire>,
   "reason": "<one line: the specific test it passed, or the specific reason rejected, grounded in the fact>"}
]}
Give EXACTLY one verdict per proposed op. When in doubt, reject."""


def _judge_ops_digest(indexed_ops: list) -> list:
    """The floor-surviving ops as the judge sees them, each tagged with its ORIGINAL op_index so
    verdicts map back unambiguously."""
    return [{
        "op_index": i,
        "operation": o.get("operation"),
        "section": o.get("section"),
        "zone": o.get("zone"),
        "valence": o.get("valence"),
        "target_subject_key": o.get("target_subject_key"),
        "subject_key": o.get("subject_key"),
        "claim": o.get("claim"),
        "derived_from": o.get("derived_from"),
        "retired_reason": o.get("retired_reason"),
        "rationale": o.get("rationale"),
    } for i, o in indexed_ops]


async def _run_judge(meta: dict, facts: list[dict], claims: list[dict], indexed_ops: list,
                     model: str | None = None) -> dict:
    comp, me = meta.get("competitor"), meta.get("my_company")
    user = (f"Competitor: {comp}" + (f"   We are: {me}" if me else "") + "\n\n"
            "GROUNDED FACTS (the ONLY admissible evidence; judge every op strictly against these):\n"
            + json.dumps(_facts_digest(facts), ensure_ascii=False, indent=2)
            + "\n\nCURRENT ACTIVE PLAYS + OBJECTIONS (the prose as it stands; the old_text a revise/"
              "retire would change):\n"
            + json.dumps(_targets_digest(claims), ensure_ascii=False, indent=2)
            + "\n\nPROPOSED OPS TO JUDGE (confirm or reject each by op_index):\n"
            + json.dumps(_judge_ops_digest(indexed_ops), ensure_ascii=False, indent=2))
    options = ClaudeAgentOptions(
        model=model or config.ORCHESTRATOR_MODEL,         # judge on Opus; fallback overrides (outage)
        system_prompt={"type": "preset", "preset": "claude_code", "append": _JUDGE_SYSTEM},
        mcp_servers={},
        allowed_tools=[],                                 # TOOLS-OFF: judge only the given facts
        disallowed_tools=["WebSearch", "WebFetch"],
        permission_mode="bypassPermissions",
        max_turns=config.JUDGE_MAX_TURNS,
        max_budget_usd=config.JUDGE_MAX_BUDGET_USD,
    )
    return await _drive(user, options, "judge")


def _parse_verdicts(text: str) -> dict:
    """op_index -> {verdict, reason} from the judge's JSON. Tolerant: coerces a digit-string op_index
    to int (models sometimes quote it) so a stylistic slip doesn't drop a real verdict; anything not a
    clean confirm normalizes to reject. Returns {} when the text has no parseable verdicts."""
    try:
        data = _extract_json(text)
    except Exception:
        return {}
    verdicts = {}
    for vd in (data.get("verdicts") or []):
        if not isinstance(vd, dict):
            continue
        oi = vd.get("op_index")
        if isinstance(oi, str) and oi.strip().isdigit():
            oi = int(oi.strip())
        if not isinstance(oi, int) or isinstance(oi, bool):
            continue
        verdicts[oi] = {
            "verdict": "confirm" if str(vd.get("verdict", "")).strip().lower() == "confirm" else "reject",
            "reason": str(vd.get("reason", "")),
            # Fail-closed like the verdict itself: only a boolean true enters the rewrite loop —
            # a missing/hedged flag means no loop, i.e. today's drop-on-reject behavior.
            "rewritable": vd.get("rewritable") is True,
        }
    return verdicts


def judge(meta: dict, facts: list[dict], claims: list[dict], indexed_ops: list) -> dict:
    """Adversarial Opus pass over the floor-surviving ops. `indexed_ops` is a list of (op_index, op)
    pairs (op_index = position in the ORIGINAL proposed list). Returns
    {'verdicts': {op_index: {'verdict','reason','rewritable','judged_by'}}, 'cost_usd',
    'raw_failures': [{'model','text'}]}.

    FAIL-CLOSED per op: anything but a clean 'confirm' normalizes to 'reject', and an op the judge
    omits from an otherwise healthy batch is treated as rejected downstream — a judge hiccup can
    only DROP an edit, never wave one onto a card (mirrors monitor's severity normalization).

    BULLETPROOF per batch (the 2026-07-01 Opus outage: two unparseable responses silently killed 4
    material drafts): an empty parse retries once on the primary judge, then ONCE on
    JUDGE_FALLBACK_MODEL — a different model family, so one provider incident can't take out both.
    Every unparseable response is captured (truncated) into raw_failures so the failure is
    diagnosable. If ALL calls fail, ops come back 'judge_unavailable' — NOT 'reject': the drafts
    ride the proposals email for explicit human judgment instead of dying silently. A fallback
    verdict is tagged judged_by='fallback:<model>' (email-gating only; never auto-applies)."""
    if not indexed_ops:
        return {"verdicts": {}, "cost_usd": 0.0, "raw_failures": []}
    plan = [config.ORCHESTRATOR_MODEL, config.ORCHESTRATOR_MODEL]
    if config.JUDGE_FALLBACK_MODEL:
        plan.append(config.JUDGE_FALLBACK_MODEL)
    cost, raw_failures, verdicts, used = 0.0, [], {}, None
    for model in plan:
        res = asyncio.run(_run_judge(meta, facts, claims, indexed_ops, model=model))
        cost += res.get("cost_usd") or 0.0
        verdicts = _parse_verdicts(res["text"])
        if verdicts:
            used = model
            break
        raw_failures.append({"model": model, "text": str(res.get("text") or "")[:4000]})
    if verdicts:
        tag = used if used == config.ORCHESTRATOR_MODEL else f"fallback:{used}"
        for v in verdicts.values():
            v["judged_by"] = tag
    else:
        # Judge UNAVAILABLE: a distinct verdict, deliberately NOT 'reject' and NOT worded
        # "fail-closed" (adjudicate keys on that string for per-op hiccups). Downstream needs no
        # special-casing: not 'reject' -> the rewrite loop skips it; not 'confirm' -> it can never
        # commit; monitor/notify surface it loudly for manual approval.
        verdicts = {i: {"verdict": "judge_unavailable", "rewritable": False, "judged_by": None,
                        "reason": "judge returned no parseable verdicts after retries and the "
                                  "fallback model (likely a model outage) — the drafted op is "
                                  "preserved for human review"}
                    for i, _ in indexed_ops}
    return {"verdicts": verdicts, "cost_usd": cost, "raw_failures": raw_failures}


# --- Bounded rewrite loop (2026-07-01): a prose-defect reject gets one guided rewrite ------------
#
# The judge rejected 2 ops on a real act-grade fact (the Sonnet-5 launch) for AUTHORING defects —
# an invented number, a rewrite-from-scratch — and the fact vanished silently. The loop: rejects the
# judge marked "rewritable" are re-authored WITH the judge's reason as the fix list (on the Opus
# tier — "upgrade the writer on failure"), re-floored, then BLIND re-judged (the re-judge never sees
# that it is a second attempt — the double-jeopardy guard). Bounded by PROPAGATE_MAX_REWRITES;
# exhaustion is surfaced loudly by the caller (proposals email), never silent.

_REWRITE_ADDENDUM = """REWRITE MODE. Each worklist item below was ALREADY AUTHORED once and REJECTED by the
adversarial judge for a prose defect. Each carries `failed_prose` (the rejected text) and
`judge_reason` (exactly why it failed). Rewrite the prose to CURE that reason and change NOTHING
ELSE: keep the routing, the structure, the still-true content, the persona, and the required
**So what:** / **Soundbite:** block. Do not fix what the reason does not name. FACTS ONLY still
governs absolutely: if the reason says a number, mechanism, or claim is not in the grounded facts,
REMOVE it — never replace it with another invented one. If the reason says still-true content was
erased or a required block was dropped, restore it from `current_text` and fold the new beat in
surgically. If the reason cannot be cured without inventing something the facts do not state,
return the item with claim "" — an empty claim tells the pipeline you could not fix it honestly.
Return the same {"authored": [{op_index, claim, persona}]} shape, one entry per worklist item,
keyed by the given op_index."""


def _rewritable_indices(ops: list, floor_results: list, verdicts: dict) -> list:
    """Original op indices eligible for a rewrite: floor-clean, judge-rejected with rewritable=True,
    and carrying prose (add/revise only — a retire has nothing to rewrite even if the judge slips)."""
    return [i for i in range(len(ops))
            if not floor_results[i]
            and (verdicts.get(i) or {}).get("verdict") == "reject"
            and (verdicts.get(i) or {}).get("rewritable") is True
            and ops[i].get("operation") in ("add", "revise")]


def _rewrite_worklist(indices: list, surface_ops: list, ops: list, verdicts: dict,
                      active_by_sk: dict) -> list:
    """The rewrite pass's worklist: the ORIGINAL author worklist row for each index (op_index
    preserved — positional identity across stages is load-bearing), annotated with the failed
    prose and the judge's reason (the rewriter's only feedback)."""
    base = {w["op_index"]: w for w in _author_worklist(surface_ops, active_by_sk)}
    out = []
    for i in indices:
        w = dict(base[i])
        w["failed_prose"] = ops[i].get("claim")
        w["judge_reason"] = (verdicts.get(i) or {}).get("reason")
        out.append(w)
    return out


async def _run_rewrite(meta: dict, worklist: list, facts: list) -> dict:
    comp, me = meta.get("competitor"), meta.get("my_company")
    user = (f"Competitor: {comp}" + (f"   We are: {me}" if me else "") + "\n\n"
            "GROUNDED FACTS (the ONLY admissible evidence; each op's derived_from points into these):\n"
            + json.dumps(_facts_digest(facts), ensure_ascii=False, indent=2)
            + "\n\nWORKLIST — REWRITE each rejected op to cure its judge_reason; key your output by "
              "op_index:\n"
            + json.dumps(worklist, ensure_ascii=False, indent=2))
    options = ClaudeAgentOptions(
        model=config.PROPAGATE_REWRITE_MODEL,             # upgraded writer: pay Opus only on failure
        system_prompt={"type": "preset", "preset": "claude_code",
                       "append": _AUTHOR_SYSTEM + "\n\n" + _REWRITE_ADDENDUM + "\n\n" + WRITING_STYLE},
        mcp_servers={},
        allowed_tools=[],                                 # TOOLS-OFF: same constraint as the author
        disallowed_tools=["WebSearch", "WebFetch"],
        permission_mode="bypassPermissions",
        max_turns=config.PROPOSE_MAX_TURNS,
        max_budget_usd=config.PROPOSE_MAX_BUDGET_USD,
    )
    return await _drive(user, options, "rewrite")


def _rewrite_loop(meta: dict, surface_ops: list, ops: list, floor_results: list, verdicts: dict,
                  author_facts: list, claims: list, active_by_sk: dict,
                  surviving_fact_ids: set) -> dict:
    """Bounded rewrite loop: judge feedback -> rewrite -> re-floor -> BLIND re-judge. Mutates ops[i]
    and verdicts[i] IN PLACE for rewritten indices (positional identity: ops index == judge op_index
    == decision-record index). Top-level floor_results is NEVER overwritten — a rewrite that flunks
    the floor terminates as a judge 'reject' with the floor failure logged in its attempt, not as a
    floor_reject. A crash anywhere degrades to today's behavior (op stays rejected), never raises.

    Returns {"attempts": {op_index: [attempt dicts]}, "cost_author": float, "cost_judge": float,
    "raw_failures": [unparseable judge responses, for the decision log]}."""
    attempts, cost_author, cost_judge, raw_failures = {}, 0.0, 0.0, []
    try:
        for _round in range(config.PROPAGATE_MAX_REWRITES):
            idx = _rewritable_indices(ops, floor_results, verdicts)
            if not idx:
                break
            for i in idx:                                 # seed history with the original attempt
                if i not in attempts:
                    v = verdicts.get(i) or {}
                    attempts[i] = [{"claim": ops[i].get("claim"), "verdict": "reject",
                                    "reason": v.get("reason"), "rewritable": True}]
            res = asyncio.run(_run_rewrite(
                meta, _rewrite_worklist(idx, surface_ops, ops, verdicts, active_by_sk), author_facts))
            cost_author += res.get("cost_usd") or 0.0
            try:
                authored = {a.get("op_index"): a
                            for a in (_extract_json(res["text"]).get("authored") or [])
                            if isinstance(a, dict) and isinstance(a.get("op_index"), int)}
            except Exception:
                authored = {}
            rejudge = []
            for i in idx:
                if i not in authored:                     # rewriter returned nothing: terminal reject
                    verdicts[i] = {"verdict": "reject", "rewritable": False,
                                   "reason": (verdicts.get(i) or {}).get("reason")}
                    attempts[i].append({"claim": None, "verdict": "reject",
                                        "reason": "rewriter returned nothing", "rewritable": False})
                    continue
                ops[i] = _finalize_op(surface_ops[i], authored[i])
                fv = floor_check(ops[i], surviving_fact_ids, active_by_sk)
                if fv:                                    # incl. the honest claim:"" escape hatch
                    verdicts[i] = {"verdict": "reject", "rewritable": False,
                                   "reason": "rewrite failed the floor: " + "; ".join(fv)}
                    attempts[i].append({"claim": ops[i].get("claim"), "verdict": "reject",
                                        "reason": "floor: " + "; ".join(fv), "rewritable": False,
                                        "floor_violations": fv})
                    continue
                rejudge.append((i, ops[i]))
            if rejudge:
                # BLIND: same digest shape as the first pass — no attempt markers, no prior reason.
                judged = judge(meta, author_facts, claims, rejudge)
                cost_judge += judged.get("cost_usd") or 0.0
                raw_failures.extend(judged.get("raw_failures") or [])
                for i, op in rejudge:
                    jv = judged["verdicts"].get(i) or {
                        "verdict": "reject", "rewritable": False,
                        "reason": "no verdict returned (fail-closed)"}
                    verdicts[i] = jv
                    attempts[i].append({"claim": op.get("claim"), "verdict": jv["verdict"],
                                        "reason": jv.get("reason"),
                                        "rewritable": jv.get("rewritable") is True})
    except Exception as e:  # NON-DISRUPTION: a rewrite crash must degrade, never kill the run
        print(f"[propagate] rewrite loop skipped ({type(e).__name__}: {e})", file=sys.stderr)
    return {"attempts": attempts, "cost_author": cost_author, "cost_judge": cost_judge,
            "raw_failures": raw_failures}


# --- Decision log (spec §17): audit trail AND authorship-shadow training corpus -----------------
SCHEMA_VERSION = 3      # v2: + attempts/rewrite_attempts/rewrite_exhausted; v3 (2026-07-01): +
PROP_DIR = "propagation"  # judged_by + judge_unavailable verdicts + judge_raw_failures payload


def _decision_records(ops: list, floor_results: list, judge_verdicts: dict,
                      facts_by_id: dict, active_by_sk: dict, rewrite_attempts: dict = None) -> list:
    """One record per PROPOSED op — floor-rejected, judge-rejected, or confirmed. Captures the
    full chain (what fired it, the edit, who decided, why, did it commit) so the log is both an
    audit trail of every model-authored prose edit and the judge's training corpus. `rewrite_attempts`
    (op_index -> attempt history, first entry = the original) marks ops that went through the rewrite
    loop; a looped op that still isn't confirmed is `rewrite_exhausted` — the caller surfaces those
    LOUDLY (proposals email), never silently."""
    records = []
    for i, op in enumerate(ops):
        violations = floor_results[i]
        if violations:                                    # floored before the judge ever saw it
            verdict, reason, committed, judged_by = "floor_reject", "; ".join(violations), False, None
        else:
            jv = judge_verdicts.get(i) or {"verdict": "reject",
                                           "reason": "no verdict returned (fail-closed)"}
            verdict, reason = jv["verdict"], jv["reason"]
            judged_by = jv.get("judged_by")
            committed = verdict == "confirm"
        att = (rewrite_attempts or {}).get(i) or []
        df = op.get("derived_from")
        fact = facts_by_id.get(df) or {}
        tgt = op.get("target_subject_key")
        old = active_by_sk.get(normalize_subject_key(str(tgt))) if tgt else None
        records.append({
            "trigger_claim_id": df,
            "trigger_source_url": fact.get("source_url"),
            "operation": op.get("operation"),
            "change_kind": op.get("change_kind"),         # the routed taxonomy label
            "section": op.get("section"),
            "zone": op.get("zone"),
            "valence": op.get("valence"),
            "subject_key": op.get("subject_key"),
            "target_subject_key": op.get("target_subject_key"),
            "old_text": (old.get("claim") if isinstance(old, dict) else None),
            "new_text": op.get("claim"),                  # null on retire
            "feed_note": op.get("feed_note"),             # the "what changed" line the updates panel shows
            "derived_from": df,
            "judge_verdict": verdict,                     # confirm | reject | floor_reject | judge_unavailable
            "judge_reason": reason,
            "judged_by": judged_by,                       # model id; "fallback:<id>" = email-gate only
            "floor_violations": violations,
            "committed": committed,
            "attempts": att,                              # rewrite history ([] = never looped)
            "rewrite_attempts": max(0, len(att) - 1),     # rounds actually spent
            # Disjoint from judge_unavailable: an unjudged op presents as UNVERIFIED (the judge never
            # ruled), not as "rejected N times" — each op lands in exactly one email section.
            "rewrite_exhausted": bool(att) and verdict not in ("confirm", "judge_unavailable"),
        })
    return records


def log_decisions(slug: str, records: list, source: str = "monitor", facts: list = None,
                  cost: dict = None, judge_raw_failures: list = None) -> list:
    """Persist the propagation decision log to the PRIVATE data store, mirroring shadow.capture's
    non-disruption contract: wrapped so it can only ever WARN, never raise into the live monitor
    path. Returns the records regardless, so a caller or test can inspect them even when no backend
    is configured (the local-FS fallback still writes; an offline test passes persist=False).

    `facts` are the FULL grounded act-fact claim dicts the ops derive from — stored alongside so a
    later out-of-band approval (scout/review.py) can apply a proposal self-contained (it needs the
    my_company tracked_facts anchor, which shadow/review never persisted to the card)."""
    try:
        now = datetime.now()
        stamp = now.strftime("%Y%m%dT%H%M%S")
        payload = {"schema_version": SCHEMA_VERSION, "slug": slug, "source": source,
                   "run_ts": now.isoformat(timespec="seconds"), "decisions": records,
                   "facts": facts or [], "cost_usd": cost or {},
                   # Truncated raw judge responses that failed to parse — the diagnosis trail the
                   # 2026-07-01 outage lacked (the text was discarded; the failure was unexplainable).
                   "judge_raw_failures": judge_raw_failures or []}
        selfserve.write_data(
            f"{PROP_DIR}/{slug}/{stamp}.json",
            json.dumps(payload, indent=2, default=str, ensure_ascii=False),
            f"propagation: decisions {source} {slug} {stamp}",
        )
    except Exception as e:  # NEVER let the decision log break a live run
        print(f"[propagate] decision-log skipped ({type(e).__name__}: {e})", file=sys.stderr)
    return records


def propagate(meta: dict, facts_with_alerts: list[dict], strength_facts: list[dict],
              claims: list[dict], slug: str = None, source: str = "monitor",
              persist: bool = True) -> dict:
    """Full propagation control flow, everything UPSTREAM of human approval:
        route (Opus, all sections, SEEDED with the materiality verdict) -> author (Sonnet) ->
        deterministic FLOOR -> judge (Opus, adversarial) -> decision log.

    `facts_with_alerts` is a list of {"fact": <grounded act-fact claim>, "alert": <the materiality
    alert carrying its so_what verdict>} — the router's routing seed. `strength_facts` are grounded
    my_company STANDING strengths (pivot fuel for a back-foot rebuttal): admissible evidence, NEVER a
    routing trigger. Returns the authored ops, floor results, judge verdicts, the CONFIRMED ops (floor-
    passed AND judge-confirmed — the only ones an apply step would touch), the router's surface_ops +
    no_surface, the decision records, and per-pass cost. APPLIES NOTHING: apply is downstream (live) or
    human-approved (review). `persist=False` skips the decision-log write (offline verification)."""
    strength_facts = strength_facts or []
    change_facts = [fa.get("fact") for fa in facts_with_alerts if fa.get("fact")]
    author_facts = change_facts + strength_facts
    facts_by_id = {f.get("id"): f for f in author_facts if f.get("id")}
    surviving_fact_ids = _trigger_fact_ids(author_facts)  # strengths excluded: pivot fuel, never a trigger
    active_by_sk = _active_targets(claims)

    # step 3a: ROUTE — which surfaces each change reshapes, across all sections (absorbs strategic_lead)
    routed = route(meta, facts_with_alerts, claims)
    surface_ops = routed["surface_ops"]

    # step 3b: AUTHOR — write the prose for each add/revise routed op (retires carry no prose)
    authored = author(meta, surface_ops, author_facts, claims)
    ops = authored["ops"]

    # step 4: FLOOR (model-free) first; only the survivors cost an Opus judge call.
    floor_results = [floor_check(o, surviving_fact_ids, active_by_sk) for o in ops]
    indexed_survivors = [(i, ops[i]) for i in range(len(ops)) if not floor_results[i]]

    judged = judge(meta, author_facts, claims, indexed_survivors)
    verdicts = judged["verdicts"]
    raw_failures = list(judged.get("raw_failures") or [])

    # step 4b: REWRITE LOOP — a prose-defect reject gets one guided rewrite + blind re-judge (2026-07-01
    # Sonnet-5 silent drop). Mutates ops/verdicts in place; exhausted rejects surface via the records.
    rw = {"attempts": {}, "cost_author": 0.0, "cost_judge": 0.0, "raw_failures": []}
    if config.PROPAGATE_MAX_REWRITES > 0:
        rw = _rewrite_loop(meta, surface_ops, ops, floor_results, verdicts,
                           author_facts, claims, active_by_sk, surviving_fact_ids)
    raw_failures += rw.get("raw_failures") or []

    records = _decision_records(ops, floor_results, verdicts, facts_by_id, active_by_sk,
                                rewrite_attempts=rw["attempts"])
    confirmed = [ops[i] for i in range(len(ops))
                 if not floor_results[i] and (verdicts.get(i) or {}).get("verdict") == "confirm"]

    cost_usd = {"route": routed.get("cost_usd"), "author": authored.get("cost_usd"),
                "judge": judged.get("cost_usd"),
                "rewrite_author": rw["cost_author"] or None, "rewrite_judge": rw["cost_judge"] or None}
    if persist and slug:
        log_decisions(slug, records, source=source, facts=author_facts, cost=cost_usd,
                      judge_raw_failures=raw_failures)

    return {
        "ops": ops,
        "surface_ops": surface_ops,
        "no_surface": routed["no_surface"],
        "run_verdict": routed.get("run_verdict") or {},  # shadow-eval consequentiality signal (was strategic_lead)
        "no_change": routed["no_surface"],               # back-compat alias for the monitor summary
        "floor_results": floor_results,
        "floor_rejected": [ops[i] for i in range(len(ops)) if floor_results[i]],
        "verdicts": verdicts,
        "confirmed": confirmed,
        "decisions": records,
        "cost_usd": cost_usd,
    }


# === Step 5: APPLY confirmed ops to the card + the RETIRE-CASCADE ==============================
#
# These are PURE, deterministic, model-free transforms on the claim list — the control line takes
# over once the judge has confirmed (mirrors how generation hands off from the SDK to code). They
# MATERIALIZE a confirmed op into a real claim object and re-validate it; an op that would produce
# an invalid claim is dropped, never written. Nothing here calls a model or the network.
#
# Lineage is sacred (spec §17): add appends a new derived interpretation; revise edits IN PLACE
# (same id, re-anchored to the firing fact); retire is a status FLIP that keeps the claim and its
# text for the lineage view — never a delete.


def _next_order(claims: list[dict], section: str, zone) -> int:
    """Append position: one past the highest order currently in this (section, zone)."""
    peers = [c.get("order", 0) for c in claims
             if c.get("section") == section and c.get("zone") == zone
             and str(c.get("status", "active")) == "active"]
    return (max(peers) + 1) if peers else 0


def _find_active(claims: list[dict], subject_key) -> dict | None:
    if not subject_key:
        return None
    norm = normalize_subject_key(str(subject_key))
    for c in claims:
        if str(c.get("status", "active")) != "active":
            continue
        if c.get("subject_key") and normalize_subject_key(str(c["subject_key"])) == norm:
            return c
    return None


# Own-source fields a REVISE strips when it re-anchors a claim to the firing fact: the revised play
# is now an interpretation derived from the new development, inheriting its provenance via
# derived_from rather than carrying a URL of its own (claim-object.md §2.3).
_OWN_SOURCE_FIELDS = ("source_url", "source_tier", "evidence_excerpt", "grounding",
                      "anchor_substitution", "corroboration")


def _no_drop(slug: str, claim: dict, op: dict):
    """The no-drop guarantee (reformat.py) at APPLY time: a judge-CONFIRMED op must never be silently
    dropped for a formatting reason. Returns a publishable claim (already valid, or render-repaired) or
    None if the claim had to be HELD — durably stored in pending_publish + flagged to the human, owed to
    the card, never cut. Model-free unless a repair is actually needed (valid claims short-circuit)."""
    if not validation_errors(claim):
        return claim
    from scout import reformat                             # lazy: avoids any import-time cycle
    status, fixed = reformat.repair_or_hold(slug, claim)   # repairs a missing So-what/Soundbite/persona, else holds
    if status in ("ok", "repaired") and not validation_errors(fixed):
        return fixed
    if status != "held":                                   # a residual (schema) error repair can't fix -> hold it too
        reformat.hold(slug, fixed, f"apply: unrepairable {validation_errors(fixed)[:2]}")
    return None


def apply_ops(claims: list[dict], confirmed_ops: list[dict], facts: list[dict],
              slug: str, today: str) -> dict:
    """Apply judge-CONFIRMED ops to a copy of `claims`, deterministically. Returns
    {'claims': new_list, 'applied': [...], 'skipped': [...], 'held': [...]}. A confirmed op whose claim
    fails the render gate is NEVER silently dropped: it is repaired in place, or HELD + flagged (no-drop
    guarantee). `skipped` is reserved for a revise/retire whose target is no longer active (already
    changed) — an ordering fact, not a content loss."""
    out = [dict(c) for c in claims]                       # shallow-copy each claim; never mutate input
    facts_by_id = {f.get("id"): f for f in facts if f.get("id")}
    applied, skipped, held = [], [], []

    for op in confirmed_ops:
        operation = op.get("operation")
        df = op.get("derived_from")
        parent = facts_by_id.get(df) or {}
        as_of = parent.get("as_of") or today

        if operation == "add":
            sk = op.get("subject_key")
            new = {
                "id": claim_id(slug, str(sk)),
                "subject_key": sk,
                "claim": op.get("claim"),
                "claim_type": "interpretation",
                "section": op.get("section"),
                "zone": op.get("zone"),
                "order": _next_order(out, op.get("section"), op.get("zone")),
                "as_of": as_of,
                "updated_on": today,                       # when this change landed (drives the changelog + badge)
                "verified": True,
                "confidence": "medium",                   # an interpretation, judge-confirmed but not grounded
                "derived_from": df,
            }
            if op.get("persona"):
                new["persona"] = op["persona"]
            publishable = _no_drop(slug, new, op)          # repair render format, or HOLD — never drop
            if publishable is None:
                held.append({"op": op, "reason": "held pending publish (render format unrepairable)"})
                continue
            out.append(publishable)
            applied.append({"operation": "add", "id": publishable["id"],
                            "subject_key": publishable["subject_key"]})

        elif operation in ("revise", "retire"):
            tgt = _find_active(out, op.get("target_subject_key"))
            if tgt is None:
                skipped.append({"op": op, "reason": "target not active (already changed?)"})
                continue
            before = dict(tgt)
            if operation == "revise":
                tgt["claim"] = op.get("claim")
                tgt["claim_type"] = "interpretation"
                tgt["derived_from"] = df
                tgt["as_of"] = as_of
                tgt["updated_on"] = today                   # when this change landed (changelog + badge)
                for k in _OWN_SOURCE_FIELDS:               # re-anchor to the firing fact's provenance
                    tgt.pop(k, None)
            else:  # retire — status flip, keep text + any own source for the lineage view
                tgt["status"] = "retired"
                tgt["retired_on"] = today
                tgt["retired_reason"] = op.get("retired_reason")
                tgt["derived_from"] = df                   # the killing fact
            publishable = _no_drop(slug, tgt, op)          # repair render format, or HOLD — never drop
            if publishable is None:                        # held: roll this claim back, it is owed to the card
                tgt.clear(); tgt.update(before)
                held.append({"op": op, "reason": "held pending publish (render format unrepairable)"})
                continue
            if publishable is not tgt:                     # a repair produced a new dict -> write it back in place
                tgt.clear(); tgt.update(publishable)
            applied.append({"operation": operation, "id": tgt["id"],
                            "subject_key": tgt["subject_key"]})

    return {"claims": out, "applied": applied, "skipped": skipped, "held": held}


def retire_cascade(claims: list[dict], falsified_fact_ids, today: str) -> dict:
    """The dependency-edge half of propagation (spec §17): when a grounded fact is FALSIFIED (its
    value flips, its source is pulled), the interpretations that descend from it just became lies.
    Walk `derived_from` and retire every ACTIVE claim anchored to a falsified fact. Pure + bounded:
    it only touches claims whose derived_from is in `falsified_fact_ids`. Returns
    {'claims': new_list, 'cascaded': [...]}."""
    falsified = set(falsified_fact_ids or [])
    out = [dict(c) for c in claims]
    cascaded = []
    for c in out:
        if str(c.get("status", "active")) != "active":
            continue
        df = c.get("derived_from")
        if df and df in falsified:
            c["status"] = "retired"
            c["retired_on"] = today
            c["retired_reason"] = f"invalidated: parent fact {df} was falsified"
            # derived_from already points at the (now falsified) parent — it IS the killer.
            if validation_errors(c):
                continue  # defensive: never leave a half-retired claim (shouldn't happen)
            cascaded.append({"id": c["id"], "subject_key": c["subject_key"], "derived_from": df})
    return {"claims": out, "cascaded": cascaded}
