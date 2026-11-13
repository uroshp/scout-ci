"""Run or read the OpenRouter study (scout/orstudy.py). Spend-safe default: prints the plan, calls nothing.

    python scripts/openrouter_study.py                      # the plan: models, calls, what is left
    python scripts/openrouter_study.py --router-check       # harmless prompts only: does the router honour provider rules
    python scripts/openrouter_study.py --run --per-model 1  # one real call per model
    python scripts/openrouter_study.py --run --tell         # tonight's share of the day's requests, checks, one email if they fail
    python scripts/openrouter_study.py --report             # the read-out from the stored results
    python scripts/openrouter_study.py --verify             # the checks alone

Reads the captured calls from the private store (read only, creds from ~/scout-replay/env the same
way the lane loads them); writes only under ~/scout-replay/openrouter_study/ on this machine.
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

STUDY_SINCE = "20261030T000000"      # fixed start: the population only grows while the study stays open


def _load_calls(since: str) -> tuple[list, dict]:
    """(calls in the study's fixed order, notes). Only roles the grader provably scores; whole captured
    prompts only; one row per call id. The order never reshuffles: roles in the order first recorded
    (smallest first on day one), oldest call first inside a role, so new calls join at the end."""
    import replay_calls as lane
    from scout import orstudy, replaybackends as rb, rolespecs
    lane._require_store()
    raw = lane.load_calls(lane.list_bundles(since))
    seen, calls, cut = set(), [], 0
    for c in raw:
        spec = rolespecs.spec(c.get("role"))
        if c.get("status") != "ok" or spec is None or spec.get("tools_on") or c.get("call_id") in seen:
            continue
        if rb._truncated_exact(c) or not rb.exact_prompt(c)[1].strip():
            cut += 1; continue
        seen.add(c["call_id"])
        calls.append(c)
    roles, not_scored = orstudy.gradable_roles(calls)
    left_out = Counter(c["role"] for c in calls if c["role"] not in roles)
    calls = [c for c in calls if c["role"] in roles]
    order_path = os.path.join(orstudy.STUDY_DIR, "role_order.json")
    try:
        order = json.load(open(order_path))
    except Exception:
        order = []
    n_role = Counter(c["role"] for c in calls)
    for role in sorted(roles, key=lambda r: (n_role[r], r)):
        if role not in order:
            order.append(role)
    os.makedirs(orstudy.STUDY_DIR, exist_ok=True)
    json.dump(order, open(order_path, "w"))
    calls.sort(key=lambda c: (order.index(c["role"]), c.get("run_ts") or "", c["call_id"]))
    json.dump({c["call_id"]: c["role"] for c in calls}, open(os.path.join(orstudy.STUDY_DIR, "population.json"), "w"))
    return calls, {"cut_off_prompts": cut, "not_scored": {r: (not_scored[r], left_out[r]) for r in not_scored}}


def _population() -> dict:
    from scout import orstudy
    try:
        return json.load(open(os.path.join(orstudy.STUDY_DIR, "population.json")))
    except Exception:
        return {}


def _median(v):
    v = sorted(v)
    return v[len(v) // 2] if v else None


def _table(rows: list, pop: dict) -> dict:
    """Per role: the models that have attempted at least COVERAGE of the role's calls, the calls all of
    them attempted, and each one's same-decision count on exactly those calls."""
    from scout import orstudy
    out = {}
    models = sorted({r["backend"] for r in rows})
    for role in sorted(set(pop.values())):
        role_calls = {cid for cid, r in pop.items() if r == role}
        got = {m: {r["call_id"]: r for r in rows if r["backend"] == m and r["call_id"] in role_calls} for m in models}
        inn = [m for m in models if len(got[m]) >= orstudy.COVERAGE * len(role_calls)]
        common = set.intersection(*[set(got[m]) for m in inn]) if inn else set()
        cells = {}
        for m in inn:
            sc = [orstudy.score_row(got[m][cid]) for cid in sorted(common)]
            cells[m] = {"same": sum(s["same"] for s in sc), "n": len(sc),
                        "no_answer": sum(1 for s in sc if s["why"].startswith("no answer")),
                        "unreadable": sum(1 for s in sc if s["why"] == "unreadable format"),
                        "agree": sum(s["agree"] for s in sc), "judged": sum(s["judged"] for s in sc)}
        out[role] = {"calls": len(role_calls), "common": len(common), "cells": cells,
                     "behind": {m: len(got[m]) for m in models if m not in inn}}
    return out


def report(say=print) -> None:
    from scout import orstudy
    rows, pop = orstudy.load_results(), _population()
    rows = [r for r in rows if r["call_id"] in pop]
    if not rows:
        say("no stored results yet"); return
    say(f"stored results: {len(rows)}  models: {len({r['backend'] for r in rows})}  calls in the study: {len(pop)}")
    say("\nSAME DECISION AS PRODUCTION, per job. Counted on the calls every compared model attempted;")
    say("a call the model failed to answer, or answered in an unreadable format, counts against it.")
    say("Not counted: extra findings a model flags that production did not name.")
    for role, t in _table(rows, pop).items():
        say(f"\n  {role}: {t['calls']} calls in the study, {t['common']} attempted by all {len(t['cells'])} compared models")
        for m, c in sorted(t["cells"].items(), key=lambda kv: -kv[1]["same"]):
            pct = f"{100 * c['same'] / c['n']:.0f}%" if c["n"] else "--"
            small = "  (too few calls to read much into)" if c["n"] < 20 else ""
            say(f"    {m[3:][:46]:46} {pct:>4}  {c['same']:3}/{c['n']:<3} same; no answer {c['no_answer']}, unreadable {c['unreadable']}; "
                f"verdicts {c['agree']}/{c['judged']}{small}")
        for m, n in sorted(t["behind"].items(), key=lambda kv: -kv[1]):
            say(f"    {m[3:][:46]:46} not compared yet: {n} of {t['calls']} calls attempted")
    say("\nPER MODEL: stored, answered, why not, median seconds, answers with a reasoning trace, who served it")
    for backend in sorted({r["backend"] for r in rows}):
        cell = [r for r in rows if r["backend"] == backend]
        ok = [r for r in cell if r["status"] == "ok"]
        why = Counter(r.get("reason") for r in cell if r["status"] != "ok")
        served = Counter(f"{(r.get('backend_version') or {}).get('served_model')} via {(r.get('backend_version') or {}).get('served_by')}" for r in cell
                         if (r.get("backend_version") or {}).get("served_by"))
        med = _median([r["duration_ms"] for r in ok if r.get("duration_ms")])
        say(f"  {backend[3:][:46]:46} stored={len(cell)} answered={len(ok)} not_answered={dict(why)} "
            f"median={(med or 0) / 1000:.1f}s traces={sum(1 for r in ok if r.get('thinking'))} "
            f"billed=${sum(r.get('cost_usd') or 0 for r in cell):.4f}")
        if backend == "or:" + orstudy.ROUTER or len(served) > 1:
            for k, n in served.most_common(12):
                say(f"      {n:4}  {k}")
    att = orstudy.load_attempts()
    say(f"\nEVERY REQUEST SENT: {len(att)}")
    per = defaultdict(Counter)
    for a in att:
        per[a["model"]][a.get("reason")] += 1
    for m in sorted(per):
        say(f"  {m[:46]:46} {dict(per[m])}")


def verify(calls: list, notes: dict, say=print) -> list:
    """Is the study doing what it claims? Every check reads stored output or re-runs the grader on a
    known answer. Returns the failures (empty = all held)."""
    from scout import modelcompare, orstudy, rolespecs
    rows, pop = orstudy.load_results(), {c["call_id"]: c["role"] for c in calls}
    fails = []

    def check(name, ok, detail=""):
        say(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f": {detail}" if detail else ""))
        if not ok:
            fails.append(f"{name}: {detail}")

    roles = sorted(set(pop.values()))
    for role, (why, n) in sorted(notes["not_scored"].items()):
        say(f"  note  {role}: {n} calls left out of the study, {why}")
    say(f"  note  {notes['cut_off_prompts']} captured calls left out: their stored prompt is cut off")
    # 1. the grader: on every role in the study, production's own answer is the same decision (that is
    #    how a role gets in), and a non-answer never is
    wrong = []
    for role in roles:
        first = next(c for c in calls if c["role"] == role)
        for garbage in ("I am not sure what you want.", "{}", '{"note": "nothing to report"}', ""):
            cmp_ = modelcompare.compare_call(first, {"status": "ok", "text": garbage, "backend": "control"})
            if orstudy.score(cmp_, role, fmt_ok=orstudy.format_ok(role, garbage))["why"] != "unreadable format":
                wrong.append(f"{role} on {garbage!r}")
    check("the grader never scores a non-answer as the same decision", not wrong, ", ".join(wrong))
    check("the study covers enough of Scout's jobs", len(roles) >= 3 and len(calls) >= 60, f"{len(roles)} jobs, {len(calls)} calls: {', '.join(roles)}")
    if not rows:
        check("results exist", False, "nothing stored yet")
        return fails
    att = orstudy.load_attempts()
    # 2. money
    billed = sum(r.get("cost_usd") or 0 for r in rows) + sum(a.get("cost") or 0 for a in att)
    check("nothing was billed", billed == 0, f"${billed:.6f}")
    # 3. providers: every request that was served, by someone approved
    served = Counter(str(a.get("served_by")) for a in att if a.get("http") == 200 and "served_by" in a)
    names = set()
    for f in sorted(os.listdir(os.path.join(orstudy.STUDY_DIR, "catalogue"))) if os.path.isdir(os.path.join(orstudy.STUDY_DIR, "catalogue")) else []:
        try:
            names |= {n for m in json.load(open(os.path.join(orstudy.STUDY_DIR, "catalogue", f)))["models"] for n in m["provider_names"]}
        except Exception:
            pass
    bad = {k: n for k, n in served.items() if k not in names}
    check("every request was served by an approved provider", not bad and bool(served), f"{dict(served)}")
    rules = Counter(json.dumps(a.get("rule"), sort_keys=True) for a in att)
    check("every request carried an allow-list of providers", all('"only"' in k for k in rules), f"{len(rules)} distinct rules")
    # 4. named models are answered by the model asked for
    off = Counter()
    for r in rows:
        v = r.get("backend_version") or {}
        if r["status"] == "ok" and r["backend"] != "or:" + orstudy.ROUTER and v.get("served_model") != v.get("model"):
            off[f"{v.get('model')} answered by {v.get('served_model')}"] += 1
    check("each named model was answered by that model", not off, str(dict(off)) if off else "")
    # 5. settings, from the stored rows: no schema anywhere; reasoning actually happened where asked
    check("no output schema was sent to any model", not any(r.get("schema_enforced") for r in rows))
    for backend in sorted({r["backend"] for r in rows}):
        ok = [r for r in rows if r["backend"] == backend and r["status"] == "ok"]
        asked = [r for r in ok if (r.get("backend_version") or {}).get("reasoning_requested")]
        traced = sum(1 for r in asked if r.get("thinking") or ((r.get("tokens") or {}).get("thinking") or 0) > 0)
        say(f"        {backend[3:][:46]:46} answered={len(ok)} reasoning asked={len(asked)} seen={traced}")
    # 6. nothing stored that is not a model result
    transient = Counter(r.get("reason") for r in rows if r.get("reason") in ("rate_limited", "network", "broken_response", "provider_error", "model_refused"))
    check("no rate limit, network error or broken response is stored as a result", not transient, str(dict(transient)) if transient else "")
    stray = sum(1 for r in rows if r["call_id"] not in pop)
    check("every stored result belongs to a call in the study", stray == 0, f"{stray} stray")
    # 7. comparability is by construction (the read-out counts only calls every compared model attempted);
    #    holes in a model's run through the list are reported, they close on its next run
    order = [c["call_id"] for c in calls]
    for backend in sorted({r["backend"] for r in rows}):
        got = {r["call_id"] for r in rows if r["backend"] == backend}
        missing = sum(1 for cid in order[:len(got)] if cid not in got)
        if missing:
            say(f"  note  {backend[3:]}: {missing} earlier calls not attempted yet (new arrivals or a rested model)")
    # 8. the request counter against the requests logged for the same UTC day
    today = orstudy._day()
    sent = sum(1 for a in att + orstudy.load_attempts("router_check.jsonl")
               if time.strftime("%Y-%m-%d", time.gmtime(datetime.fromisoformat(a["at"]).timestamp())) == today)
    check("the request counter matches the requests logged today", abs(orstudy.used_today() - sent) <= 3, f"counter {orstudy.used_today()}, logged {sent}")
    # 9. is it getting anywhere
    answered = {b for b in {r["backend"] for r in rows} if any(r["status"] == "ok" for r in rows if r["backend"] == b)}
    check("at least half the models are answering", len(answered) * 2 >= len({r["backend"] for r in rows}), f"{len(answered)} of {len({r['backend'] for r in rows})}")
    return fails


def _tell(lines: list) -> None:
    """One plain email a day, marked as sent only when it was."""
    from scout import orstudy
    mark = os.path.join(orstudy.STUDY_DIR, "told.json")
    day = time.strftime("%Y-%m-%d")
    try:
        if json.load(open(mark)).get("day") == day:
            return
    except Exception:
        pass
    try:
        from scout import notify
        body = ("The OpenRouter study needs a look before its numbers are used.\n\n" + "\n".join("- " + f for f in lines)
                + "\n\nNothing was billed unless a line above says so. The log is ~/scout-replay/openrouter_study.log.")
        res = notify._dispatch("Scout: the OpenRouter study needs a look", body, dry_run=False)
        if isinstance(res, dict) and res.get("sent") is False:
            print(f"  the study email was not sent: {res}", file=sys.stderr); return
        json.dump({"day": day}, open(mark, "w"))
    except Exception as e:
        print(f"  could not send the study email ({type(e).__name__})", file=sys.stderr)


def router_check(n: int, say=print) -> None:
    """Harmless prompts only. Asks the free router for answers while allowing one provider; if every
    answer comes from that provider, the router honours the allow-list the study relies on."""
    from scout import orstudy
    cat = orstudy.catalogue()
    router = next((e for e in cat["models"] if e.get("router")), None)
    if not router:
        say("the router is not in the catalogue"); return
    rec = {"call_id": "router-check", "role": None}
    for label, prov, names in (("only nvidia allowed", {"only": ["nvidia"]}, ["Nvidia"]),
                               ("the study's allow-list", {"only": router["providers"]}, router["provider_names"])):
        seen = Counter()
        for _ in range(n):
            r = orstudy.call({**router, "reasoning": False, "provider_names": names}, rec, "", orstudy.HARMLESS_PROMPT,
                             provider=prov, timeout=120, log="router_check.jsonl")
            v = r.get("backend_version") or {}
            seen[f"{v.get('served_model')} via {v.get('served_by')}" if v.get("served_by") else f"{r.get('reason')}: {str(r.get('text'))[:80]}"] += 1
            if r.get("outcome") == "stop_all":
                say(f"  stopped: {r.get('reason')}: {str(r.get('text'))[:120]}"); break
        say(f"router check, {label}:")
        for k, c in seen.most_common():
            say(f"  {c:3}  {k}")


def _other_run_alive(lock: str) -> bool:
    try:
        pid = int(open(lock).read().strip())
        cmd = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=10).stdout
        return "openrouter_study" in cmd
    except Exception:
        return False


def run(args) -> list:
    from scout import orstudy
    cat = orstudy.catalogue()
    entries = cat["models"]
    if args.models:
        want = set(args.models.split(","))
        entries = [e for e in entries if e["model"] in want]
    if args.no_router:
        entries = [e for e in entries if not e.get("router")]
    calls, notes = _load_calls(args.since)
    left = max(0, orstudy.DAILY_CAP - orstudy.used_today())
    print(f"{datetime.now():%Y-%m-%d %H:%M} study: {len(entries)} models, {len(calls)} calls since {args.since}; "
          f"requests used today {orstudy.used_today()}, left {left}")
    print("  jobs: " + ", ".join(f"{r} {n}" for r, n in Counter(c["role"] for c in calls).items()))
    for d in cat["dropped"]:
        print(f"  left out: {d['model']}: {d['why']}")
    todo = {}
    for e in entries:
        have = orstudy.have(e["model"])
        todo[e["model"]] = sum(1 for c in calls if orstudy.slug(c["call_id"]) not in have)
        print(f"  {e['model'][:50]:50} to do {todo[e['model']]:4}  via {','.join(e['providers'])} "
              f"reasoning={'asked' if e['reasoning'] else 'n/a'} output room={min(orstudy.MAX_OUT, e['max_out']) if e.get('max_out') else 'provider default'}")
    if args.verify and not args.run:
        print("checks:")
        fails = verify(calls, notes)
        print("ALL CHECKS HELD" if not fails else f"{len(fails)} CHECK(S) FAILED")
        return fails
    if not args.run:
        print("\nPLAN ONLY: no model calls made. Re-run with --run.")
        return []
    lock = os.path.join(orstudy.STUDY_DIR, "running.pid")
    if _other_run_alive(lock):
        print("another study run is active; exit 0"); return []
    open(lock, "w").write(str(os.getpid()))
    try:
        orstudy.save_catalogue(cat)
        orstudy.reset_stop()
        t0 = time.monotonic()
        deadline = None
        if args.deadline:
            h, m = args.deadline.split(":")
            deadline = datetime.now().replace(hour=int(h), minute=int(m), second=0, microsecond=0)
            if deadline < datetime.now():
                deadline += timedelta(days=1)

        def should_stop():
            return (time.monotonic() - t0) / 60 > args.max_minutes or (deadline is not None and datetime.now() >= deadline)

        out_lock = threading.Lock()

        def say(s):
            with out_lock:
                print(s, flush=True)
        prior = Counter((a["model"], a.get("call_id")) for a in orstudy.load_attempts() if a.get("reason") == "provider_error")
        resting, totals, crashed = {}, Counter(), []
        for rnd in (1, 2, 3):
            active = [e for e in entries if e["model"] not in resting and todo[e["model"]] > 0]
            left = max(0, orstudy.DAILY_CAP - orstudy.used_today())
            if not active or orstudy.stop_reason() or should_stop():
                break
            quota = args.per_model if args.per_model is not None else left // len(active)
            if quota < 1:
                break
            say(f"round {rnd}: {len(active)} models, up to {quota} requests each")
            results = []

            def work(e):
                try:
                    results.append(orstudy.run_model(e, calls, quota, prior_errors=prior, should_stop=should_stop, say=say))
                except Exception as ex:
                    results.append({"model": e["model"], "stored": 0, "attempts": 0, "ended": f"crashed ({type(ex).__name__}: {str(ex)[:120]})"})
            threads = [threading.Thread(target=work, args=(e,)) for e in active]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            for r in results:
                totals[r["model"]] += r["stored"]
                todo[r["model"]] -= r["stored"]
                if r["ended"] not in ("done", "quota"):
                    resting[r["model"]] = r["ended"]
                if r["ended"].startswith("crashed"):
                    crashed.append(f"{r['model']}: {r['ended']}")
                say(f"  {r['model'][:50]:50} stored {r['stored']:3} of {r['attempts']:3} requests; ended: {r['ended']}")
            if args.per_model is not None:
                break
        say(f"{datetime.now():%Y-%m-%d %H:%M} study run over: {sum(totals.values())} results stored; "
            f"requests used today {orstudy.used_today()}")
        say("checks:")
        fails = verify(calls, notes, say) + crashed
        why = orstudy.stop_reason()
        if why and not any(k in why for k in ("daily_cap", "account_daily_limit")):
            fails.append("stopped for all models: " + why)
        if sum(totals.values()) == 0 and sum(todo.values()) > 0 and left > len(entries):
            fails.append(f"tonight's run stored nothing; models rested: {resting}")
        say("ALL CHECKS HELD" if not fails else f"{len(fails)} CHECK(S) FAILED:\n  " + "\n  ".join(fails))
        return fails
    finally:
        try:
            os.remove(lock)
        except OSError:
            pass


def main():
    import replay_calls as lane
    lane._load_env()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--verify", action="store_true", help="check the study against its own stored output")
    ap.add_argument("--tell", action="store_true", help="email once a day when a check fails or the run could not start")
    ap.add_argument("--router-check", type=int, nargs="?", const=8, default=0)
    ap.add_argument("--per-model", type=int, default=None, help="requests per model this run (default: an even share of what is left today)")
    ap.add_argument("--models", default=None, help="comma list of model ids; default = every usable free model")
    ap.add_argument("--no-router", action="store_true")
    ap.add_argument("--since", default=STUDY_SINCE)
    ap.add_argument("--max-minutes", type=int, default=240)
    ap.add_argument("--deadline", default=None, help="HH:MM local; stop cleanly")
    args = ap.parse_args()
    if args.report:
        report(); return
    if args.router_check:
        router_check(args.router_check); return
    try:
        fails = run(args)
    except (Exception, SystemExit) as e:
        if isinstance(e, SystemExit) and e.code in (0, None):
            raise
        fails = [f"the run could not start or died: {type(e).__name__}: {str(e)[:200]}"]
        print(fails[0], file=sys.stderr)
        if args.tell and args.run:
            _tell(fails)
        raise SystemExit(1)
    if args.tell and args.run and fails:
        _tell(fails)


if __name__ == "__main__":
    main()
