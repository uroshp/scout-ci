"""Shadow-eval CHALLENGER for v3.5 (docs/vnext-roadmap.md §v3.5, decision-log §11).

The champion is the deterministic code grader (grounding) + the verifier's cut log — captured by
scout/shadow.py to shadow/<slug>/*.json. This module runs the CHALLENGER: a tools-off model that
INDEPENDENTLY re-judges each captured claim (kept or cut) keep-or-cut, over the claim's CAPTURED
EVIDENCE only (no re-research), so we can mine DISAGREEMENTS with the code grader:

  - champion CUT  + challenger KEEP  -> recovery candidate  (model would keep what code cut)
  - champion KEPT + challenger CUT   -> slop candidate      (model would cut what code kept)

Disagreements are the unit of study; a human later adjudicates each (was the challenger right?).
NON-DISRUPTION: read-only over already-captured data, no card is ever touched, the whole pass is a
separate offline step (scripts/run_challenger.py / shadow-eval.yml).

MODEL = config.CHALLENGER_MODEL (Sonnet, NOT Haiku — decision-log §11). The challenger judges over
the captured source excerpt, the reference-based regime where agreement with ground truth is highest.

Two agreement numbers (don't conflate):
  - kappa_champion_vs_challenger: computed HERE, free, no human. "How aligned is the model judge with
    the code floor." A sanity/aggregate number, NOT the promotion metric.
  - kappa_challenger_vs_human: the PROMOTION metric (lab practice, target ~0.6+). Needs a human to
    adjudicate the disagreements first (shadow_eval/challenger_labels.jsonl); scaffolded below,
    populated once labels exist. TODO until adjudication is done.
"""
import asyncio
import hashlib
import json
import sys
from datetime import datetime

from claude_agent_sdk import ClaudeAgentOptions

from scout import config, selfserve
from scout.generate import _drive, _extract_json

SCHEMA_VERSION = 1
SHADOW_EVAL_DIR = "shadow_eval"
LABELS_PATH = "shadow_eval/challenger_labels.jsonl"   # human adjudication of disagreements (delta_id -> agree|disagree)

_CHALLENGER_SYSTEM = (
    "You are an independent verification judge for a competitive-intelligence pipeline. For each "
    "CLAIM you are given, decide whether it should SURVIVE verification (keep) or be removed (cut), "
    "judged ONLY on whether the supplied evidence credibly and specifically supports the claim from "
    "a trustworthy source. Be adversarial about groundedness: cut a claim whose evidence is a weak "
    "aggregator, second-hand/proxy attribution, an unverifiable figure, or an excerpt that does not "
    "actually state what the claim asserts; keep a claim whose evidence is specific, on-point, and "
    "from a credible (primary or tier-1) source. Judge each claim ON ITS OWN MERITS — do not defer "
    "to any prior note about it. Reason only from what you are given; you have no tools and must not "
    "assume facts not present. Return ONLY a JSON object:\n"
    '{"verdicts": [{"item_id": "<id>", "verdict": "keep" | "cut", '
    '"reason": "<one sentence>", "confidence": "high" | "medium" | "low"}]}\n'
    "Return exactly one verdict per item_id you were given."
)

# Neutral variant — used by the prompt-bias A/B (decision-log §11). Same task, but NOT adversarial,
# and it explicitly tells the judge that the supplied excerpt may be only ONE of several supporting
# snippets, so it should not cut merely because the excerpt is partial. If the slop count collapses
# under this prompt, the adversarial framing + single-excerpt capture were driving the disagreements.
_CHALLENGER_SYSTEM_NEUTRAL = (
    "You are a verification judge for a competitive-intelligence pipeline. For each CLAIM, decide "
    "whether it should SURVIVE verification (keep) or be removed (cut). KEEP a claim if the supplied "
    "evidence reasonably supports its core assertion from a credible source. CUT only when the "
    "evidence clearly fails to support the core assertion, or the source is untrustworthy. IMPORTANT: "
    "the excerpt you are shown may be just ONE of several snippets that grounded the claim — do NOT "
    "cut a claim merely because this single excerpt omits some sub-detail or figure; cut only if the "
    "core assertion is clearly unsupported or contradicted. Reason only from what you are given; you "
    "have no tools. Return ONLY a JSON object:\n"
    '{"verdicts": [{"item_id": "<id>", "verdict": "keep" | "cut", '
    '"reason": "<one sentence>", "confidence": "high" | "medium" | "low"}]}\n'
    "Return exactly one verdict per item_id you were given."
)
_SYSTEMS = {"adversarial": _CHALLENGER_SYSTEM, "neutral": _CHALLENGER_SYSTEM_NEUTRAL}


def _items(record: dict) -> tuple[list, dict]:
    """Build (model_items, champion_by_id) from a captured champion record. model_items is what the
    challenger sees (NO champion label — judge fresh); champion_by_id maps item_id -> 'keep'|'cut'.
    Kept claims carry their real id + evidence excerpt; cut entries get a synthetic id and their
    recorded cut reason as a neutral note (the only evidence captured for a cut)."""
    model_items, champion = [], {}
    for i, c in enumerate(record.get("kept") or []):
        if not isinstance(c, dict):
            continue
        item_id = c.get("id") or f"keep:{i}"
        champion[item_id] = "keep"
        model_items.append({
            "item_id": item_id,
            "claim": c.get("claim"),
            "evidence": c.get("evidence_excerpt"),
            "source_url": c.get("source_url"),
            "source_tier": c.get("source_tier"),
        })
    for j, e in enumerate(record.get("cut") or []):
        if not isinstance(e, dict):
            continue
        item_id = f"cut:{j}"
        champion[item_id] = "cut"
        model_items.append({
            "item_id": item_id,
            "claim": e.get("claim"),
            "evidence": None,                       # a cut claim had no surviving grounded excerpt
            "note": e.get("reason"),                # the recorded reason, framed neutrally below
        })
    return model_items, champion


async def _run_challenger(slug: str, model_items: list, system: str = _CHALLENGER_SYSTEM) -> dict:
    user = (
        f"Card: {slug}. Judge each of the following {len(model_items)} claims keep-or-cut on "
        "groundedness, independently.\n\n" + json.dumps(model_items, ensure_ascii=False, indent=2)
    )
    options = ClaudeAgentOptions(
        model=config.CHALLENGER_MODEL,                # Sonnet — decision-log §11
        system_prompt=system,
        mcp_servers={},
        allowed_tools=[],                             # TOOLS-OFF: judge only the captured evidence
        disallowed_tools=["WebSearch", "WebFetch"],
        permission_mode="bypassPermissions",
        max_turns=config.CHALLENGER_MAX_TURNS,
        max_budget_usd=config.CHALLENGER_MAX_BUDGET_USD,
    )
    return await _drive(user, options, "challenger")


def judge_record(record: dict, variant: str = "adversarial") -> dict:
    """Run the challenger over one captured champion record. Returns
    {verdicts: {item_id: {verdict, reason, confidence}}, cost_usd, model}. Never raises into the
    caller — a parse failure yields empty verdicts (the run still cost money; cost is reported).
    `variant` selects the system prompt ('adversarial' default, or 'neutral' for the prompt-bias A/B)."""
    model_items, _ = _items(record)
    if not model_items:
        return {"verdicts": {}, "cost_usd": 0.0, "model": config.CHALLENGER_MODEL}
    res = asyncio.run(_run_challenger(record.get("slug", "?"), model_items,
                                      _SYSTEMS.get(variant, _CHALLENGER_SYSTEM)))
    verdicts = {}
    try:
        for v in _extract_json(res["text"]).get("verdicts", []) or []:
            iid = v.get("item_id")
            verd = str(v.get("verdict", "")).strip().lower()
            if iid and verd in ("keep", "cut"):
                verdicts[iid] = {"verdict": verd, "reason": v.get("reason"),
                                 "confidence": v.get("confidence")}
    except Exception as e:
        print(f"[challenger] verdict parse failed ({type(e).__name__}: {e})", file=sys.stderr)
    return {"verdicts": verdicts, "cost_usd": res.get("cost_usd"), "model": config.CHALLENGER_MODEL}


def cohens_kappa(rater_a: list, rater_b: list) -> float | None:
    """Cohen's kappa for two raters over paired categorical labels. Model-free, pure, testable.
    None when undefined (no items, or expected agreement == 1 i.e. a rater used a single class)."""
    n = len(rater_a)
    if n == 0 or n != len(rater_b):
        return None
    po = sum(1 for x, y in zip(rater_a, rater_b) if x == y) / n
    labels = set(rater_a) | set(rater_b)
    pe = sum((rater_a.count(l) / n) * (rater_b.count(l) / n) for l in labels)
    if pe >= 1.0:
        return None
    return (po - pe) / (1.0 - pe)


def _delta_id(slug: str, run_ts, item_id: str) -> str:
    """Stable id for one champion-vs-challenger disagreement (the human-label key)."""
    return "x_" + hashlib.sha256(f"{slug}|{run_ts}|{item_id}".encode("utf-8")).hexdigest()[:12]


def compare(record: dict, judged: dict) -> dict:
    """Compare the challenger's verdicts to the champion's keep/cut for one record. Classifies each
    item agree/disagree (+ direction) and computes the champion-vs-challenger Cohen's kappa over the
    items the challenger actually judged."""
    _, champion = _items(record)
    verdicts = judged.get("verdicts", {})
    slug, run_ts = record.get("slug"), record.get("run_ts")
    items, ca, cb = [], [], []
    by_id = {c.get("id"): c for c in (record.get("kept") or []) if isinstance(c, dict)}
    cut_by_idx = {f"cut:{j}": e for j, e in enumerate(record.get("cut") or []) if isinstance(e, dict)}
    for item_id, champ in champion.items():
        v = verdicts.get(item_id)
        if not v:
            items.append({"item_id": item_id, "champion": champ, "challenger": None,
                          "status": "abstain"})
            continue
        chal = v["verdict"]
        ca.append(champ); cb.append(chal)
        if chal == champ:
            items.append({"item_id": item_id, "champion": champ, "challenger": chal,
                          "status": "agree"})
            continue
        direction = "recovery_candidate" if (champ == "cut" and chal == "keep") else "slop_candidate"
        src = by_id.get(item_id) or cut_by_idx.get(item_id) or {}
        items.append({
            "delta_id": _delta_id(slug, run_ts, item_id),
            "item_id": item_id, "champion": champ, "challenger": chal, "status": "disagree",
            "direction": direction,
            "claim": src.get("claim"),
            "challenger_reason": v.get("reason"), "challenger_confidence": v.get("confidence"),
        })
    disagreements = [x for x in items if x["status"] == "disagree"]
    return {
        "slug": slug, "run_ts": run_ts, "source": record.get("source"),
        "items": items,
        "summary": {
            "judged": len(ca), "abstain": sum(1 for x in items if x["status"] == "abstain"),
            "agree": sum(1 for x in items if x["status"] == "agree"),
            "disagree": len(disagreements),
            "recovery_candidates": sum(1 for x in disagreements if x["direction"] == "recovery_candidate"),
            "slop_candidates": sum(1 for x in disagreements if x["direction"] == "slop_candidate"),
            "agreement_rate": (round(sum(1 for x in items if x["status"] == "agree") / len(ca), 3)
                               if ca else None),
            "kappa_champion_vs_challenger": (round(k, 3) if (k := cohens_kappa(ca, cb)) is not None
                                             else None),
        },
    }


def _content_hash(record: dict) -> str:
    model_items, _ = _items(record)
    blob = json.dumps([(i.get("item_id"), i.get("claim")) for i in model_items],
                      ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(f"{record.get('slug')}|{record.get('run_ts')}|{blob}".encode()).hexdigest()[:12]


def result_record(record: dict, judged: dict, comparison: dict) -> dict:
    """The persisted challenger result for one champion record (idempotency-keyed by content hash)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "slug": record.get("slug"), "run_ts": record.get("run_ts"),
        "champion_source": record.get("source"),
        "challenger_model": judged.get("model"),
        "content_hash": _content_hash(record),
        "judged_at": datetime.now().isoformat(timespec="seconds"),
        "cost_usd": judged.get("cost_usd"),
        "summary": comparison["summary"],
        "items": comparison["items"],
    }


def persist(result: dict) -> str:
    """Write a challenger result to shadow_eval/<slug>/<content_hash>.json (idempotent — re-judging
    the same champion record overwrites the same file). Returns the path."""
    path = f"{SHADOW_EVAL_DIR}/{result['slug']}/{result['content_hash']}.json"
    selfserve.write_data(path, json.dumps(result, indent=2, ensure_ascii=False),
                         f"shadow-eval: challenger {result['slug']} {result['content_hash']}")
    return path


def already_judged(slug: str, content_hash: str) -> bool:
    """True if this exact champion record already has a challenger result (so the Action skips it)."""
    return bool(selfserve.read_data(f"{SHADOW_EVAL_DIR}/{slug}/{content_hash}.json"))


# --- aggregate scorecard ------------------------------------------------------
def load_labels() -> dict:
    """delta_id -> human verdict ('agree' the challenger was right | 'disagree' it was wrong).
    Empty until a human adjudicates the disagreements (see scout.adjudicate for the analog)."""
    raw = selfserve.read_data(LABELS_PATH)
    out = {}
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            out[d["delta_id"]] = d["human_verdict"]
        except (json.JSONDecodeError, KeyError):
            continue
    return out


def scorecard(results: list) -> dict:
    """Aggregate across challenger result records. The champion-vs-challenger kappa is recomputed on
    the POOLED item labels (not averaged per card). The challenger-vs-human kappa is computed only
    over disagreements a human has labeled (empty until adjudication happens)."""
    ca, cb, disagreements = [], [], []
    cost = 0.0
    for r in results:
        cost += r.get("cost_usd") or 0.0
        for it in r.get("items", []):
            if it["status"] in ("agree", "disagree") and it.get("challenger"):
                ca.append(it["champion"]); cb.append(it["challenger"])
            if it["status"] == "disagree":
                # stamp the parent record's slug/run_ts onto the flat disagreement (items don't carry them)
                disagreements.append({**it, "slug": r.get("slug"), "run_ts": r.get("run_ts")})
    labels = load_labels()
    # Promotion metric: was the challenger right on the disagreements the human adjudicated?
    adjudicated = [d for d in disagreements if d.get("delta_id") in labels]
    judged_right = sum(1 for d in adjudicated if labels[d["delta_id"]] == "agree")
    # challenger-vs-human over adjudicated deltas: human 'agree' => challenger's label was correct
    human_truth = ["keep" if (d["challenger"] == "keep") == (labels[d["delta_id"]] == "agree")
                   else "cut" for d in adjudicated]
    chal_on_adj = [d["challenger"] for d in adjudicated]
    return {
        "records": len(results),
        "items_judged": len(ca),
        "agree": sum(1 for x, y in zip(ca, cb) if x == y),
        "disagreements": len(disagreements),
        "recovery_candidates": sum(1 for d in disagreements if d["direction"] == "recovery_candidate"),
        "slop_candidates": sum(1 for d in disagreements if d["direction"] == "slop_candidate"),
        "agreement_rate": round(sum(1 for x, y in zip(ca, cb) if x == y) / len(ca), 3) if ca else None,
        "kappa_champion_vs_challenger": (round(k, 3) if (k := cohens_kappa(ca, cb)) is not None else None),
        "cost_usd": round(cost, 4),
        "adjudication": {
            "disagreements": len(disagreements),
            "adjudicated": len(adjudicated),
            "challenger_right": judged_right,
            "challenger_wrong": len(adjudicated) - judged_right,
            # PROMOTION metric — needs human labels; None until any disagreement is adjudicated.
            "kappa_challenger_vs_human": (round(k2, 3)
                                          if (k2 := cohens_kappa(chal_on_adj, human_truth)) is not None
                                          else None),
        },
        "pending_disagreements": [d for d in disagreements if d.get("delta_id") not in labels],
    }
