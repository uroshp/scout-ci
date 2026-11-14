"""One table per job across every way Scout's calls have been answered: production (Claude direct),
the Mac mini's local models (the nightly lane), and OpenRouter three ways (its free models, pinned
paid models, and routing as OpenRouter intends it: the Auto Router and host-routed models).

Reads only: the study folders under ~/scout-replay/ and the private store's local mirror. Calls nothing.

    python scripts/openrouter_brief.py            # the tables
    python scripts/openrouter_brief.py --json     # the same numbers as JSON
"""
import argparse
import glob
import json
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HOME = os.path.expanduser("~/scout-replay")
MIRROR = os.path.expanduser("~/code/scout-user-data")
JOBS = {"screen": "screening news", "audience_judge": "audience judge", "judge": "verification judge", "gate_judge": "gate"}
LOCAL = {"ollama_gemma4": "Gemma 4 on the mini", "ollama_gptoss": "gpt-oss 20B on the mini", "ollama": "Magistral on the mini",
         "ollama_bolt": "Bolt E4B on the mini", "ollama_nano30b": "Nemotron Nano 30B on the mini",
         "ollama_nemotron": "Nemotron Nano 4B on the mini (retired)", "apple_ondevice": "Apple on-device"}


def _rows(folder: str) -> list:
    out = []
    for f in glob.glob(os.path.join(HOME, folder, "results", "*", "*.json")):
        try:
            out.append(json.load(open(f)))
        except (OSError, ValueError):
            pass
    return out


def _pop(folder: str) -> dict:
    try:
        return json.load(open(os.path.join(HOME, folder, "population.json")))
    except Exception:
        return {}


def _lane(pop: dict) -> list:
    out = []
    for f in glob.glob(os.path.join(MIRROR, "shadow_models", "**", "*.json"), recursive=True):
        try:
            r = json.load(open(f))
        except (OSError, ValueError):
            continue
        if isinstance(r, dict) and r.get("call_id") in pop and r.get("mode") == "exact" and not r.get("rep"):
            out.append(r)
    return out


def _median(v):
    v = sorted(x for x in v if x)
    return v[len(v) // 2] if v else None


def arms() -> tuple[dict, dict]:
    """({arm label: {"group", "rows": {call_id: row}}}, population {call_id: role})."""
    pop = {**_pop("openrouter_study"), **_pop("openrouter_paid"), **_pop("openrouter_routed")}
    out = {}

    def add(label, group, rows):
        cell = out.setdefault(label, {"group": group, "rows": {}})
        for r in rows:
            if r.get("call_id") in pop:
                cell["rows"][r["call_id"]] = r
    by = defaultdict(list)
    for r in _rows("openrouter_paid"):
        by[r["backend"][3:]].append(r)
    for m, rs in by.items():
        if m == "anthropic/claude-opus-5.5":
            add("Opus asked again (through OpenRouter)", "yardstick", rs)
        else:
            add(m, "OpenRouter, pinned paid model", rs)
    for n in (2, 3):
        rs = _rows(f"openrouter_paid_rep{n}")
        if rs:
            add(f"Opus asked again, pass {n}", "yardstick", rs)
    by = defaultdict(list)
    for r in _rows("openrouter_routed"):
        by[r["backend"][3:]].append(r)
    for m, rs in by.items():
        add("Auto Router" if m == "openrouter/auto" else f"{m}, host chosen by OpenRouter", "OpenRouter, routed", rs)
    by = defaultdict(list)
    for r in _rows("openrouter_study"):
        by[r["backend"][3:]].append(r)
    for m, rs in by.items():
        add("free router" if m == "openrouter/free" else m, "OpenRouter, free model", rs)
    by = defaultdict(list)
    for r in _lane(pop):
        by[r.get("backend")].append(r)
    for b, rs in by.items():
        if b == "anthropic":
            add("Opus asked again (Scout's production setup)", "yardstick", rs)
        elif b in LOCAL:
            add(LOCAL[b], "local, on the mini", rs)
    return out, pop


def cell(rows: list) -> dict:
    """What one arm did on these rows: decisions, verdicts, cost, time, who served."""
    from scout import orstudy
    sc = [orstudy.score_row(r) for r in rows]
    ok = [r for r in rows if r.get("status") == "ok"]
    v = [r.get("backend_version") or {} for r in ok]
    return {"n": len(rows), "same": sum(s["same"] for s in sc), "no_answer": sum(1 for s in sc if s["why"].startswith("no answer")),
            "unreadable": sum(1 for s in sc if s["why"] == "unreadable format"),
            "verdicts_same": sum(s.get("agree") or 0 for s in sc), "verdicts": sum(s.get("judged") or 0 for s in sc),
            "missed": sum(1 for s in sc if s["why"] == "missed it"), "false_alarm": sum(1 for s in sc if s["why"] == "false alarm"),
            "found": sum(s.get("found") or 0 for s in sc), "loud": sum(s.get("loud") or 0 for s in sc),
            "cost": sum(r.get("cost_usd") or 0 for r in rows), "median_s": (_median([r.get("duration_ms") for r in ok]) or 0) / 1000,
            "reasoning_tokens": _median([(r.get("tokens") or {}).get("thinking") for r in ok]),
            "models": Counter(str(x.get("served_model")) for x in v if x.get("routed")),
            "hosts": Counter(str(x.get("served_by")) for x in v if x.get("served_by")),
            "second_try": sum(1 for x in v if ((x.get("routing") or {}).get("attempt") or 1) > 1)}


def tables(min_cover: float = 0.9) -> dict:
    """Per job: every arm on its own calls, and the arms covering at least `min_cover` of the job on
    the calls all of them answered (the only rows that may be compared with each other)."""
    from scout import orstudy
    a, pop = arms()
    out = {}
    for role in JOBS:
        calls = {c for c, r in pop.items() if r == role}
        if not calls:
            continue
        own, cover = {}, {}
        for label, x in a.items():
            rows = [r for c, r in x["rows"].items() if c in calls]
            if rows:
                own[label] = {"group": x["group"], **cell(rows)}
                cover[label] = {c for c in x["rows"] if c in calls}
        inn = [l for l in own if len(cover[l]) >= min_cover * len(calls)]
        common = sorted(set.intersection(*[cover[l] for l in inn])) if inn else []
        compared = {l: {"group": a[l]["group"], **cell([a[l]["rows"][c] for c in common])} for l in inn}
        base = orstudy.baselines(role, [next(a[l]["rows"][c] for l in inn if ((a[l]["rows"][c].get("comparison") or {}).get("items")))
                                        for c in common if any((a[l]["rows"][c].get("comparison") or {}).get("items") for l in inn)]) if inn else {}
        out[role] = {"calls": len(calls), "common": len(common), "own": own, "compared": compared, "baselines": base}
    return out


def production_cost() -> dict:
    """{role: (calls, dollars, median seconds)} for what production paid on the same calls (from the capture)."""
    import openrouter_study as s
    from scout import orstudy
    out = {}
    try:
        s.PAID.update(on=True, mode="routed", roles=orstudy.PAID_ROLES + ("screen",))
        calls, _ = s._load_calls(orstudy.PAID_SINCE, False)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:120]}"}
    by = defaultdict(list)
    for c in calls:
        by[c["role"]].append(c)
    for role, cs in by.items():
        out[role] = (len(cs), sum((c.get("result") or {}).get("cost_usd") or 0 for c in cs),
                     (_median([(c.get("result") or {}).get("duration_ms") or c.get("wall_ms") for c in cs]) or 0) / 1000)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-production-cost", action="store_true", help="skip reading the captured calls (needs the store)")
    args = ap.parse_args()
    t = tables()
    prod = {} if args.no_production_cost else production_cost()
    if args.json:
        for role in t.values():
            for part in ("own", "compared"):
                for c in role[part].values():
                    c["models"], c["hosts"] = dict(c["models"]), dict(c["hosts"])
        print(json.dumps({"jobs": t, "production": prod}, indent=1)); return
    order = ["yardstick", "OpenRouter, routed", "OpenRouter, pinned paid model", "OpenRouter, free model", "local, on the mini"]

    def line(label, c):
        pct = f"{100 * c['same'] / c['n']:.0f}%" if c["n"] else "--"
        vd = f"{100 * c['verdicts_same'] / c['verdicts']:.0f}% ({c['verdicts_same']}/{c['verdicts']})" if c["verdicts"] else "--"
        per = f"${c['cost'] / c['n']:.4f}" if c["n"] else "--"
        return (f"    {label[:52]:52} calls {c['n']:3}  same decision {pct:>4} ({c['same']}/{c['n']})  verdicts {vd:>16}  "
                f"no answer {c['no_answer'] + c['unreadable']:2}  {per}/call  {c['median_s']:.0f}s  reasoning {c['reasoning_tokens'] or 0}")
    for role, x in t.items():
        print(f"\n=== {JOBS[role]} ({role}): {x['calls']} calls ===")
        if role in prod and isinstance(prod.get(role), tuple):
            n, usd, sec = prod[role]
            print(f"    production (Claude direct): {n} calls, ${usd / n:.4f}/call, {sec:.0f}s median")
        print(f"  COMPARABLE: arms that answered at least 90% of the job, on the {x['common']} calls all of them answered")
        for name, n in x["baselines"].items():
            print(f"    {'constant answer: ' + name:52} {100 * n / max(1, x['common']):.0f}% ({n}/{x['common']})")
        for g in order:
            for label, c in sorted(((l, c) for l, c in x["compared"].items() if c["group"] == g), key=lambda kv: -kv[1]["same"]):
                print(line(f"[{g.split(',')[0]}] {label}", c))
        print("  EVERY ARM ON ITS OWN CALLS (not comparable with each other where the call counts differ)")
        for g in order:
            for label, c in sorted(((l, c) for l, c in x["own"].items() if c["group"] == g), key=lambda kv: -kv[1]["same"] / max(1, kv[1]["n"])):
                print(line(f"[{g.split(',')[0]}] {label}", c))
                if c["models"]:
                    print(f"        picked: {dict(c['models'].most_common(8))}")
                if g == "OpenRouter, routed":
                    print(f"        hosts: {dict(c['hosts'].most_common(8))}; answered on a second host: {c['second_try']}")
                if role == "screen":
                    print(f"        missed {c['missed']}, false alarm {c['false_alarm']}; named {c['found']} of the {c['loud']} findings production flagged")


if __name__ == "__main__":
    main()
