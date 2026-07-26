"""No-drop guarantee for confirmed material updates (decision-log §11).

A material update that a judge CONFIRMED must always reach the card — it can NEVER go to the Cut Log
and can NEVER be silently dropped. The Cut Log is only for claims verification could not CONFIRM; a
confirmed claim that merely fails the render-structure format is a different thing. So when a confirmed
claim/op fails the render gate (scout.schema.render_structure_errors), this module does ONE of two
things, never a third:

  1. REPAIR — a tools-off model re-asks to add the required **So what:** / **Soundbite:** block,
     SUBSTANCE UNCHANGED (bounded retries). If the result validates, the update publishes.
  2. HOLD — if repair can't produce a valid format, the update is HELD in pending_publish/<slug>/ in
     the private store and FLAGGED to the human (and, later, a model-judge reformatter). It stays
     pending until it lands on the card (a human edit or a model reformat). NEVER cut, never dropped.

`repair_or_hold` is deterministic orchestration (testable with an injected reformatter); the model
call lives in `reformat_claim`.
"""
import asyncio
import json
import sys
from datetime import datetime

from scout import config, schema, selfserve

PENDING_DIR = "pending_publish"

_REFORMAT_SYSTEM = (
    "You reformat a competitive-battlecard claim so it renders correctly, WITHOUT changing its "
    "substance. The claim has one or both of these render-contract problems:\n"
    "- MISSING BLOCK: an objection_handling OR executive_summary claim must end with a block "
    "beginning literally '**So what:**' that states the move/decision the claim implies (pull it "
    "from the claim's closing sentences into that block); a where_we_win / where_they_win "
    "battlecard play must end with a block beginning literally '**Soundbite:**' giving one "
    "rep-ready sentence (derive it from the claim's own headline/body).\n"
    "- OVER THE WORD CAP: the claim buries its answer in accreted history. CONDENSE it: lead with "
    "the CURRENT state; keep every number, date, name, and source-anchored fact that is STILL true; "
    "collapse resolved intermediate beats (an on-off saga, superseded interim rulings) into at most "
    "one sentence of arc; keep the bold headline/question and the required block. NEVER invent "
    "anything, never drop a still-true current fact — cut only redundancy and resolved history.\n"
    "Return ONLY JSON: {\"claim\": \"<the reformatted claim text, including the bold "
    "headline/question and the required block>\"}."
)


async def _run_reformat(claim_text: str, section: str, zone, errors: list | None = None) -> dict:
    from claude_agent_sdk import ClaudeAgentOptions
    from scout.generate import _drive
    from scout.prompts import WRITING_STYLE            # 2026-07-25: a live condense minted an em
    # dash — every prose-writing call must carry the style contract, the reformatter included.
    errs = "\n".join(f"- {e}" for e in (errors or [])) or "(recompute from the contract above)"
    user = (f"section={section} zone={zone}\n\nRENDER-CONTRACT VIOLATIONS (the deterministic gate's "
            f"exact findings — cure these and nothing else):\n{errs}\n\n"
            f"CLAIM TO REFORMAT (add the missing required block and/or CONDENSE under the cap, "
            f"substance unchanged):\n\n{claim_text}")
    options = ClaudeAgentOptions(
        model=config.CHALLENGER_MODEL,                 # Sonnet — reliable, cheap for one claim
        system_prompt=_REFORMAT_SYSTEM + "\n\n" + WRITING_STYLE,
        mcp_servers={}, allowed_tools=[], disallowed_tools=["WebSearch", "WebFetch"],
        permission_mode="bypassPermissions",
        max_turns=2, max_budget_usd=0.25,
    )
    return await _drive(user, options, "reformat")


def reformat_claim(claim_text: str, section: str, zone=None, tries: int = 2,
                   errors: list | None = None, cost: dict | None = None) -> str | None:
    """Re-ask a tools-off model to cure the render-contract violations (missing block and/or
    over-cap condense), substance unchanged. Returns the reformatted text ONLY if it passes the
    render-structure gate, else None (after `tries`). `errors` = the gate's exact findings (fed to
    the model verbatim); `cost` = optional accumulator dict (key 'reformat')."""
    from scout.generate import _extract_json
    for _ in range(max(1, tries)):
        try:
            res = asyncio.run(_run_reformat(claim_text, section, zone, errors=errors))
            if cost is not None:
                cost["reformat"] = cost.get("reformat", 0.0) + (res.get("cost_usd") or 0.0)
            new = _extract_json(res["text"]).get("claim")
        except Exception as e:
            print(f"[reformat] attempt failed ({type(e).__name__}: {e})", file=sys.stderr)
            continue
        if isinstance(new, str) and new.strip() and not schema.render_structure_errors(
                {"section": section, "zone": zone, "claim": new}):
            return new
    return None


# --- Condense fidelity judge (2026-07-25, "re-judge everywhere") --------------------------------
# A condense CHANGES prose that a judge confirmed, so it must be re-verified before it can publish
# — at EVERY seam, including this gate/approve/apply backstop (owner decision 7/25: no unverified
# alteration ever publishes). The lens here is FIDELITY, not grounding: grounding was already
# certified on the original text, so the only new risk is the condense dropping a still-true fact
# or inventing one. The ORIGINAL text is therefore the ground truth. Fail-closed: no clean confirm
# -> the op is HELD (never published), mirroring the pipeline judge's posture.

_CONDENSE_VERIFY_SYSTEM = (
    "You are a fidelity judge. A competitive-battlecard claim that was ALREADY verified for factual "
    "grounding has been CONDENSED to fit a word cap. Compare the CONDENSED text against the ORIGINAL "
    "(the ground truth) and confirm ONLY if all hold:\n"
    "- every number, date, name, and source-anchored fact still true in the original survives in the "
    "condensed text (compressing resolved history into a one-line arc is CORRECT, deleting a "
    "still-true current fact is not);\n"
    "- nothing was invented: no number, mechanism, entity, or causal reason appears in the condensed "
    "text that the original does not contain;\n"
    "- the bold headline/question and any required '**So what:**' / '**Soundbite:**' block survive;\n"
    "- the meaning and competitive direction of the claim are unchanged.\n"
    "DEFAULT TO REJECT when not convinced. Return ONLY JSON: "
    '{"verdict": "confirm|reject", "reason": "<one line>"}'
)


async def _run_condense_verify(original: str, condensed: str, section: str, zone, model: str) -> dict:
    from claude_agent_sdk import ClaudeAgentOptions
    from scout.generate import _drive
    user = (f"section={section} zone={zone}\n\nORIGINAL (ground truth):\n\n{original}\n\n"
            f"CONDENSED (verify against the original):\n\n{condensed}")
    options = ClaudeAgentOptions(
        model=model,                                   # judge tier: Opus, fallback per config
        system_prompt=_CONDENSE_VERIFY_SYSTEM,
        mcp_servers={}, allowed_tools=[], disallowed_tools=["WebSearch", "WebFetch"],
        permission_mode="bypassPermissions",
        max_turns=config.JUDGE_MAX_TURNS, max_budget_usd=config.JUDGE_MAX_BUDGET_USD,
    )
    return await _drive(user, options, "gate_judge")


def verify_condense(original: str, condensed: str, section: str = None, zone=None,
                    cost: dict | None = None) -> tuple[bool, str]:
    """Machine re-verification of a condense: (True, reason) only on a clean judge confirm.
    Retries once on the primary judge model, then once on JUDGE_FALLBACK_MODEL (the 7/01-outage
    pattern); anything else fails CLOSED to (False, ...) so the caller holds, never publishes."""
    from scout.generate import _extract_json
    plan = [config.ORCHESTRATOR_MODEL, config.ORCHESTRATOR_MODEL]
    if config.JUDGE_FALLBACK_MODEL:
        plan.append(config.JUDGE_FALLBACK_MODEL)
    for model in plan:
        try:
            res = asyncio.run(_run_condense_verify(original, condensed, section, zone, model))
            if cost is not None:
                cost["gate_judge"] = cost.get("gate_judge", 0.0) + (res.get("cost_usd") or 0.0)
            data = _extract_json(res["text"])
        except Exception as e:
            print(f"[reformat] condense verify attempt failed ({type(e).__name__}: {e})",
                  file=sys.stderr)
            continue
        verdict = str(data.get("verdict", "")).strip().lower()
        if verdict in ("confirm", "reject"):
            return (verdict == "confirm", str(data.get("reason", "")))
    return (False, "fidelity judge returned no parseable verdict after retries and the fallback "
                   "model — fail-closed, the condense is not trusted")


_PERSONA_TOOL = {
    "name": "assign_persona",
    "description": "Return the single best-fit buyer persona for this play/objection.",
    "input_schema": {"type": "object", "properties": {"persona": {"type": "string"}},
                     "required": ["persona"]},
}


def classify_persona(claim_text: str, section: str, zone=None) -> str | None:
    """Pick the single buyer persona a play is aimed at / that raises an objection (the 'Raised by' /
    'Best for' badge). A field, not prose — so the no-drop repair CLASSIFIES it (cheap Haiku) rather
    than reformatting text. Returns a valid persona from schema.PERSONAS, or None."""
    import anthropic
    from scout import schema as _schema
    personas = list(_schema.PERSONAS)
    sys_prompt = ("Tag a competitive-battlecard play or objection with the SINGLE buyer persona it is "
                  "primarily aimed at (or that tends to raise the objection). Choose exactly one of: "
                  + ", ".join(personas) + ". Use the assign_persona tool.")
    try:
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model=config.FAST_MODEL, max_tokens=200,
            system=sys_prompt, tools=[_PERSONA_TOOL],
            tool_choice={"type": "tool", "name": "assign_persona"},
            messages=[{"role": "user", "content": f"section={section} zone={zone}\n\n{claim_text}"}],
        )
        for block in msg.content:
            if getattr(block, "type", "") == "tool_use" and block.name == "assign_persona":
                pid = block.input.get("persona")
                return pid if pid in personas else None
    except Exception as e:
        print(f"[reformat] persona classify failed ({type(e).__name__}: {e})", file=sys.stderr)
    return None


def hold(slug: str, item: dict, reason: str, *, alert: bool = True) -> str:
    """Durably HOLD a confirmed-material update that could not be auto-formatted, and flag it. This is
    NOT the Cut Log: the update is pending publication, owed to the card, awaiting a human edit or a
    model-judge reformat. Returns the store path."""
    now = datetime.now()
    rec = {"slug": slug, "held_at": now.isoformat(timespec="seconds"), "reason": reason,
           "status": "pending_publish", "item": item}
    path = f"{PENDING_DIR}/{slug}/{now.strftime('%Y%m%dT%H%M%S')}.json"
    try:
        selfserve.write_data(path, json.dumps(rec, indent=2, ensure_ascii=False, default=str),
                             f"pending-publish: HELD (needs format) {slug}")
    except Exception as e:
        # Even if the store write fails, we must be LOUD — never let a held update vanish silently.
        print(f"[reformat] HOLD store-write failed ({type(e).__name__}: {e}); item NOT lost: "
              f"{json.dumps(item, default=str)[:500]}", file=sys.stderr)
    if alert:
        _alert_human(slug, item, reason)
    return path


def _alert_human(slug: str, item: dict, reason: str) -> None:
    """Best-effort email/log flag for a held update (the human is the formatter of last resort)."""
    msg = (f"[Scout] A confirmed material update for {slug} is HELD pending publication — it could not "
           f"be auto-formatted ({reason}). It is NOT cut; it is owed to the card. Reformat + publish "
           f"it (or re-run the model reformatter):\n\n{json.dumps(item, indent=2, default=str)[:1500]}")
    try:
        from scout import notify
        # _dispatch is a no-op without email creds (dev/test), and emails the owner when configured.
        notify._dispatch("Scout: material update HELD pending publish", msg, dry_run=False)
    except Exception as e:
        print(f"[reformat] alert skipped ({type(e).__name__}: {e})\n{msg}", file=sys.stderr)


def repair_or_hold(slug: str, claim: dict, *, reformatter=reformat_claim,
                   persona_classifier=classify_persona, condense_verifier=verify_condense,
                   alert: bool = True, cost: dict | None = None) -> tuple[str, dict]:
    """The no-drop decision for ONE confirmed claim. Returns (status, claim) where status is:
      'ok'       — already well-formed, publish as-is;
      'repaired' — auto-fixed (a model added the missing So-what/Soundbite block, classified the
                   missing persona, and/or CONDENSED an over-cap body), substance unchanged and —
                   when a condense actually changed the prose — RE-VERIFIED by the fidelity judge
                   before it may publish (2026-07-25, "re-judge everywhere");
      'held'     — could not auto-fix, or a condense failed re-verification; HELD + flagged to the
                   human, NEVER cut/dropped.
    There is no fourth outcome: a confirmed update is published or held, never lost. `reformatter` /
    `persona_classifier` / `condense_verifier` are injectable for tests. `alert=False` skips the
    standalone hold email — the pre-email render gate uses it because the proposals email itself
    carries the callout. `cost` = optional accumulator ('reformat' / 'gate_judge' keys)."""
    errs = schema.render_structure_errors(claim)
    if not errs:
        return ("ok", claim)
    fixed = dict(claim)
    original_text = fixed.get("claim") or ""
    # missing persona (the "Raised by"/"Best for" badge) is a FIELD, not prose -> classify + assign.
    if any("persona" in e for e in errs) and not fixed.get("persona"):
        pid = persona_classifier(fixed.get("claim") or "", fixed.get("section"), fixed.get("zone"))
        if pid:
            fixed["persona"] = pid
    # PROSE defects -> reformat: a missing So-what/Soundbite BLOCK, and/or an over-cap body (the
    # 7/25 hold: a length-only violation used to get NO repair attempt and went straight to hold).
    had_cap = bool(schema.word_cap_errors(fixed))
    if any(("So what" in e or "Soundbite" in e) for e in errs) or had_cap:
        new_text = reformatter(fixed.get("claim") or "", fixed.get("section"), fixed.get("zone"),
                               errors=[e for e in errs if "persona" not in e], cost=cost)
        if new_text:
            fixed["claim"] = new_text
    residual = schema.render_structure_errors(fixed)       # re-validate — never publish malformed
    if not residual:
        # A condense ALTERED judge-confirmed prose -> it must pass the fidelity judge before it can
        # publish. Missing-block-only repairs keep today's path (the reformat contract restructures
        # without touching substance and never triggered on length).
        if had_cap and fixed.get("claim") != original_text:
            ok, reason = condense_verifier(original_text, fixed.get("claim") or "",
                                           fixed.get("section"), fixed.get("zone"), cost=cost)
            if not ok:
                hold(slug, fixed, f"condense failed re-verification: {reason}", alert=alert)
                return ("held", fixed)
        return ("repaired", fixed)
    # Carry the EXACT violations into the hold reason (e.g. "201 words exceeds the 170-word render
    # cap") — the proposals email shows this verbatim so the human knows what to cure.
    hold(slug, fixed, "render-structure repair exhausted: " + "; ".join(residual), alert=alert)
    return ("held", fixed)
