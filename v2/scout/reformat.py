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
    "substance. The claim is missing a required structural block:\n"
    "- an objection_handling claim must end with a block beginning literally '**So what:**' that "
    "states the rep's concrete move (pull the move already implied by the claim's closing sentences "
    "into that block);\n"
    "- a where_we_win / where_they_win battlecard play must end with a block beginning literally "
    "'**Soundbite:**' giving one rep-ready sentence (derive it from the claim's own headline/body).\n"
    "Keep ALL facts, numbers, and wording otherwise intact — only restructure into the required shape; "
    "add nothing not already implied by the text. Return ONLY JSON: {\"claim\": \"<the reformatted "
    "claim text, including the bold headline/question and the required block>\"}."
)


async def _run_reformat(claim_text: str, section: str, zone) -> dict:
    from claude_agent_sdk import ClaudeAgentOptions
    from scout.generate import _drive
    user = (f"section={section} zone={zone}\n\nCLAIM TO REFORMAT (add the missing required block, "
            f"substance unchanged):\n\n{claim_text}")
    options = ClaudeAgentOptions(
        model=config.CHALLENGER_MODEL,                 # Sonnet — reliable, cheap for one claim
        system_prompt=_REFORMAT_SYSTEM,
        mcp_servers={}, allowed_tools=[], disallowed_tools=["WebSearch", "WebFetch"],
        permission_mode="bypassPermissions",
        max_turns=2, max_budget_usd=0.25,
    )
    return await _drive(user, options, "reformat")


def reformat_claim(claim_text: str, section: str, zone=None, tries: int = 2) -> str | None:
    """Re-ask a tools-off model to add the missing render block, substance unchanged. Returns the
    reformatted text ONLY if it passes the render-structure gate, else None (after `tries`)."""
    from scout.generate import _extract_json
    for _ in range(max(1, tries)):
        try:
            res = asyncio.run(_run_reformat(claim_text, section, zone))
            new = _extract_json(res["text"]).get("claim")
        except Exception as e:
            print(f"[reformat] attempt failed ({type(e).__name__}: {e})", file=sys.stderr)
            continue
        if isinstance(new, str) and new.strip() and not schema.render_structure_errors(
                {"section": section, "zone": zone, "claim": new}):
            return new
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


def repair_or_hold(slug: str, claim: dict, *, reformatter=reformat_claim) -> tuple[str, dict]:
    """The no-drop decision for ONE confirmed claim. Returns (status, claim) where status is:
      'ok'       — already well-formed, publish as-is;
      'repaired' — a model added the missing block, substance unchanged, publish the repaired claim;
      'held'     — could not auto-format; HELD + flagged to the human, NEVER cut/dropped.
    There is no fourth outcome: a confirmed update is published or held, never lost. `reformatter` is
    injectable for tests."""
    if not schema.render_structure_errors(claim):
        return ("ok", claim)
    new_text = reformatter(claim.get("claim") or "", claim.get("section"), claim.get("zone"))
    if new_text:
        repaired = {**claim, "claim": new_text}
        if not schema.render_structure_errors(repaired):   # re-validate — never publish malformed
            return ("repaired", repaired)
    hold(slug, claim, "render-structure repair exhausted")
    return ("held", claim)
