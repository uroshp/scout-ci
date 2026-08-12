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
from scout.schema import (ZONES, claim_id, normalize_subject_key, render_structure_errors,
                          validation_errors, word_cap_errors)

# change_kind -> the ONLY operation that kind may carry (the resilience contract, enforced by the
# floor). A router op is rejected if its change_kind and operation disagree; kinds absent from this
# map (or a legacy op with no change_kind) skip the check, so direct apply/test paths are unaffected.
_KIND_OP = {
    "new": "add", "update": "revise", "partial_invalidation": "revise",
    "reconcile_beat": "revise",
    "supersede_lead": "revise",     # LEGACY (retired 2026-08-12): the router no longer emits it — the
                                    # lead election owns which verdict leads; kept only so a replayed
                                    # historical decision log still floor-validates.
    "full_invalidation": "retire", "neutralize": "retire",
    "supersede_retire": "retire",   # 2026-07-25: code-synthesized sweep candidates (never routed)
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
- AN EXECUTIVE_SUMMARY VERDICT (a top-line strategic verdict; one may become "Today's angle", but that
  ranking is decided downstream, not here) is written as: a bold one-line headline, ONE short proof
  sentence, then a **Soundbite:** "one line to say out loud", then a final **So what:** block with the
  rep's concrete move. Keep it short and scannable — a rep skims it in ten seconds.
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


def _targets_digest(claims: list[dict], full_for: set | None = None) -> list[dict]:
    """The ACTIVE plays + objections propose may revise or retire (reuse the EXACT subject_key).

    full_for=None (the author path): every claim's text IN FULL — to reconcile a follow-up into a
    layered claim the writer must see every prior beat (a clipped view is what made the proposer
    rewrite from scratch and erase still-true content).

    full_for=<targeted subject_keys> (the judge path, 2026-07-02 cost pass): full text ONLY for the
    claims the batch's ops actually touch (old_text comparison, blast-radius, reconcile checks are
    against targets); every other claim is a compact row — enough for the one check that needs
    non-targets (an add duplicating an existing play/objection) at a fraction of the Opus input."""
    full_keys = ({normalize_subject_key(str(k)) for k in full_for if k}
                 if full_for is not None else None)
    out = []
    for c in claims:
        if c.get("section") not in ROUTABLE_SECTIONS:
            continue
        if str(c.get("status", "active")) != "active":
            continue
        text = str(c.get("claim", ""))
        if (full_keys is not None
                and normalize_subject_key(str(c.get("subject_key"))) not in full_keys
                and len(text) > 150):
            text = text[:150] + " …[truncated — not a target of this batch]"
        out.append({"subject_key": c.get("subject_key"), "section": c.get("section"),
                    "zone": c.get("zone"), "claim": text})
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
        # Plain-string system (2026-07-02 cost pass): tools are OFF, so the ~10-15K-token
        # claude_code preset was pure input overhead on every call. Same for judge/rewrite/route.
        system_prompt=_AUTHOR_SYSTEM + "\n\n" + WRITING_STYLE,
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
            v.append("change_kind 'supersede_lead' is executive_summary only")  # legacy: not emitted since 2026-08-12

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
            if not (isinstance(rr, str) and (rr.startswith("neutralized:") or rr.startswith("invalidated:")
                                             or rr.startswith("superseded:"))):
                v.append(f"retire needs retired_reason 'neutralized: ...' | 'invalidated: ...' | "
                         f"'superseded: ...', got {rr!r}")
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
  (The hard 170-word render cap is enforced deterministically downstream — never certify length
  yourself; judge substance.)
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

ON EVERY REJECT: MATERIALITY-FIRST, THEN CURE ROUTING. A claim whose point would move a deal must
NEVER be silently dropped for a fixable reason. So on a reject, answer two things — set "material"
and "cure":

1. MATERIAL? — is the underlying POINT (not this exact prose) deal-moving: would it change what a
   rep says or does in a live deal, or how a buyer decides? The triggering fact is already act-grade,
   but an act-grade fact can spawn an op whose specific point is NOT deal-moving — judge THIS op's
   point. Set "material": true/false. material=false means the point itself does not earn a place on
   the card; set "cure":"none" and stop — it is correctly dropped (a common, correct outcome).

2. If material=true, NEVER drop the point — set "cure" to how it must be fixed:
   - "prose": the op is RIGHTLY ROUTED (correct section, operation, valence, target, scope) and the
     defect is in the PROSE alone — a guided rewrite of the wording passes. (invented number/erased
     still-true content/rewrite-from-scratch/dropped **So what:**/**Soundbite:** block/bloated-buried/
     hollow-or-self-incriminating rebuttal, where the ROUTING is right and only the words are wrong.)
   - "root": the point is material but the APPROACH/ROOT is wrong — a wrong pivot, framing, or an
     INVENTED MECHANISM (e.g. "Azure Foundry governance fixes a model-behavior breach" — governance
     does not address a behavior failure). No phrase-patch fixes this, but the deal-moving point can
     be PRESERVED and re-expressed on a correct, grounded approach. Set "cure":"root".
   - "none": the point is material but NO grounded correct expression exists in the given facts (the
     honest version would require inventing something the facts do not state). Do NOT invent to cure.
     Set "cure":"none" — it routes to the owner's urgent queue for a human, never onto the card.

FULL DIAGNOSIS, UP FRONT (this is your only feedback to the rewriter). Your "reason" must name the
COMPLETE fix, not an incremental one. For "cure":"root" the reason MUST state (a) what the material
POINT is and (b) the CORRECT grounded approach that preserves it. NEVER tell the rewriter to keep a
wrong pivot and patch a phrase — diagnose the root and name the honest approach in THIS rejection.
(The 7/31 failure: a first rejection said "keep the Foundry pivot, fix a phrase"; the honest version
— pivot to eval-only scope + the vendor's fast containment response — only surfaced on the second
rejection, too late. Name it the first time.)

"rewritable" is DERIVED (material AND cure in {prose,root}) — you may still emit it for readability,
but code recomputes it as the single source of truth for loop eligibility. A retire carries no prose:
material=false, cure="none", rewritable false for a retire.

SUPERSEDE-RETIRE CANDIDATES (change_kind "supersede_retire") are SYNTHESIZED BY CODE, not the
proposer: a deterministic sweep found the target claim still cites an identifier the grounded fact
SUPERSEDES (the identifier is named in retired_reason). Judge these with the DEAL-MOVING lens — the
card's unit of value is a claim a rep can use TODAY. CONFIRM the retire when the claim's argument
rides on the superseded identifier: a benchmark, comparison, or capability statement about a replaced
model/version/product/price that no buyer will weigh now that the replacement exists. REJECT it when
the claim's point SURVIVES the replacement and still moves deals today (the identifier is incidental
to the argument, or the comparison remains operative). The WEAK RETIRE bar above does NOT apply to
these candidates: the question is not whether the claim is false — it may be perfectly true — but
whether it still earns its place on the active card. True-but-inert is a correct reason to retire.

Return ONLY a single fenced ```json block:
{"verdicts": [
  {"op_index": <int — the op's given index>,
   "verdict": "confirm|reject",
   "material": <bool — on a reject: would the underlying POINT move a deal? omit/true on a confirm>,
   "cure": "prose|root|none — on a material reject, how to fix it (see CURE ROUTING); none on a confirm/immaterial/retire",
   "rewritable": <bool — derived = material AND cure in {prose,root}; false on a confirm or a retire>,
   "reason": "<the FULL diagnosis: what passed, or — on a material reject — what the deal-moving point is AND the correct grounded approach>"}
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
        "change_kind": o.get("change_kind"),   # 2026-07-25: the judge's supersede_retire lens keys on it
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
    # Full prose only for the claims this batch's ops touch; compact rows for the rest (the judge
    # needs non-targets only for the duplicate-add check). See _targets_digest.
    targeted = {k for _, o in indexed_ops
                for k in (o.get("target_subject_key"), o.get("subject_key")) if k}
    user = (f"Competitor: {comp}" + (f"   We are: {me}" if me else "") + "\n\n"
            "GROUNDED FACTS (the ONLY admissible evidence; judge every op strictly against these):\n"
            + json.dumps(_facts_digest(facts), ensure_ascii=False, indent=2)
            + "\n\nCURRENT ACTIVE PLAYS + OBJECTIONS (full prose for the ops' targets — the old_text "
              "a revise/retire would change; other claims truncated, enough to catch a duplicate "
              "add):\n"
            + json.dumps(_targets_digest(claims, full_for=targeted), ensure_ascii=False, indent=2)
            + "\n\nPROPOSED OPS TO JUDGE (confirm or reject each by op_index):\n"
            + json.dumps(_judge_ops_digest(indexed_ops), ensure_ascii=False, indent=2))
    options = ClaudeAgentOptions(
        model=model or config.ORCHESTRATOR_MODEL,         # judge on Opus; fallback overrides (outage)
        system_prompt=_JUDGE_SYSTEM,                      # tools-off: no preset (cost pass 2026-07-02)
        mcp_servers={},
        allowed_tools=[],                                 # TOOLS-OFF: judge only the given facts
        disallowed_tools=["WebSearch", "WebFetch"],
        permission_mode="bypassPermissions",
        max_turns=config.JUDGE_MAX_TURNS,
        max_budget_usd=config.JUDGE_MAX_BUDGET_USD,
    )
    return await _drive(user, options, "judge")


def _parse_verdicts(text: str) -> dict:
    """op_index -> {verdict, reason, material, cure, rewritable} from the judge's JSON. Tolerant:
    coerces a digit-string op_index to int (models sometimes quote it) so a stylistic slip doesn't
    drop a real verdict; anything not a clean confirm normalizes to reject. Returns {} when the text
    has no parseable verdicts.

    MATERIALITY-FIRST (2026-07-31): on a reject the judge answers 'is the underlying POINT deal-
    moving?' (`material`) and, if so, HOW to cure it (`cure`: prose | root | none). `rewritable` is
    now DERIVED (`material and cure in {prose,root}`) — the single source of truth for loop
    eligibility. Back-compat: a legacy judge response with only `rewritable` (no `material`) maps to
    material=True, cure='prose' so it behaves exactly as before. Fail-closed everywhere: a missing/
    garbled flag lands on the conservative side (material=False → drop, no spend)."""
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
        verdict = "confirm" if str(vd.get("verdict", "")).strip().lower() == "confirm" else "reject"
        cure = str(vd.get("cure", "")).strip().lower()
        if cure not in ("prose", "root", "none"):
            cure = "none"
        if "material" in vd:                               # new-schema judge response
            material = vd.get("material") is True
        else:                                              # legacy: derive from `rewritable`
            material = vd.get("rewritable") is True
            cure = "prose" if material else "none"
        verdicts[oi] = {
            "verdict": verdict,
            "reason": str(vd.get("reason", "")),
            "material": material,
            "cure": cure,
            # DERIVED single source of truth for loop eligibility (ignores any judge-emitted value).
            "rewritable": material and cure in ("prose", "root"),
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
        # material=False so an OUTAGE can never trigger the urgent-material alert — the `unjudged`
        # email owns this path; material_uncured requires a real material=True judge call.
        verdicts = {i: {"verdict": "judge_unavailable", "rewritable": False, "judged_by": None,
                        "material": False, "cure": "none",
                        "reason": "judge returned no parseable verdicts after retries and the "
                                  "fallback model (likely a model outage) — the drafted op is "
                                  "preserved for human review"}
                    for i, _ in indexed_ops}
    return {"verdicts": verdicts, "cost_usd": cost, "raw_failures": raw_failures}


# --- Lead election (2026-08-08): decide 'Today's angle' on deal impact, with hysteresis ----------
#
# The viewer's lead is the order-0 executive_summary claim, frozen since generation. When a run
# produces or revises an exec-summary verdict, THIS pass decides whether that fresh verdict is
# MATERIALLY more deal-moving than the current lead. Deciding "biggest deal impact" is judgment (the
# model); recency is only the trigger. The model returns a WINNER + a MARGIN; only decisive/clear
# margins promote (the stability bar). Fail-closed to HOLD on any parse/model failure. See
# config.LEAD_ELECTION. The apply half is promote_lead() (a pure order rewrite, model-free).
_ELECTION_SYSTEM = """You decide the LEAD of a competitive battlecard — its "Today's angle", the single
line a rep opens a live deal with. You are given the CURRENT lead (the incumbent) and one or more FRESH
challenger verdicts. Choose the ONE verdict that, if the rep could say only one thing THIS QUARTER,
moves the deal most.

JUDGE ON DEAL IMPACT, NOT NOVELTY OR RECENCY. Which verdict most changes what a buyer decides — a
pricing or cost exposure, a data / security / legal risk, a capability gap that maps to an active
evaluation? A fresher verdict does NOT win for being fresher; freshness only earned it a hearing.

THE STABILITY BAR (a working lead is not displaced on a toss-up). Return one margin:
  "decisive" — the challenger is far more deal-moving; the incumbent is now clearly secondary.
  "clear"    — the challenger is the stronger opener, not a close call.
  "marginal" — comparable / close; keep the incumbent (do NOT churn the lead for a marginal gain).
  "none"     — the incumbent is as strong or stronger; keep it.
Only "decisive" and "clear" change the lead. When unsure, keep the incumbent (margin "none" or
"marginal"). If the incumbent is itself the strongest opener, return the incumbent's subject_key.

Return ONLY JSON:
{"winner_subject_key": "<the subject_key of the strongest opener, incumbent or a challenger>",
 "margin": "decisive|clear|marginal|none",
 "rationale": "<one or two sentences: why this opener moves deals most, in the competitor's terms>"}"""


def _election_digest(claim: dict, role: str) -> dict:
    return {"role": role, "subject_key": claim.get("subject_key"), "as_of": claim.get("as_of"),
            "text": claim.get("claim")}


async def _run_election(meta: dict, incumbent: dict, challengers: list[dict]) -> dict:
    comp, me = meta.get("competitor"), meta.get("my_company")
    user = (f"Competitor: {comp}" + (f"   We are: {me}" if me else "") + "\n\n"
            "INCUMBENT (the current lead / Today's angle):\n"
            + json.dumps(_election_digest(incumbent, "incumbent"), ensure_ascii=False, indent=2)
            + "\n\nCHALLENGER VERDICT(S) (fresh this run — eligible to take the lead):\n"
            + json.dumps([_election_digest(c, "challenger") for c in challengers],
                         ensure_ascii=False, indent=2)
            + "\n\nPick the single strongest opener and its margin over the incumbent.")
    options = ClaudeAgentOptions(
        model=config.LEAD_ELECTION_MODEL,
        system_prompt=_ELECTION_SYSTEM,
        mcp_servers={},
        allowed_tools=[],                                 # TOOLS-OFF: reason only from the given verdicts
        disallowed_tools=["WebSearch", "WebFetch"],
        permission_mode="bypassPermissions",
        max_turns=config.JUDGE_MAX_TURNS,
        max_budget_usd=config.JUDGE_MAX_BUDGET_USD,
    )
    return await _drive(user, options, "lead_election")


def _lead_pinned(meta: dict) -> bool:
    """The human override: a pinned card freezes its angle — the election is skipped entirely."""
    return bool((meta.get("lead_election") or {}).get("pinned"))


def _lead_within_cooldown(meta: dict) -> bool:
    """True if the last promotion was < LEAD_COOLDOWN_DAYS ago — the anti-churn lock. Within it, only
    a 'decisive' challenger may displace the lead."""
    last = (meta.get("lead_election") or {}).get("last_promoted_on")
    if not last:
        return False
    try:
        d = datetime.strptime(str(last)[:10], "%Y-%m-%d").date()
    except Exception:
        return False
    return (datetime.now().date() - d).days < config.LEAD_COOLDOWN_DAYS


def _lead_headline(text: str, limit: int = 80) -> str:
    """A short rep-facing name for a verdict, for the feed note — its bold headline, else its first
    line, trimmed."""
    t = str(text or "").strip()
    m = re.match(r"\*\*(.+?)\*\*", t)
    h = m.group(1) if m else t.split("\n", 1)[0]
    h = re.sub(r"\s+", " ", h).strip().rstrip(".")
    return (h[: limit - 1].rstrip() + "…") if len(h) > limit else h


def _lead_election(meta: dict, claims: list[dict], challenger_keys, *,
                   within_cooldown: bool = False) -> dict | None:
    """Run one lead-election judgment. `challenger_keys` are the exec-summary subject_keys touched
    (added/revised) THIS run — the fresh verdicts eligible to challenge the incumbent. Returns an
    election dict (always, when there is a real contest) with `promoted` decided by the margin +
    cooldown bar, or None when there is nothing to decide (no active lead / no distinct challenger).
    Fail-closed: any parse/model failure lands as margin 'none' -> HOLD. Records `cost_usd`.

    The apply is NOT done here (propagate applies nothing); the caller (monitor) calls promote_lead()
    on a promote. `within_cooldown` tightens the bar to 'decisive' only (the anti-churn lock)."""
    es = sorted([c for c in claims if c.get("section") == "executive_summary"
                 and str(c.get("status", "active")) == "active"],
                key=lambda c: c.get("order", 0))
    if not es:
        return None
    incumbent = es[0]
    inc_norm = normalize_subject_key(str(incumbent.get("subject_key")))
    want = {normalize_subject_key(str(k)) for k in (challenger_keys or [])}
    challengers = [c for c in es
                   if normalize_subject_key(str(c.get("subject_key"))) in want
                   and normalize_subject_key(str(c.get("subject_key"))) != inc_norm]
    if not challengers:
        return None                                         # the fresh verdict IS already the lead, or none
    cost = 0.0
    winner_key, margin, rationale = incumbent.get("subject_key"), "none", ""
    try:
        res = asyncio.run(_run_election(meta, incumbent, challengers))
        cost = res.get("cost_usd") or 0.0
        data = _extract_json(res["text"])
        wk = data.get("winner_subject_key")
        mg = str(data.get("margin", "")).strip().lower()
        if mg in ("decisive", "clear", "marginal", "none") and wk:
            margin = mg
            rationale = str(data.get("rationale", ""))
            # only accept a winner_key that is actually the incumbent or a named challenger
            valid = {inc_norm} | {normalize_subject_key(str(c.get("subject_key"))) for c in challengers}
            winner_key = wk if normalize_subject_key(str(wk)) in valid else incumbent.get("subject_key")
    except Exception as e:                                  # fail-closed -> HOLD (no promotion, no raise)
        print(f"[propagate] lead election failed ({type(e).__name__}: {e})", file=sys.stderr)
        margin, winner_key = "none", incumbent.get("subject_key")
    bar = ("decisive",) if within_cooldown else ("decisive", "clear")
    promoted = (normalize_subject_key(str(winner_key)) != inc_norm and margin in bar)
    win_norm = normalize_subject_key(str(winner_key))
    win_claim = next((c for c in challengers
                      if normalize_subject_key(str(c.get("subject_key"))) == win_norm), incumbent)
    return {
        "promoted": promoted,
        "winner_key": winner_key if promoted else incumbent.get("subject_key"),
        "incumbent_key": incumbent.get("subject_key"),
        "challenger_keys": [c.get("subject_key") for c in challengers],
        "margin": margin,
        "within_cooldown": within_cooldown,
        "rationale": rationale,
        "incumbent_as_of": incumbent.get("as_of"),
        "winner_as_of": (win_claim.get("as_of") if promoted else incumbent.get("as_of")),
        "cost_usd": cost,
    }


def _election_record(election: dict) -> dict:
    """A distinct decision-log entry for a lead election (additive; judge_verdict='lead_election' so
    adjudicate — which keys on confirm/reject — ignores it). Auditable trail of every angle change."""
    return {
        "operation": "promote_lead",
        "change_kind": "promote_lead",
        "section": "executive_summary",
        "subject_key": election.get("winner_key"),
        "target_subject_key": election.get("incumbent_key"),
        "judge_verdict": "lead_election",
        "judge_reason": election.get("rationale"),
        "judged_by": config.LEAD_ELECTION_MODEL,
        "committed": bool(election.get("promoted")),
        "lead_promoted": bool(election.get("promoted")),
        "lead_margin": election.get("margin"),
        "lead_within_cooldown": bool(election.get("within_cooldown")),
        "lead_incumbent_key": election.get("incumbent_key"),
        "lead_winner_key": election.get("winner_key"),
        "lead_challenger_keys": election.get("challenger_keys"),
        "incumbent_as_of": election.get("incumbent_as_of"),
        "winner_as_of": election.get("winner_as_of"),
        "feed_note": election.get("feed_note"),
    }


# --- Bounded rewrite loop (2026-07-01): a prose-defect reject gets one guided rewrite ------------
#
# The judge rejected 2 ops on a real act-grade fact (the Sonnet-5 launch) for AUTHORING defects —
# an invented number, a rewrite-from-scratch — and the fact vanished silently. The loop: rejects the
# judge marked "rewritable" are re-authored WITH the judge's reason as the fix list (on the Opus
# tier — "upgrade the writer on failure"), re-floored, then BLIND re-judged (the re-judge never sees
# that it is a second attempt — the double-jeopardy guard). Bounded by PROPAGATE_MAX_REWRITES;
# exhaustion is surfaced loudly by the caller (proposals email), never silent.

_REWRITE_ADDENDUM = """REWRITE MODE. Each worklist item below was ALREADY AUTHORED once and REJECTED by the
adversarial judge. Each carries `failed_prose` (the rejected text), `judge_reason` (the FULL fix the
judge diagnosed — your only feedback), and `cure` (how to fix it: "prose" or "root").

FIRST re-read and re-verify the WHOLE op against the grounded facts — not only the phrase the reason
names. Then cure per `cure`:

- cure == "prose": the routing is RIGHT and only the wording is wrong. Cure the reason and change
  NOTHING ELSE — keep the routing, structure, still-true content, persona, and the required
  **So what:** / **Soundbite:** block. Restore erased still-true content / a dropped block from
  `current_text`.

- cure == "root": the deal-moving POINT is sound but the APPROACH is wrong (a wrong pivot, framing,
  or an invented mechanism). PRESERVE the material point the judge named in `judge_reason`, and
  RE-APPROACH the root: you MAY change the pivot, framing, mechanism, and structure to the correct
  grounded approach the judge described. This is NOT a phrase-patch — rethink the op so the root is
  correct while the point survives.

FACTS ONLY governs absolutely in BOTH modes: use only the grounded facts (trigger or standing-
strength). If the reason says a number, mechanism, or claim is not in the facts, REMOVE it — never
replace it with another invented one; and the re-approach itself must be grounded. If the point
cannot be expressed correctly without inventing something the facts do not state, return the item
with claim "" — an empty claim tells the pipeline you could not fix it honestly (it routes to the
owner, never onto the card). Return the same {"authored": [{op_index, claim, persona}]} shape, one
entry per worklist item, keyed by the given op_index."""


def _rewritable_indices(ops: list, floor_results: list, verdicts: dict) -> list:
    """Original op indices eligible for a cure round: floor-clean, judge-rejected, carrying prose
    (add/revise only — a retire has nothing to rewrite), and the judge deemed the point MATERIAL with
    a curable path (cure in {prose,root}). Gating on material (not the derived `rewritable`) is the
    self-documenting form of the 2026-07-31 materiality-first rule: material+cure:none goes to the
    urgent queue, not the loop; immaterial rejects drop."""
    return [i for i in range(len(ops))
            if not floor_results[i]
            and (verdicts.get(i) or {}).get("verdict") == "reject"
            and (verdicts.get(i) or {}).get("material") is True
            and (verdicts.get(i) or {}).get("cure") in ("prose", "root")
            and ops[i].get("operation") in ("add", "revise")]


def _demote_overcap_confirms(ops: list, floor_results: list, verdicts: dict) -> dict:
    """The deterministic LENGTH FLOOR on judge confirms (2026-07-25): the judge certifies substance,
    code certifies length. A floor-clean, judge-CONFIRMED add/revise whose prose exceeds the render
    cap (schema.word_cap_errors — the SAME count the render gate enforces) is demoted IN PLACE to a
    rewritable reject whose reason is the exact cap violation, so the existing rewrite machinery
    (facts-only rewrite + blind re-judge) cures and re-verifies it. Returns {op_index: {"verdict":
    <original verdict dict>, "claim": <original text>}} so the caller can RESTORE any op the cure
    fails on — a confirmed material claim never degrades to an exhausted reject."""
    demoted = {}
    for i in range(len(ops)):
        if floor_results[i] or (verdicts.get(i) or {}).get("verdict") != "confirm":
            continue
        if ops[i].get("operation") not in ("add", "revise"):
            continue
        cap_errs = word_cap_errors(ops[i])
        if not cap_errs:
            continue
        # Save the WHOLE op (the cure rewrite replaces ops[i] via _finalize_op, so this reference
        # stays the untouched original) — restore must bring back text AND fields like persona.
        demoted[i] = {"verdict": dict(verdicts[i]), "op": ops[i]}
        # material+cure:prose keeps the demotion loop-eligible under the materiality gate (a
        # judge-confirmed op is material by definition; the fix is a pure prose condense).
        verdicts[i] = {"verdict": "reject", "rewritable": True, "material": True, "cure": "prose",
                       "judged_by": (verdicts.get(i) or {}).get("judged_by"),
                       "reason": "judge-confirmed but over the deterministic render cap — condense "
                                 "ONLY, change nothing else: " + "; ".join(cap_errs)}
    return demoted


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
        w["cure"] = (verdicts.get(i) or {}).get("cure") or "prose"   # branch the addendum (root vs prose)
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
        system_prompt=_AUTHOR_SYSTEM + "\n\n" + _REWRITE_ADDENDUM + "\n\n" + WRITING_STYLE,
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
                  surviving_fact_ids: set, rounds: int | None = None,
                  eligible: set | None = None) -> dict:
    """Bounded rewrite loop: judge feedback -> rewrite -> re-floor -> BLIND re-judge. Mutates ops[i]
    and verdicts[i] IN PLACE for rewritten indices (positional identity: ops index == judge op_index
    == decision-record index). Top-level floor_results is NEVER overwritten — a rewrite that flunks
    the floor terminates as a judge 'reject' with the floor failure logged in its attempt, not as a
    floor_reject. A crash anywhere degrades to today's behavior (op stays rejected), never raises.

    `rounds` overrides config.PROPAGATE_MAX_REWRITES (the length-cure pass runs on its OWN budget);
    `eligible` restricts the loop to those op indices (so cure rounds can't be spent on leftover
    content rejects that happen to still be marked rewritable). Defaults preserve today's behavior.

    Returns {"attempts": {op_index: [attempt dicts]}, "cost_author": float, "cost_judge": float,
    "raw_failures": [unparseable judge responses, for the decision log]}."""
    attempts, cost_author, cost_judge, raw_failures = {}, 0.0, 0.0, []
    try:
        for _round in range(rounds if rounds is not None else config.PROPAGATE_MAX_REWRITES):
            idx = _rewritable_indices(ops, floor_results, verdicts)
            if eligible is not None:
                idx = [i for i in idx if i in eligible]
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
                # These ops entered the loop as MATERIAL (via _rewritable_indices). A terminal
                # failure here keeps material=True + cure="none" so it reads as uncured-material
                # (→ material_uncured → urgent email), never a silent drop of a deal-moving point.
                if i not in authored:                     # rewriter returned nothing: terminal reject
                    verdicts[i] = {"verdict": "reject", "rewritable": False,
                                   "material": True, "cure": "none",
                                   "reason": (verdicts.get(i) or {}).get("reason")}
                    attempts[i].append({"claim": None, "verdict": "reject",
                                        "reason": "rewriter returned nothing", "rewritable": False})
                    continue
                ops[i] = _finalize_op(surface_ops[i], authored[i])
                fv = floor_check(ops[i], surviving_fact_ids, active_by_sk)
                if fv:                                    # incl. the honest claim:"" escape hatch
                    verdicts[i] = {"verdict": "reject", "rewritable": False,
                                   "material": True, "cure": "none",
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
                        "material": True, "cure": "none",     # material op the re-judge dropped -> escalate
                        "reason": "no verdict returned (fail-closed)"}
                    verdicts[i] = jv
                    attempts[i].append({"claim": op.get("claim"), "verdict": jv["verdict"],
                                        "reason": jv.get("reason"),
                                        "rewritable": jv.get("rewritable") is True})
    except Exception as e:  # NON-DISRUPTION: a rewrite crash must degrade, never kill the run
        print(f"[propagate] rewrite loop skipped ({type(e).__name__}: {e})", file=sys.stderr)
    return {"attempts": attempts, "cost_author": cost_author, "cost_judge": cost_judge,
            "raw_failures": raw_failures}


def _render_gate(slug: str, ops: list, floor_results: list, verdicts: dict,
                 active_by_sk: dict, *, repair=None) -> dict:
    """PRE-EMAIL render gate (2026-07-18): validate + auto-repair every judge-confirmed add/revise
    BEFORE the decision log and the proposals email, so the email shows the PUBLISHABLE text and
    calls out anything HELD. (The incident: a 201-word confirmed op rode the email looking fine,
    then was silently held at approve time — the gate used to exist only at apply.) Mutates ops in
    place (positional identity with the decision records, the same contract _rewrite_loop uses).

    Returns {"held": {op_index: {"reason": str}}, "condensed": {op_index: True}, "cost_usd": dict}.
    Held ops are already durably HELD in pending_publish by repair_or_hold(alert=False); the email
    is their loud flag, so no separate hold email. "condensed" marks ops whose over-cap prose the
    gate condensed — each was RE-VERIFIED by the fidelity judge inside repair_or_hold (2026-07-25
    "re-judge everywhere"; a condense that fails that judge comes back held, so a condensed op here
    is by construction verified). A per-op crash leaves that op untouched (degrades to the old
    at-apply gating), never breaks the run. `repair` is injectable for tests (2-tuple returns from
    legacy injected repairers are tolerated)."""
    held, condensed, cost_acc = {}, {}, {}
    if repair is None:
        from scout import reformat                         # lazy: avoids any import-time cycle
        repair = lambda s, o: reformat.repair_or_hold(s, o, alert=False, cost=cost_acc)
    for i, op in enumerate(ops):
        try:
            if floor_results[i] or (verdicts.get(i) or {}).get("verdict") != "confirm":
                continue
            if op.get("operation") not in ("add", "revise"):
                continue                                   # retires carry no prose to gate
            # A REVISE missing its persona inherits the badge from the claim it edits — a field the
            # card already carries; no model call for it (apply keeps the target's persona anyway).
            tgt = op.get("target_subject_key")
            if not op.get("persona") and tgt:
                old = active_by_sk.get(normalize_subject_key(str(tgt)))
                if isinstance(old, dict) and old.get("persona"):
                    op["persona"] = old["persona"]
            if not render_structure_errors(op):
                continue                                   # clean op: zero cost, no model call
            had_cap = bool(word_cap_errors(op))
            status, fixed = repair(slug, op)
            ops[i] = fixed
            if status == "held":
                held[i] = {"reason": "; ".join(render_structure_errors(fixed))
                                     + "; auto-repair exhausted"}
            elif status == "repaired" and had_cap and fixed.get("claim") != op.get("claim"):
                condensed[i] = True
        except Exception as e:
            print(f"[propagate] render gate skipped for op {i} ({type(e).__name__}: {e})",
                  file=sys.stderr)
    return {"held": held, "condensed": condensed, "cost_usd": cost_acc}


# --- Decision log (spec §17): audit trail AND authorship-shadow training corpus -----------------
SCHEMA_VERSION = 7      # v2: + attempts/rewrite_attempts/rewrite_exhausted; v3 (2026-07-01): +
PROP_DIR = "propagation"  # judged_by + judge_unavailable verdicts + judge_raw_failures payload
                          # v4 (2026-07-18): + persona + held_for_format/hold_reason (pre-email gate)
                          # v5 (2026-07-25): + length_demoted/length_cured/length_cure_attempts +
                          # condensed_at_gate/gate_rejudge (length-cure loop) + superseded_terms
                          # (supersede-retire) — all additive
                          # v6 (2026-07-31): + material/cure/material_uncured (materiality-first cure
                          # routing) — all additive; judge_verdict semantics unchanged
                          # v7 (2026-08-08): + a distinct 'lead_election' decision record (judge_verdict
                          # 'lead_election', ignored by adjudicate) for angle changes — all additive


def _decision_records(ops: list, floor_results: list, judge_verdicts: dict,
                      facts_by_id: dict, active_by_sk: dict, rewrite_attempts: dict = None,
                      length_cures: dict = None) -> list:
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
            jv = {}
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
            "persona": op.get("persona"),                 # v4: survives into the log so approve-time
                                                          # apply is model-free (no re-classify)
            "old_text": (old.get("claim") if isinstance(old, dict) else None),
            "new_text": op.get("claim"),                  # null on retire
            "feed_note": op.get("feed_note"),             # the "what changed" line the updates panel shows
            "derived_from": df,
            "judge_verdict": verdict,                     # confirm | reject | floor_reject | judge_unavailable | gated_routine
            "judge_reason": reason,
            "judged_by": judged_by,                       # model id; "fallback:<id>" = email-gate only
            "floor_violations": violations,
            "committed": committed,
            "attempts": att,                              # rewrite history ([] = never looped)
            "rewrite_attempts": max(0, len(att) - 1),     # rounds actually spent
            # Disjoint from judge_unavailable: an unjudged op presents as UNVERIFIED (the judge never
            # ruled), not as "rejected N times" — each op lands in exactly one email section.
            "rewrite_exhausted": bool(att) and verdict not in ("confirm", "judge_unavailable"),
            # v6 materiality-first (additive; null/false unless the judge ruled on a reject):
            "material": jv.get("material"),
            "cure": jv.get("cure"),
        })
        # material_uncured = a DEAL-MOVING point that never made it onto the card: the urgent-email
        # trigger. Superset of rewrite_exhausted — it ALSO fires for the no-loop cure:"none" case
        # (judge says material but no grounded correct expression). judge_verdict semantics unchanged.
        rec = records[-1]
        rec["material_uncured"] = bool(rec.get("material")) and rec["judge_verdict"] == "reject" \
            and not rec["committed"] and (rec.get("cure") == "none" or rec["rewrite_exhausted"])
        lc = (length_cures or {}).get(i)
        if lc:                                            # v5: the length-cure trail (additive)
            records[-1]["length_demoted"] = True
            records[-1]["length_cured"] = bool(lc.get("cured"))
            records[-1]["length_cure_attempts"] = lc.get("attempts", 0)
        if op.get("superseded_term"):                     # v5: the sweep's trigger term (additive)
            records[-1]["superseded_term"] = op["superseded_term"]
            records[-1]["retired_reason"] = op.get("retired_reason")
    return records


def log_decisions(slug: str, records: list, source: str = "monitor", facts: list = None,
                  cost: dict = None, judge_raw_failures: list = None,
                  superseded_terms: list = None) -> list:
    """Persist the propagation decision log to the PRIVATE data store, mirroring shadow.capture's
    non-disruption contract: wrapped so it can only ever WARN, never raise into the live monitor
    path. Returns the records regardless, so a caller or test can inspect them even when no backend
    is configured (the local-FS fallback still writes; an offline test passes persist=False).

    `facts` are the FULL grounded act-fact claim dicts the ops derive from — stored alongside so a
    later out-of-band approval (scout/review.py) can apply a proposal self-contained (it needs the
    my_company tracked_facts anchor, which shadow/review never persisted to the card).

    SOURCE CONVENTION (2026-07-25): a source beginning with 'manual' marks an ADVISORY log — an
    audit record of an out-of-band pass. review._latest_log skips advisory logs, so they can never
    become the run scout-proposals lists or --approve applies."""
    try:
        now = datetime.now()
        stamp = now.strftime("%Y%m%dT%H%M%S")
        payload = {"schema_version": SCHEMA_VERSION, "slug": slug, "source": source,
                   "run_ts": now.isoformat(timespec="seconds"), "decisions": records,
                   "facts": facts or [], "cost_usd": cost or {},
                   # Truncated raw judge responses that failed to parse — the diagnosis trail the
                   # 2026-07-01 outage lacked (the text was discarded; the failure was unexplainable).
                   "judge_raw_failures": judge_raw_failures or [],
                   # v5: the verified superseded identifiers this run swept on ([] on most runs).
                   "superseded_terms": superseded_terms or []}
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

    # CONSEQUENTIALITY GATE (docs/consequential-filter-spec.md): the router's own run_verdict
    # decides whether this change-set earns the paid authoring stages. shadow (default) changes
    # nothing; gate mode + an EXPLICIT consequential=False stops here — facts and alerts already
    # landed upstream (the card stays current), and the routed reshaping is DEFERRED: recorded
    # per-op in the decision log (verdict "gated_routine"), surfaced by the caller's digest note,
    # never silent. FAIL-OPEN: a missing/empty verdict counts as consequential (the router's own
    # when-unsure bias) — the gate can only ever skip work the router explicitly called routine.
    rv = routed.get("run_verdict") or {}
    if config.CONSEQUENTIAL_FILTER == "gate" and surface_ops and rv.get("consequential") is False:
        gated_ops = [_finalize_op(op, None) for op in surface_ops]
        gated_verdicts = {i: {"verdict": "gated_routine", "rewritable": False, "judged_by": None,
                              "reason": rv.get("consequence_rationale") or "routine run"}
                          for i in range(len(gated_ops))}
        records = _decision_records(gated_ops, [[] for _ in gated_ops], gated_verdicts,
                                    facts_by_id, active_by_sk)
        cost_usd = {"route": routed.get("cost_usd")}
        if persist and slug:
            log_decisions(slug, records, source=source, facts=author_facts, cost=cost_usd)
        return {"ops": [], "surface_ops": surface_ops, "no_surface": routed["no_surface"],
                "run_verdict": rv, "no_change": routed["no_surface"], "floor_results": [],
                "floor_rejected": [], "verdicts": gated_verdicts, "confirmed": [],
                "election": None, "decisions": records, "cost_usd": cost_usd, "gated": "routine"}

    # step 3b: AUTHOR — write the prose for each add/revise routed op (retires carry no prose)
    authored = author(meta, surface_ops, author_facts, claims)
    ops = authored["ops"]

    # step 3c: SUPERSEDE-RETIRE SWEEP (2026-07-25) — the router NAMED superseded identifiers, code
    # verifies them against the grounded evidence and sweeps the card; every hit becomes a retire
    # CANDIDATE appended to this run's op list, so the ordinary floor + judge (deal-moving lens)
    # decide each one. Review/live only: shadow never spends the extra judge tokens.
    sup_terms = []
    if config.SUPERSEDE_SWEEP and config.PROPAGATE_MODE in ("review", "live"):
        try:
            sup_terms = verified_superseded_terms(surface_ops, facts_by_id, active_by_sk)
            if sup_terms:
                routed_keys = {str(k) for op in surface_ops
                               for k in (op.get("target_subject_key"), op.get("subject_key")) if k}
                cands = supersede_candidates(claims, sup_terms, routed_keys)
                if cands:
                    surface_ops = surface_ops + cands      # positional identity: ops[i] ~ surface_ops[i]
                    ops = ops + [dict(c) for c in cands]
        except Exception as e:  # NON-DISRUPTION: the sweep degrades, never kills the run
            print(f"[propagate] supersede sweep skipped ({type(e).__name__}: {e})", file=sys.stderr)

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

    # step 4b2: DETERMINISTIC LENGTH FLOOR + dedicated cure loop (2026-07-25, the 182-word hold).
    # The judge certifies substance; code certifies length. A confirmed-but-over-cap op is demoted
    # to a rewritable reject and cured on its OWN budget (a content rewrite that ate step 4b's
    # budget can't starve the cure), then BLIND re-judged. Uncured -> RESTORED to its confirmed
    # state so the render gate condenses-or-holds it — never an exhausted reject.
    length_cures = {}
    if config.PROPAGATE_MAX_LENGTH_CURES > 0:
        demoted = _demote_overcap_confirms(ops, floor_results, verdicts)
        if demoted:
            cure = _rewrite_loop(meta, surface_ops, ops, floor_results, verdicts,
                                 author_facts, claims, active_by_sk, surviving_fact_ids,
                                 rounds=config.PROPAGATE_MAX_LENGTH_CURES, eligible=set(demoted))
            raw_failures += cure.get("raw_failures") or []
            for i, orig in demoted.items():
                att = (cure["attempts"].get(i) or [])
                # Cured = the blind re-judge confirmed AND the deterministic cap now passes (a
                # confirm on still-over-cap prose is possible — the judge rules substance only).
                cured = ((verdicts.get(i) or {}).get("verdict") == "confirm"
                         and not word_cap_errors(ops[i]))
                if not cured:                              # RESTORE: never degrade a confirm
                    verdicts[i] = orig["verdict"]
                    ops[i] = orig["op"]
                length_cures[i] = {"cured": cured, "attempts": max(0, len(att) - 1)}
                rw["attempts"][i] = (rw["attempts"].get(i) or []) + att
            length_cures["_cost_author"] = cure["cost_author"]
            length_cures["_cost_judge"] = cure["cost_judge"]

    # step 4c: PRE-EMAIL RENDER GATE — repair/hold confirmed ops BEFORE the log and the email, so
    # the proposals email shows publishable text and flags holds inline (2026-07-18). Shadow mode
    # skips it: no new spend, no holds, from a mode that never emails or applies. Any over-cap op
    # the gate condenses was fidelity-re-judged inside repair_or_hold (2026-07-25).
    gate = {"held": {}, "condensed": {}, "cost_usd": {}}
    if slug and config.PROPAGATE_MODE in ("review", "live"):
        gate = _render_gate(slug, ops, floor_results, verdicts, active_by_sk)
    render_holds = gate["held"]

    records = _decision_records(ops, floor_results, verdicts, facts_by_id, active_by_sk,
                                rewrite_attempts=rw["attempts"],
                                length_cures={k: v for k, v in length_cures.items()
                                              if isinstance(k, int)})
    for i, info in render_holds.items():
        records[i]["held_for_format"] = True               # judge-confirmed but NOT publishable yet
        records[i]["hold_reason"] = info["reason"]
        records[i]["committed"] = False                    # a held op did not commit
    for i in gate["condensed"]:
        records[i]["condensed_at_gate"] = True             # condensed AND fidelity-judged at the gate
        records[i]["gate_rejudge"] = "confirm"             # held-on-reject never reaches here
    confirmed = [ops[i] for i in range(len(ops))
                 if not floor_results[i] and (verdicts.get(i) or {}).get("verdict") == "confirm"
                 and i not in render_holds]

    # step 4d: LEAD ELECTION (2026-08-08) — a confirmed exec-summary verdict this run is a CHALLENGER
    # for 'Today's angle'. A model judges deal-impact vs the incumbent; only a decisive/clear margin
    # promotes (within a cooldown, only decisive). AUTO-APPLIES with hysteresis (the apply is
    # promote_lead(), done by the monitor's write path); this pass only DECIDES. Review/live only, and
    # skipped on a pinned card. propagate() applies nothing itself, so `election` rides the return.
    election = None
    if (config.LEAD_ELECTION and config.PROPAGATE_MODE in ("review", "live")
            and not _lead_pinned(meta)):
        exec_revises = [op for op in confirmed
                        if op.get("section") == "executive_summary"
                        and op.get("operation") == "revise" and op.get("target_subject_key")]
        if exec_revises:
            fresh = {normalize_subject_key(str(op["target_subject_key"])): op.get("claim")
                     for op in exec_revises}
            view = [({**c, "claim": fresh[normalize_subject_key(str(c.get("subject_key")))]}
                     if (c.get("section") == "executive_summary"
                         and normalize_subject_key(str(c.get("subject_key"))) in fresh) else c)
                    for c in claims]
            election = _lead_election(meta, view, [op["target_subject_key"] for op in exec_revises],
                                      within_cooldown=_lead_within_cooldown(meta))
            if election and election["promoted"]:
                win = next((c for c in view if normalize_subject_key(str(c.get("subject_key")))
                            == normalize_subject_key(str(election["winner_key"]))), None)
                inc = next((c for c in view if normalize_subject_key(str(c.get("subject_key")))
                            == normalize_subject_key(str(election["incumbent_key"]))), None)
                election["feed_note"] = (
                    f"Today's angle changed: \"{_lead_headline(win.get('claim') if win else '')}\" now "
                    f"leads, displacing \"{_lead_headline(inc.get('claim') if inc else '')}\"")
            if election:
                records = records + [_election_record(election)]

    cost_usd = {"route": routed.get("cost_usd"), "author": authored.get("cost_usd"),
                "judge": judged.get("cost_usd"),
                "rewrite_author": rw["cost_author"] or None, "rewrite_judge": rw["cost_judge"] or None,
                "length_cure_author": length_cures.get("_cost_author") or None,
                "length_cure_judge": length_cures.get("_cost_judge") or None,
                "reformat": gate["cost_usd"].get("reformat") or None,
                "gate_judge": gate["cost_usd"].get("gate_judge") or None,
                "lead_election": (election or {}).get("cost_usd") or None}
    if persist and slug:
        log_decisions(slug, records, source=source, facts=author_facts, cost=cost_usd,
                      judge_raw_failures=raw_failures, superseded_terms=sup_terms)

    return {
        "ops": ops,
        "superseded_terms": sup_terms,
        "surface_ops": surface_ops,
        "no_surface": routed["no_surface"],
        "run_verdict": routed.get("run_verdict") or {},  # shadow-eval consequentiality signal (was strategic_lead)
        "no_change": routed["no_surface"],               # back-compat alias for the monitor summary
        "floor_results": floor_results,
        "floor_rejected": [ops[i] for i in range(len(ops)) if floor_results[i]],
        "verdicts": verdicts,
        "confirmed": confirmed,
        "election": election,                            # None, or the auto-apply lead-election decision
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


def promote_lead(claims: list[dict], winner_subject_key) -> list[dict]:
    """Deterministic order rewrite (the apply half of a lead election): make `winner_subject_key` the
    executive_summary LEAD (order 0) and renumber the section's remaining ACTIVE claims 1..N by their
    current order (stable). Returns a NEW list; never mutates the input. Fail-safe no-op (returns the
    claims unchanged) when the winner is not an active exec-summary claim or is already order 0, so a
    stale/unknown key can never scramble the section. Text and lineage are untouched — this only moves
    the sort key that `page._briefing` reads to pick 'Today's angle'."""
    out = [dict(c) for c in claims]
    norm = normalize_subject_key(str(winner_subject_key)) if winner_subject_key else None
    es = [c for c in out if c.get("section") == "executive_summary"
          and str(c.get("status", "active")) == "active"]
    if not es or norm is None:
        return out
    winner = next((c for c in es
                   if normalize_subject_key(str(c.get("subject_key"))) == norm), None)
    if winner is None:
        return out                                          # unknown/inactive winner -> no change
    rest = sorted([c for c in es if c is not winner], key=lambda c: c.get("order", 0))
    ordered = [winner] + rest
    if all(c.get("order") == i for i, c in enumerate(ordered)):
        return out                                          # already the lead in this exact order
    for i, c in enumerate(ordered):
        c["order"] = i
    return out


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


# === Supersede-retire sweep (2026-07-25): true-but-inert claims leave the active card ===========
#
# Owner's philosophy: there is NO "stale" state — a claim either moves deals or it doesn't. When a
# grounded fact establishes that a named identifier (a model, product, version, price list) is
# REPLACED, other active claims still citing the OLD identifier have usually stopped moving deals,
# even though they are still true. The model NAMES the superseded identifiers (route.py emits
# superseded_terms per op); code VERIFIES the naming against the grounded evidence and SWEEPS the
# card; the judge decides retire-or-keep PER CLAIM with the deal-moving lens. Retirement reuses the
# existing lineage-preserving machinery — never a delete, and a rejected candidate changes nothing.


def verified_superseded_terms(surface_ops: list, facts_by_id: dict, active_by_sk: dict) -> list[dict]:
    """The control-vs-model seam: the router NAMED superseded identifiers; this pure helper keeps a
    term ONLY if it is grounded — literally present (casefold) in the trigger fact's claim text or
    evidence_excerpt, or in the current text of the claim the op revises. Terms under 3 chars or
    purely numeric/date-shaped are dropped (too collision-prone to sweep on). Returns
    [{'term', 'trigger_claim_id'}], deduped casefold."""
    out, seen = [], set()
    for op in surface_ops or []:
        df = op.get("derived_from")
        fact = facts_by_id.get(df) or {}
        hay = ((str(fact.get("claim") or "")) + " " + (str(fact.get("evidence_excerpt") or ""))).casefold()
        tgt = op.get("target_subject_key")
        cur = active_by_sk.get(normalize_subject_key(str(tgt))) if tgt else None
        hay_claim = str(cur.get("claim") or "").casefold() if isinstance(cur, dict) else ""
        for t in (op.get("superseded_terms") or []):
            if not isinstance(t, str):
                continue
            t = t.strip()
            key = t.casefold()
            if len(t) < 3 or key in seen:
                continue
            if re.fullmatch(r"[\d\W_]+", t):               # purely numeric/date/punctuation
                continue
            if key in hay or key in hay_claim:
                seen.add(key)
                out.append({"term": t, "trigger_claim_id": df,
                            "as_of": fact.get("as_of")})   # the supersession date (sweep filter)
    return out


def supersede_candidates(claims: list[dict], terms: list[dict], exclude_subject_keys) -> list[dict]:
    """Deterministic sweep: synthesize a judge-ready RETIRE candidate for every ACTIVE routable claim
    whose text cites a verified superseded term — excluding claims this run already routes to
    (they're being revised/retired anyway) and the trigger facts themselves. Pure; never mutates
    input; the JUDGE decides each candidate (deal-moving lens), code never retires on its own."""
    if not terms:
        return []
    excl = {normalize_subject_key(str(k)) for k in (exclude_subject_keys or set())}
    trigger_ids = {t.get("trigger_claim_id") for t in terms}
    cands = []
    for c in claims or []:
        if c.get("section") not in ROUTABLE_SECTIONS:
            continue
        if str(c.get("status", "active")) != "active":
            continue
        sk = c.get("subject_key")
        if not sk or c.get("id") in trigger_ids:
            continue
        if normalize_subject_key(str(sk)) in excl:
            continue
        text = str(c.get("claim") or "").casefold()
        hit = next((t for t in terms if t["term"].casefold() in text), None)
        if hit is None:
            continue
        # RECONCILED-HISTORY FILTER: a claim touched ON/AFTER the supersession date was written
        # knowing the news — its mention of the old identifier is deliberate compressed history
        # ("Opus 5 replaces Opus 4.8"), not staleness. ISO date strings compare lexically.
        claim_date = str(c.get("updated_on") or c.get("as_of") or "")
        if hit.get("as_of") and claim_date and claim_date >= str(hit["as_of"]):
            continue
        cands.append({
            "operation": "retire", "section": c.get("section"), "zone": c.get("zone"),
            "valence": "neutral", "change_kind": "supersede_retire",
            "target_subject_key": sk, "subject_key": sk,
            "claim": None, "claim_type": "interpretation",
            "derived_from": hit["trigger_claim_id"],
            "superseded_term": hit["term"],
            "retired_reason": f"superseded: still cites {hit['term']}, replaced per the linked "
                              f"update ({hit['trigger_claim_id']})",
            "feed_note": f"Retired a {c.get('section')} claim citing {hit['term']}: the identifier "
                         f"is superseded and the claim no longer moves deals.",
            "why": f"active claim still cites superseded identifier {hit['term']!r}",
        })
    return cands


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
