"""The screen: one direct Haiku call per card, no tools, bounded input, a fixed output schema.

Why a direct Messages API call and not the agent harness: the harness's preset system prompt costs
more than the screen itself ("Say OK" through it: Haiku $0.02, Sonnet $0.06, Opus $0.11, measured
2026-10-03). The plain system prompt also gives the captured call `exact` replay fidelity, which is
what the on-device lane needs to take the role over for nothing.

Output: candidates in exactly the shape triage emits, so the escalation floor, materiality,
grounding and everything downstream run unchanged. `source_hint` is set by CODE from the finding the
model pointed at (`finding_id`); the model cannot add a URL. At most MAX_CANDIDATES per card.
"""
from __future__ import annotations

import json
import sys
import time

from scout import calllog, config, judgment

ROLE = "screen"
BLOCK = "sensors._SCREEN_SYSTEM"
MAX_CANDIDATES = 5
MAX_FINDINGS = 60
TOOL = {
    "name": "screen",
    "description": "The candidates worth the downstream judge, strongest first; an empty list on a quiet morning.",
    "input_schema": {
        "type": "object",
        "properties": {
            "candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "finding_id": {"type": "string"},
                        "signal": {"type": "string"},
                        "subject_key": {"type": "string"},
                        "about": {"type": "string", "enum": ["competitor", "my_company"]},
                        "valence": {"type": "string", "enum": ["front_foot", "back_foot"]},
                        "substantial": {"type": "boolean"},
                        "why_new": {"type": "string"},
                    },
                    "required": ["finding_id", "signal", "subject_key", "about", "valence", "substantial", "why_new"],
                },
            }
        },
        "required": ["candidates"],
    },
}
# Haiku 4.5 list prices per million tokens (input, output, cache read, cache write); other models
# fall back to the input/output pair only, which under-counts and is marked `approx` in the result.
_PRICES = {"claude-haiku-4-5-20251001": (1.00, 5.00, 0.10, 1.25)}


def system_prompt() -> str | None:
    cats = judgment.optional("monitor.MATERIAL_CATEGORIES")
    return judgment.optional(BLOCK, {"MATERIAL_CATEGORIES": cats or ""})


def cost_of(model: str, usage) -> float | None:
    if usage is None:
        return None
    p = _PRICES.get(model)
    if not p:
        return None
    g = lambda k: float(getattr(usage, k, 0) or (usage.get(k, 0) if isinstance(usage, dict) else 0) or 0)
    return round((g("input_tokens") * p[0] + g("output_tokens") * p[1]
                  + g("cache_read_input_tokens") * p[2] + g("cache_creation_input_tokens") * p[3]) / 1e6, 6)


def _digest(claims: list[dict]) -> str:
    return "\n".join(f"- {c.get('subject_key')} — {str(c.get('claim', ''))[:160]}" for c in claims)


def build_user(meta: dict, claims: list[dict], findings: list[dict], cutoff: str, sig_block: str = "") -> tuple[str, dict]:
    """The user message and the finding index {id: finding}. Findings are capped and trimmed."""
    comp, me = meta.get("competitor"), meta.get("my_company")
    index: dict[str, dict] = {}
    rows = []
    for i, f in enumerate(findings[:MAX_FINDINGS]):
        fid = f"f{i + 1}"
        index[fid] = f
        rows.append({"id": fid, "about": f.get("role") or f.get("entity"), "kind": f.get("kind"),
                     "source": f.get("source_url"), "host": f.get("source_host"), "class": f.get("source_class"),
                     "flag": f.get("flag"), "title": (f.get("title") or "")[:200],
                     "published": f.get("published"), "seen": (f.get("seen_at") or "")[:10],
                     "text": (f.get("text") or "")[:900]})
    user = (f"Competitor: {comp}" + (f"   We are: {me}" if me else "") +
            f"\nCUTOFF: {cutoff}. Surface ONLY developments dated on/after it that are NOT already reflected "
            f"in the tracked claims.\n\nTRACKED SUBJECTS (subject_key — current value already known):\n" + _digest(claims) +
            (("\n" + sig_block) if sig_block else "") +
            "\n\nFINDINGS (everything new that code read this morning; `about` says whose source it is):\n"
            + json.dumps(rows, ensure_ascii=False, indent=1))
    return user, index


def validate(out: dict | None, index: dict) -> list[dict]:
    """Model output -> triage-shaped candidates. Drops anything that points at a finding id that does
    not exist (the model cannot invent a source) and anything past MAX_CANDIDATES."""
    cands = []
    for c in ((out or {}).get("candidates") or []):
        if len(cands) >= MAX_CANDIDATES:
            break
        if not isinstance(c, dict):
            continue
        f = index.get(str(c.get("finding_id") or ""))
        if not f or not f.get("source_url"):
            continue
        about = str(c.get("about") or f.get("role") or "competitor")
        cands.append({
            "signal": str(c.get("signal") or f.get("title") or "")[:300],
            "subject_key": str(c.get("subject_key") or "NEW").strip() or "NEW",
            "about": about if about in ("competitor", "my_company") else "competitor",
            "valence": c.get("valence") if c.get("valence") in ("front_foot", "back_foot") else "back_foot",
            "substantial": c.get("substantial") is True,
            "why_new": str(c.get("why_new") or "")[:400],
            "source_hint": f["source_url"],
            "source_class": f.get("source_class"),
            "finding_id": f.get("fingerprint"),
            "sensor_kind": f.get("kind"),
            # EVIDENCE IN HAND (Part 3, gate mode): the page text code read, for page findings only. A
            # feed summary or a news snippet is not the page and would ground "absent", so it is left out.
            **({"evidence": (f.get("text") or "")[:1500]} if f.get("kind") in ("page_change", "value_change", "redesign") and f.get("text") else {}),
        })
    return cands


def run(meta: dict, claims: list[dict], findings: list[dict], cutoff: str, sig_block: str = "", client=None) -> dict:
    """{"candidates", "cost_usd", "status": ok|skipped|failed, "detail"}; never raises."""
    system = system_prompt()
    if not system:
        return {"candidates": [], "cost_usd": 0.0, "status": "skipped", "detail": f"pack block {BLOCK} missing"}
    if not findings:
        return {"candidates": [], "cost_usd": 0.0, "status": "skipped", "detail": "no open findings"}
    user, index = build_user(meta, claims, findings, cutoff, sig_block)
    model = config.FAST_MODEL
    t0 = time.monotonic()
    try:
        judgment.require()
        judgment.assert_clean(system, user)
        if client is None:
            import anthropic
            client = anthropic.Anthropic()
        msg = client.messages.create(model=model, max_tokens=1500, system=system, tools=[TOOL],
                                     tool_choice={"type": "tool", "name": "screen"},
                                     messages=[{"role": "user", "content": user}])
        out = None
        for block in msg.content:
            if getattr(block, "type", "") == "tool_use" and getattr(block, "name", "") == "screen":
                out = block.input
        usage = getattr(msg, "usage", None)
        cost = cost_of(model, usage)
        calllog.record_direct(role=ROLE, model=model, system=system, user=user, tools=[TOOL],
                              tool_choice={"type": "tool", "name": "screen"},
                              result_text=json.dumps(out) if out is not None else "", usage=usage,
                              duration_ms=int((time.monotonic() - t0) * 1000))
        try:
            from scout import generate
            generate._merge_role_totals({ROLE: {
                "input": int(getattr(usage, "input_tokens", 0) or 0), "output": int(getattr(usage, "output_tokens", 0) or 0),
                "cache_read": int(getattr(usage, "cache_read_input_tokens", 0) or 0),
                "cache_creation": int(getattr(usage, "cache_creation_input_tokens", 0) or 0), "messages": 1}})
        except Exception:
            pass
        cands = validate(out, index)
        return {"candidates": cands, "cost_usd": cost or 0.0, "status": "ok",
                "detail": f"{len(cands)} candidate(s), {sum(1 for c in cands if c['substantial'])} substantial, from {len(index)} finding(s)"}
    except Exception as e:
        print(f"[sensors] screen failed ({type(e).__name__}: {e})", file=sys.stderr)
        try:
            calllog.record_direct(role=ROLE, model=model, system=system, user=user, tools=[TOOL],
                                  tool_choice={"type": "tool", "name": "screen"}, result_text=None,
                                  status="failed", error=f"{type(e).__name__}: {e}",
                                  duration_ms=int((time.monotonic() - t0) * 1000))
        except Exception:
            pass
        return {"candidates": [], "cost_usd": 0.0, "status": "failed", "detail": f"{type(e).__name__}: {e}"}
