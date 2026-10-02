"""The arbiter: a research layer on top of the evals (Uroš, 2026-10-01).

For every disagreement between the live judge and the local models (and, later, any judged role),
a stronger, DIFFERENT model settles the truth from the facts the proposer had (web search allowed,
capped, only when the facts cannot settle a point), then grades the verdict-and-reason texts of
every arm, anonymised as A/B/C/D, on who reasoned best for the tool's goal (accurate, deal-moving,
inside the judge's rules) and why the others failed, in a FIXED failure vocabulary so causes can be
counted per model over time. Uroš reads three things per item (content, resolution, diagnosis) and
ratifies or overrules; ratified verdicts become labels, unread ones stay a separate line.

Records: research/arbiter/<YYYY-MM>/<delta_id>.json in the private store. Model: Opus 5.5
(claude-opus-5-5), a different and stronger model than the live judge (Opus 4.8), so it never
grades itself. Spend: per-call cap + a daily ledger (research/arbiter_state.json).
"""
from __future__ import annotations

import json
import os
import random
import re
import time
from datetime import datetime

from scout import adjudicate_models, config, modelcompare, rolespecs, selfserve

ARBITER_MODEL = os.environ.get("SCOUT_ARBITER_MODEL", "claude-opus-5-5")
ARBITER_DIR = "research/arbiter"
ARBITER_STATE = "research/arbiter_state.json"
ARBITER_MAX_USD = float(os.environ.get("SCOUT_ARBITER_MAX_USD", "0.75"))        # per item, hard stop
ARBITER_DAILY_USD = float(os.environ.get("SCOUT_ARBITER_DAILY_USD", "5"))
ARBITER_DAILY_ITEMS = int(os.environ.get("SCOUT_ARBITER_DAILY_ITEMS", "20"))
ARBITER_SEARCHES = int(os.environ.get("SCOUT_ARBITER_SEARCHES", "3"))
PRICE = {"claude-opus-5-5": (4.0, 20.0), "claude-fable-5-1": (10.0, 50.0), "claude-opus-5": (5.0, 25.0)}

# The failure vocabulary: fixed so causes are countable per model across months. Add, never rename.
FAILURE_MODES = {
    "overreach": "asserted something the facts do not say",
    "invented_contrast": "contrasted with our company without a fact for our side",
    "missed_materiality": "confirmed an edit that moves no deal, or rejected one that does",
    "wrong_section": "the edit belongs in a different section or zone",
    "misread_rule": "applied one of the judge's rules wrongly",
    "right_for_wrong_reason": "reached the right verdict with reasoning that does not support it",
    "ignored_fact": "overlooked a fact that decides the case",
    "fidelity_loss": "the edit drops or distorts a number, date or name the facts carry",
}

_SYSTEM = """You are the arbiter of a competitive-intelligence tool's evaluation: a research layer, not a product step.
The tool keeps sales battlecards whose every claim must be grounded in verified facts. A PROPOSER drafts edits to the
card from a list of GROUNDED FACTS; a JUDGE confirms or rejects each edit. The judge's rules: facts only (an edit may
not say anything the facts do not say); no invented contrast with our company; the edit must sit in the right section;
an edit must be deal-moving to be worth confirming; reject when not convinced.

You receive: the facts the proposer had, the text currently on the card, one proposed edit, and the verdicts with
reasons that several models gave (labelled A, B, C, ... in random order; you are not told which is which). Your job:
1. Settle the TRUTH: should this edit be confirmed or rejected under the rules, given the facts? Decide from the facts.
   Use web search only if a point cannot be settled from the facts and the decision turns on it (at most a few
   searches); say what you searched and what you found. Name the decisive fact(s) by id.
2. Grade each model's reasoning against the tool's goal (100% accuracy, deal-moving judgment, inside the rules):
   which reasoned most correctly, and for each model whose verdict or reasoning fails, why, using ONLY these failure
   modes: """ + ", ".join(f"{k} ({v})" for k, v in FAILURE_MODES.items()) + """.
   A model can have the right verdict and still fail (right_for_wrong_reason). A model that is right needs no mode.
3. Write for a product manager who will read only your output: plain full sentences, no hedging, no bullet fragments.
   The resolution is at most 120 words: the ruling, the decisive fact(s), the one thing that settles it.
   If no model reasoned correctly, say so: "best" is "none".

Return ONLY a JSON object:
{"verdict": "confirm"|"reject", "decisive_facts": ["fact id", ...], "resolution": "at most 120 words: the ruling and why",
 "searched": "what you searched and found, or an empty string",
 "best": "A"|"B"|...|"none", "best_why": "one or two sentences",
 "grades": {"A": {"right": true|false, "modes": ["overreach", ...], "diagnosis": "one or two sentences"}, ...},
 "confidence": 0.0-1.0}"""


# --- context assembly (from the captured judge call + the arms' results) ----------------------------
def _between(text: str, start: str, end: str | None) -> str:
    i = text.find(start)
    if i < 0:
        return ""
    i += len(start)
    j = text.find(end, i) if end else -1
    return text[i:j if j >= 0 else None]


def judge_context(record: dict, item_id: str) -> dict:
    """facts / current claims / the op, parsed out of the captured judge prompt (propagate.judge)."""
    user = record.get("user") or ""
    def _json(s):
        try:
            return json.loads(s)
        except Exception:
            return []
    facts = _json(_between(user, "judge every op strictly against these):\n", "\n\nCURRENT ACTIVE PLAYS"))
    current = _json(_between(user, "enough to catch a duplicate add):\n", "\n\nPROPOSED OPS TO JUDGE"))
    ops = _json(_between(user, "PROPOSED OPS TO JUDGE (confirm or reject each by op_index):\n", None))
    op = next((o for o in ops if str(o.get("op_index")) == str(item_id)), {}) if isinstance(ops, list) else {}
    target = op.get("target_subject_key") or op.get("subject_key")
    old = next((c for c in current if isinstance(c, dict) and c.get("subject_key") == target), None) if isinstance(current, list) else None
    return {"facts": facts if isinstance(facts, list) else [], "current": old, "op": op,
            "competitor": _between(user, "Competitor: ", "\n").split("   We are:")[0].strip(),
            "my_company": _between(user, "We are: ", "\n").strip()}


def _verdict_from(text: str, item_id: str) -> tuple[str | None, str]:
    try:
        i = text.index("{")
        for v in json.loads(text[i:text.rindex("}") + 1]).get("verdicts") or []:
            if str(v.get("op_index")) == str(item_id):
                return v.get("verdict"), v.get("reason") or ""
    except Exception:
        pass
    return None, ""


def arm_texts(row: dict, record: dict, results: list) -> list[dict]:
    """Every side's verdict + reason on this op: the live judge (from the capture) and each local arm
    (from its result). Identity kept here, hidden from the arbiter by `anonymise`."""
    out = []
    v, r = _verdict_from((record.get("result") or {}).get("text") or "", row["item_id"])
    out.append({"who": "live_judge", "model": record.get("model"), "verdict": v, "reason": r})
    for res in results:
        if res.get("call_id") == row["call_id"] and res.get("mode", "exact") == "exact" and not res.get("rep") and res.get("status") == "ok":
            v, r = _verdict_from(res.get("text") or "", row["item_id"])
            out.append({"who": res.get("backend"), "model": res.get("backend_model"), "verdict": v, "reason": r})
    return [a for a in out if a["verdict"]]


def anonymise(arms: list[dict], seed: str) -> tuple[list[dict], dict]:
    """Shuffle deterministically per item and label A, B, C...; returns (labelled, label -> who)."""
    order = list(range(len(arms)))
    random.Random(seed).shuffle(order)
    labelled, key = [], {}
    for n, i in enumerate(order):
        label = chr(ord("A") + n)
        labelled.append({"label": label, "verdict": arms[i]["verdict"], "reason": arms[i]["reason"]})
        key[label] = arms[i]["who"]
    return labelled, key


def _prompt(ctx: dict, labelled: list[dict]) -> str:
    facts = [{"id": f.get("id"), "claim": f.get("claim"), "source": f.get("source_url"), "as_of": f.get("as_of")}
             for f in ctx["facts"] if isinstance(f, dict)]
    op = ctx["op"]
    parts = [f"Competitor: {ctx['competitor']}   We are: {ctx['my_company']}",
             "\nGROUNDED FACTS (already verified true; the only admissible evidence):",
             json.dumps(facts, ensure_ascii=False, indent=1),
             "\nTEXT ON THE CARD NOW (the target of a revise/retire; absent for an add):",
             json.dumps({"subject_key": (ctx["current"] or {}).get("subject_key"), "text": (ctx["current"] or {}).get("claim")}, ensure_ascii=False, indent=1),
             "\nTHE PROPOSED EDIT:",
             json.dumps({k: op.get(k) for k in ("operation", "section", "zone", "valence", "change_kind", "subject_key",
                                                 "target_subject_key", "derived_from", "claim", "rationale", "retired_reason")},
                        ensure_ascii=False, indent=1),
             "\nTHE MODELS' VERDICTS AND REASONS (anonymised, random order):"]
    for a in labelled:
        parts.append(f"[{a['label']}] verdict: {a['verdict']}\n reason: {a['reason']}")
    return "\n".join(parts)


# --- the call -----------------------------------------------------------------------------------------
def _usd(usage, model: str) -> float:
    pin, pout = PRICE.get(model, (5.0, 25.0))
    i = getattr(usage, "input_tokens", 0) or 0
    o = getattr(usage, "output_tokens", 0) or 0
    cr = getattr(usage, "cache_read_input_tokens", 0) or 0
    cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
    return round((i * pin + cr * pin * 0.1 + cw * pin * 1.25 + o * pout) / 1e6, 4)


def _parse(text: str) -> dict:
    i, j = text.find("{"), text.rfind("}")
    return json.loads(text[i:j + 1])


def call_arbiter(prompt: str, *, searches: int = ARBITER_SEARCHES, model: str = ARBITER_MODEL) -> dict:
    """One arbitration: Opus 5.5, adaptive thinking (the default on 5.5), effort high, web search
    capped. Loops on pause_turn. Returns {parsed, text, cost_usd, duration_ms, searches_used}."""
    import anthropic
    client = anthropic.Anthropic()
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": searches}] if searches > 0 else []
    messages = [{"role": "user", "content": prompt}]
    t0 = time.monotonic()
    cost, text, used = 0.0, "", 0
    for _ in range(4):
        with client.messages.stream(model=model, max_tokens=16000, system=_SYSTEM, messages=messages, tools=tools,
                                    output_config={"effort": "high"}) as stream:
            msg = stream.get_final_message()
        cost += _usd(getattr(msg, "usage", None), model)
        su = getattr(getattr(msg, "usage", None), "server_tool_use", None)
        used = getattr(su, "web_search_requests", used) if su else used
        text = "".join(getattr(b, "text", "") for b in msg.content if getattr(b, "type", "") == "text")
        if msg.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": msg.content}); continue
        break
    if cost > ARBITER_MAX_USD:
        raise RuntimeError(f"arbiter call cost ${cost:.2f} > cap ${ARBITER_MAX_USD}")
    return {"parsed": _parse(text), "text": text, "cost_usd": round(cost, 4),
            "duration_ms": int((time.monotonic() - t0) * 1000), "searches_used": used, "model": model}


# --- records -----------------------------------------------------------------------------------------
def record_path(delta_id: str, run_ts: str | None) -> str:
    month = str(run_ts or datetime.now().isoformat())[:7]
    return f"{ARBITER_DIR}/{month}/{delta_id}.json"


def existing(month: str) -> set:
    return {n[:-5] for n in (selfserve.list_data(f"{ARBITER_DIR}/{month}") or []) if n.endswith(".json")}


def arbitrate(row: dict, record: dict, results: list, *, searches: int = ARBITER_SEARCHES, persist: bool = True) -> dict:
    """Arbitrate one pending disagreement row (adjudicate_models.pending() shape)."""
    ctx = judge_context(record, row["item_id"])
    arms = arm_texts(row, record, results)
    labelled, key = anonymise(arms, row["delta_id"])
    out = call_arbiter(_prompt(ctx, labelled), searches=searches)
    p = out["parsed"]
    grades = {}
    for label, g in (p.get("grades") or {}).items():
        who = key.get(label)
        if who:
            grades[who] = {"verdict": next((a["verdict"] for a in labelled if a["label"] == label), None),
                           "right": bool(g.get("right")), "modes": [m for m in (g.get("modes") or []) if m in FAILURE_MODES],
                           "diagnosis": g.get("diagnosis") or ""}
    rec = {
        "schema_version": 1, "delta_id": row["delta_id"], "call_id": row["call_id"], "role": row["role"],
        "slug": row.get("slug"), "item_id": row["item_id"], "run_ts": record.get("run_ts"),
        "arbitrated_at": datetime.now().isoformat(timespec="seconds"), "arbiter_model": out["model"],
        "verdict": p.get("verdict"), "decisive_facts": p.get("decisive_facts") or [], "resolution": p.get("resolution") or "",
        "searched": p.get("searched") or "", "searches_used": out["searches_used"], "confidence": p.get("confidence"),
        "best": key.get(p.get("best")) if p.get("best") not in (None, "none") else None,
        "best_why": p.get("best_why") or "", "grades": grades,
        "prompt_chars": len(record.get("user") or "") + len(((record.get("system") or {}).get("text") or "")),
        "arms": [{"who": a["who"], "verdict": a["verdict"]} for a in arms],
        "content": {"op": {k: ctx["op"].get(k) for k in ("operation", "section", "zone", "subject_key", "claim", "derived_from")},
                    "current": (ctx["current"] or {}).get("claim"),
                    "facts": [{"id": f.get("id"), "claim": f.get("claim")} for f in ctx["facts"]
                              if isinstance(f, dict) and f.get("id") in (p.get("decisive_facts") or [])]},
        "cost_usd": out["cost_usd"], "duration_ms": out["duration_ms"],
        "review": {"status": "pending", "by": None, "at": None, "truth": None, "note": None},
    }
    if persist:
        selfserve.write_data(record_path(row["delta_id"], record.get("run_ts")),
                             json.dumps(rec, indent=1, ensure_ascii=False), f"arbiter: {row['delta_id']} {rec['verdict']}")
    return rec


def load_records() -> list[dict]:
    out = []
    for month in (selfserve.list_data(ARBITER_DIR, include_dirs=True) or []):
        if "." in month:
            continue
        for fn in (selfserve.list_data(f"{ARBITER_DIR}/{month}") or []):
            if fn.endswith(".json"):
                raw = selfserve.read_data(f"{ARBITER_DIR}/{month}/{fn}")
                if raw:
                    try:
                        out.append(json.loads(raw))
                    except json.JSONDecodeError:
                        pass
    return out


def ratify(rec: dict, truth: str | None, note: str = "", by: str = "uros") -> dict:
    """Uroš's read: truth None = agree with the arbiter; a truth = overrule. Either way the human
    label is written through adjudicate_models.label (the lane's ground truth) and the record keeps
    the review. Ratified = feeds precision; pending = reported separately."""
    final = truth or rec["verdict"]
    status = "agreed" if not truth or truth == rec["verdict"] else "overruled"
    label = adjudicate_models.label(rec["delta_id"], final, note or f"{status} the arbiter ({rec['arbiter_model']})")
    rec["review"] = {"status": status, "by": by, "at": datetime.now().isoformat(timespec="seconds"), "truth": final, "note": note}
    selfserve.write_data(record_path(rec["delta_id"], rec.get("run_ts")), json.dumps(rec, indent=1, ensure_ascii=False),
                         f"arbiter: {rec['delta_id']} {status}")
    return label
