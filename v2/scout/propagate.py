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
from scout.schema import ZONES, claim_id, normalize_subject_key, validation_errors


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
  worked). The strength you pivot to MUST itself be grounded — drawn from the given facts or the
  card's existing claims, never invented — and do NOT speculate about if/when the constraint resolves
  unless a fact states it. A rebuttal that only restates the problem is a FAIL; rep-facing prose must
  leave the rep with a move.
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
        if c.get("section") not in ("battlecard", "objection_handling"):
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
    if section not in ("battlecard", "objection_handling"):
        v.append(f"section {section!r} not in (battlecard, objection_handling)")
    zone = op.get("zone")
    if section == "battlecard" and zone not in ZONES:
        v.append(f"battlecard op needs zone in {ZONES}, got {zone!r}")
    if section == "objection_handling" and zone is not None:
        v.append(f"objection_handling op must have zone=null, got {zone!r}")

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
prose (plays + objections) from grounded facts. Confirm or reject EACH, and DEFAULT TO REJECT when not
convinced. Rejecting every op and returning no rep-facing change is a correct, common outcome — most
deal-grade facts still move no specific play or objection.

You are handed: the GROUNDED FACTS (already verified TRUE — the ONLY admissible evidence), the CURRENT
active plays + objections, and the PROPOSED OPS. You have no search or fetch tools, on purpose: judge
ONLY against the grounded facts given, exactly as the proposer was constrained to. You cannot go find
new support for a weak op.

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
  (The pivot's strength must be grounded — reject it equally if the rebuttal INVENTS a capability or
  speculates about when the constraint lifts.)

CONFIRM an op ONLY when the grounded fact DIRECTLY and near-certainly licenses exactly that change, at
exactly that scope, routed by the correct valence, as the lightest true operation. For a back-foot
objection, "lightest true" still REQUIRES a grounded pivot — an honest constraint plus a real next move.

Return ONLY a single fenced ```json block:
{"verdicts": [
  {"op_index": <int — the op's given index>,
   "verdict": "confirm|reject",
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


async def _run_judge(meta: dict, facts: list[dict], claims: list[dict], indexed_ops: list) -> dict:
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
        model=config.ORCHESTRATOR_MODEL,                  # judge on Opus (spec §17)
        system_prompt={"type": "preset", "preset": "claude_code", "append": _JUDGE_SYSTEM},
        mcp_servers={},
        allowed_tools=[],                                 # TOOLS-OFF: judge only the given facts
        disallowed_tools=["WebSearch", "WebFetch"],
        permission_mode="bypassPermissions",
        max_turns=config.JUDGE_MAX_TURNS,
        max_budget_usd=config.JUDGE_MAX_BUDGET_USD,
    )
    return await _drive(user, options, "judge")


def judge(meta: dict, facts: list[dict], claims: list[dict], indexed_ops: list) -> dict:
    """Adversarial Opus pass over the floor-surviving ops. `indexed_ops` is a list of (op_index, op)
    pairs (op_index = position in the ORIGINAL proposed list). Returns
    {'verdicts': {op_index: {'verdict','reason'}}, 'cost_usd'}.

    FAIL-CLOSED: anything but a clean 'confirm' normalizes to 'reject', and an op the judge omits a
    verdict for is treated as rejected downstream — a judge hiccup can only DROP an edit, never wave
    one onto a card (mirrors monitor's severity normalization)."""
    if not indexed_ops:
        return {"verdicts": {}, "cost_usd": 0.0}
    res = asyncio.run(_run_judge(meta, facts, claims, indexed_ops))
    try:
        data = _extract_json(res["text"])
    except Exception:
        data = {"verdicts": []}
    verdicts = {}
    for vd in (data.get("verdicts") or []):
        if not isinstance(vd, dict) or not isinstance(vd.get("op_index"), int):
            continue
        verdicts[vd["op_index"]] = {
            "verdict": "confirm" if str(vd.get("verdict", "")).strip().lower() == "confirm" else "reject",
            "reason": str(vd.get("reason", "")),
        }
    return {"verdicts": verdicts, "cost_usd": res.get("cost_usd")}


# --- Decision log (spec §17): audit trail AND authorship-shadow training corpus -----------------
SCHEMA_VERSION = 1
PROP_DIR = "propagation"


def _decision_records(ops: list, floor_results: list, judge_verdicts: dict,
                      facts_by_id: dict, active_by_sk: dict) -> list:
    """One record per PROPOSED op — floor-rejected, judge-rejected, or confirmed. Captures the
    full chain (what fired it, the edit, who decided, why, did it commit) so the log is both an
    audit trail of every model-authored prose edit and the judge's training corpus."""
    records = []
    for i, op in enumerate(ops):
        violations = floor_results[i]
        if violations:                                    # floored before the judge ever saw it
            verdict, reason, committed = "floor_reject", "; ".join(violations), False
        else:
            jv = judge_verdicts.get(i) or {"verdict": "reject",
                                           "reason": "no verdict returned (fail-closed)"}
            verdict, reason = jv["verdict"], jv["reason"]
            committed = verdict == "confirm"
        df = op.get("derived_from")
        fact = facts_by_id.get(df) or {}
        tgt = op.get("target_subject_key")
        old = active_by_sk.get(normalize_subject_key(str(tgt))) if tgt else None
        records.append({
            "trigger_claim_id": df,
            "trigger_source_url": fact.get("source_url"),
            "operation": op.get("operation"),
            "section": op.get("section"),
            "zone": op.get("zone"),
            "valence": op.get("valence"),
            "subject_key": op.get("subject_key"),
            "old_text": (old.get("claim") if isinstance(old, dict) else None),
            "new_text": op.get("claim"),                  # null on retire
            "derived_from": df,
            "judge_verdict": verdict,                     # confirm | reject | floor_reject
            "judge_reason": reason,
            "floor_violations": violations,
            "committed": committed,
        })
    return records


def log_decisions(slug: str, records: list, source: str = "monitor") -> list:
    """Persist the propagation decision log to the PRIVATE data store, mirroring shadow.capture's
    non-disruption contract: wrapped so it can only ever WARN, never raise into the live monitor
    path. Returns the records regardless, so a caller or test can inspect them even when no backend
    is configured (the local-FS fallback still writes; an offline test passes persist=False)."""
    try:
        now = datetime.now()
        stamp = now.strftime("%Y%m%dT%H%M%S")
        payload = {"schema_version": SCHEMA_VERSION, "slug": slug, "source": source,
                   "run_ts": now.isoformat(timespec="seconds"), "decisions": records}
        selfserve.write_data(
            f"{PROP_DIR}/{slug}/{stamp}.json",
            json.dumps(payload, indent=2, default=str, ensure_ascii=False),
            f"propagation: decisions {source} {slug} {stamp}",
        )
    except Exception as e:  # NEVER let the decision log break a live run
        print(f"[propagate] decision-log skipped ({type(e).__name__}: {e})", file=sys.stderr)
    return records


def propagate(meta: dict, facts: list[dict], claims: list[dict],
              slug: str = None, source: str = "monitor", persist: bool = True) -> dict:
    """Run the full propagation control flow over already-grounded act-grade facts:
        propose (Sonnet) -> deterministic FLOOR -> judge (Opus, adversarial) -> decision log.
    Returns the proposed ops, their floor results, the judge verdicts, the CONFIRMED ops (floor-
    passed AND judge-confirmed — the only ones an apply step would touch), the decision records, and
    cost. APPLIES NOTHING: this ships in shadow first (capture, don't mutate) — apply + the retire-
    cascade are step 5. `persist=False` skips the decision-log write (offline verification)."""
    surviving_fact_ids = {f.get("id") for f in facts if f.get("id")}
    facts_by_id = {f.get("id"): f for f in facts if f.get("id")}
    active_by_sk = _active_targets(claims)

    proposed = propose(meta, facts, claims)
    ops = proposed["ops"]

    # FLOOR every op first (model-free). Only the survivors cost an Opus judge call.
    floor_results = [floor_check(o, surviving_fact_ids, active_by_sk) for o in ops]
    indexed_survivors = [(i, ops[i]) for i in range(len(ops)) if not floor_results[i]]

    judged = judge(meta, facts, claims, indexed_survivors)
    verdicts = judged["verdicts"]

    records = _decision_records(ops, floor_results, verdicts, facts_by_id, active_by_sk)
    confirmed = [ops[i] for i in range(len(ops))
                 if not floor_results[i] and (verdicts.get(i) or {}).get("verdict") == "confirm"]

    if persist and slug:
        log_decisions(slug, records, source=source)

    return {
        "ops": ops,
        "no_change": proposed["no_change"],
        "floor_results": floor_results,
        "floor_rejected": [ops[i] for i in range(len(ops)) if floor_results[i]],
        "verdicts": verdicts,
        "confirmed": confirmed,
        "decisions": records,
        "cost_usd": {"propose": proposed.get("cost_usd"), "judge": judged.get("cost_usd")},
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


def apply_ops(claims: list[dict], confirmed_ops: list[dict], facts: list[dict],
              slug: str, today: str) -> dict:
    """Apply judge-CONFIRMED ops to a copy of `claims`, deterministically. Returns
    {'claims': new_list, 'applied': [...], 'skipped': [...]}. Each produced/edited claim is
    re-validated; a malformed result is SKIPPED (logged), never written — apply can only ever
    add sound claims or leave the card unchanged."""
    out = [dict(c) for c in claims]                       # shallow-copy each claim; never mutate input
    facts_by_id = {f.get("id"): f for f in facts if f.get("id")}
    applied, skipped = [], []

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
                "verified": True,
                "confidence": "medium",                   # an interpretation, judge-confirmed but not grounded
                "derived_from": df,
            }
            if op.get("persona"):
                new["persona"] = op["persona"]
            errs = validation_errors(new)
            if errs:
                skipped.append({"op": op, "reason": f"invalid add: {errs[:2]}"})
                continue
            out.append(new)
            applied.append({"operation": "add", "id": new["id"], "subject_key": sk})

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
                for k in _OWN_SOURCE_FIELDS:               # re-anchor to the firing fact's provenance
                    tgt.pop(k, None)
            else:  # retire — status flip, keep text + any own source for the lineage view
                tgt["status"] = "retired"
                tgt["retired_on"] = today
                tgt["retired_reason"] = op.get("retired_reason")
                tgt["derived_from"] = df                   # the killing fact
            errs = validation_errors(tgt)
            if errs:                                       # roll back this one claim; never write invalid
                tgt.clear(); tgt.update(before)
                skipped.append({"op": op, "reason": f"invalid {operation}: {errs[:2]}"})
                continue
            applied.append({"operation": operation, "id": tgt["id"],
                            "subject_key": tgt["subject_key"]})

    return {"claims": out, "applied": applied, "skipped": skipped}


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
