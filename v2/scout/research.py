"""The research layer's numbers (Uroš, 2026-10-01: "a perpetual research layer on top of evals;
track how often models vary, how often they are right or wrong, the causes and how often they
happen, anything else an AI lab would track"). Pure aggregation over the arbiter's records
(scout/arbiter.py) and the replay results (scout/modelcompare.py). No model calls.

  variance      how often the arms disagree with the live model, per role (from the replay results)
  outcomes      per arm: arbitrated items, right, wrong, precision-vs-arbiter; the live judge too
  causes        per arm: failure-mode counts (the fixed vocabulary), and overall
  review        the human layer: agreed / overruled / pending, overrule rate (the arbiter's own error)
  by_period     the same per month (settings drift shows up here)
  by_size       right/wrong by prompt-size bucket (does the big picture get lost in long prompts?)
  cost          arbiter spend, items, seconds
"""
from __future__ import annotations

from collections import Counter, defaultdict

from scout import arbiter, modelcompare

SIZE_BUCKETS = ((0, 15000, "<15k chars"), (15000, 30000, "15-30k"), (30000, 45000, "30-45k"), (45000, 10**9, ">45k"))


def _bucket(n: int | None) -> str:
    if n is None:
        return "unknown"
    for lo, hi, name in SIZE_BUCKETS:
        if lo <= n < hi:
            return name
    return "unknown"


def variance(results: list) -> dict:
    """Per role: items the arms judged, how many they disagreed on, the share, and per arm."""
    out = {}
    per = defaultdict(lambda: Counter())
    for r in results:
        if r.get("mode", "exact") != "exact" or r.get("rep") or r.get("status") != "ok":
            continue
        items = (r.get("comparison") or {}).get("items") or []
        role, arm = r.get("role"), r.get("backend")
        for it in items:
            if it.get("status") in ("agree", "disagree"):
                per[(role, arm)]["judged"] += 1
                per[(role, arm)]["disagree"] += it.get("status") == "disagree"
    for (role, arm), c in per.items():
        d = out.setdefault(role, {"judged": 0, "disagree": 0, "arms": {}})
        d["judged"] += c["judged"]; d["disagree"] += c["disagree"]
        d["arms"][arm] = {"judged": c["judged"], "disagree": c["disagree"],
                          "rate": round(c["disagree"] / c["judged"], 3) if c["judged"] else None}
    for d in out.values():
        d["rate"] = round(d["disagree"] / d["judged"], 3) if d["judged"] else None
    return out


def outcomes(records: list) -> dict:
    """Per arm (live judge included): arbitrated, right, wrong, precision vs the arbiter's ruling."""
    per = defaultdict(Counter)
    for rec in records:
        for who, g in (rec.get("grades") or {}).items():
            per[who]["arbitrated"] += 1
            per[who]["right" if g.get("right") else "wrong"] += 1
            if g.get("right") and "right_for_wrong_reason" in (g.get("modes") or []):
                per[who]["right_for_wrong_reason"] += 1
    out = {}
    for who, c in per.items():
        out[who] = {"arbitrated": c["arbitrated"], "right": c["right"], "wrong": c["wrong"],
                    "right_for_wrong_reason": c["right_for_wrong_reason"],
                    "precision": round(c["right"] / c["arbitrated"], 3) if c["arbitrated"] else None}
    return out


def causes(records: list) -> dict:
    per = defaultdict(Counter); total = Counter()
    for rec in records:
        for who, g in (rec.get("grades") or {}).items():
            for m in g.get("modes") or []:
                per[who][m] += 1; total[m] += 1
    return {"overall": dict(total.most_common()), "by_arm": {w: dict(c.most_common()) for w, c in per.items()}}


def review(records: list) -> dict:
    c = Counter((rec.get("review") or {}).get("status") or "pending" for rec in records)
    decided = c["agreed"] + c["overruled"]
    return {"agreed": c["agreed"], "overruled": c["overruled"], "pending": c["pending"],
            "overrule_rate": round(c["overruled"] / decided, 3) if decided else None}


def by_period(records: list) -> dict:
    out = {}
    for month in sorted({str(r.get("run_ts") or r.get("arbitrated_at") or "")[:7] for r in records}):
        sub = [r for r in records if str(r.get("run_ts") or r.get("arbitrated_at") or "")[:7] == month]
        out[month] = {"items": len(sub), "outcomes": outcomes(sub), "review": review(sub)}
    return out


def by_size(records: list) -> dict:
    per = defaultdict(lambda: defaultdict(Counter))
    for rec in records:
        b = _bucket(rec.get("prompt_chars"))
        for who, g in (rec.get("grades") or {}).items():
            per[b][who]["right" if g.get("right") else "wrong"] += 1
    return {b: {w: {"right": c["right"], "wrong": c["wrong"],
                    "precision": round(c["right"] / (c["right"] + c["wrong"]), 3) if (c["right"] + c["wrong"]) else None}
                for w, c in arms.items()} for b, arms in per.items()}


def cost(records: list) -> dict:
    n = len(records)
    usd = sum(float(r.get("cost_usd") or 0) for r in records)
    secs = sum(int(r.get("duration_ms") or 0) for r in records) / 1000
    searched = sum(1 for r in records if r.get("searches_used"))
    return {"items": n, "usd": round(usd, 2), "usd_per_item": round(usd / n, 3) if n else None,
            "seconds_per_item": round(secs / n, 1) if n else None, "items_with_search": searched}


def report(records: list | None = None, results: list | None = None) -> dict:
    records = arbiter.load_records() if records is None else records
    results = modelcompare.load_results() if results is None else results
    return {"variance": variance(results), "outcomes": outcomes(records), "causes": causes(records),
            "review": review(records), "by_period": by_period(records), "by_size": by_size(records), "cost": cost(records)}


def render(rep: dict) -> str:
    """Plain-text research digest (scout-research; the brief carries the short form)."""
    L = ["# Scout research layer", ""]
    L.append("## Variance: how often a local model disagrees with the live model")
    for role, d in sorted(rep["variance"].items()):
        arms = ", ".join(f"{a} {v['disagree']}/{v['judged']}" for a, v in sorted(d["arms"].items()))
        L.append(f"- {role}: {d['disagree']} of {d['judged']} items ({d['rate']}); {arms}")
    L.append("\n## Outcomes vs the arbiter (right when it disagreed or agreed, per side)")
    for who, o in sorted(rep["outcomes"].items(), key=lambda kv: -(kv[1]["precision"] or 0)):
        L.append(f"- {who}: {o['right']} right / {o['wrong']} wrong of {o['arbitrated']} (precision {o['precision']}; right for the wrong reason {o['right_for_wrong_reason']})")
    L.append("\n## Causes (failure modes, how often)")
    L.append("- overall: " + ", ".join(f"{m} {n}" for m, n in rep["causes"]["overall"].items()))
    for who, c in sorted(rep["causes"]["by_arm"].items()):
        L.append(f"- {who}: " + ", ".join(f"{m} {n}" for m, n in c.items()))
    r = rep["review"]
    L.append(f"\n## Human review of the arbiter: agreed {r['agreed']}, overruled {r['overruled']}, pending {r['pending']} (overrule rate {r['overrule_rate']})")
    L.append("\n## By prompt size (does the big picture get lost in long prompts?)")
    for b, arms in rep["by_size"].items():
        L.append(f"- {b}: " + ", ".join(f"{w} {v['right']}/{v['right'] + v['wrong']}" for w, v in sorted(arms.items())))
    c = rep["cost"]
    L.append(f"\n## Cost: {c['items']} items, ${c['usd']} (${c['usd_per_item']}/item, {c['seconds_per_item']} s/item, {c['items_with_search']} used web search)")
    return "\n".join(L)
