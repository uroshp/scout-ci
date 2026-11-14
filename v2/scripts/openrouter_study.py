"""Run or read the OpenRouter study (scout/orstudy.py). Spend-safe default: prints the plan, calls nothing.

    python scripts/openrouter_study.py                      # the plan: models, calls, what is left
    python scripts/openrouter_study.py --router-check       # harmless prompts only: does the router honour provider rules
    python scripts/openrouter_study.py --run --per-model 1  # one real call per model
    python scripts/openrouter_study.py --run --tell         # tonight's share of the day's requests, checks, one email if they fail
    python scripts/openrouter_study.py --report             # the read-out from the stored results
    python scripts/openrouter_study.py --verify             # the checks alone
    python scripts/openrouter_study.py --paid               # the paid judging section's plan (add --run, --report, --verify)

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
PAID = {"on": False}                 # --paid: the paid judging section, its own folder, model list and spending limit


def _write(path: str, obj) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh)
    os.replace(tmp, path)


def _load_calls(since: str, write: bool = False) -> tuple[list, dict]:
    """(calls in the study's fixed order, notes). Only calls whose production answer can serve as the
    reference; whole captured prompts only; one row per call id. The order never reshuffles: jobs in
    the order first recorded (smallest first on day one), oldest call first inside a job, so new
    calls join at the end. The order and the population are written only by a real run."""
    import replay_calls as lane
    from scout import orstudy, replaybackends as rb, rolespecs
    lane._require_store()
    raw = lane.load_calls(lane.list_bundles(since))
    seen, calls, cut_off = set(), [], 0
    for c in raw:
        spec = rolespecs.spec(c.get("role"))
        if c.get("status") != "ok" or spec is None or spec.get("tools_on") or c.get("call_id") in seen:
            continue
        if PAID["on"] and c.get("role") not in orstudy.PAID_ROLES:
            continue
        if rb._truncated_exact(c) or not rb.exact_prompt(c)[1].strip():
            cut_off += 1; continue
        seen.add(c["call_id"])
        calls.append(c)
    calls, left = orstudy.gradable(calls, min_good=0.0 if PAID["on"] else 0.9)
    roles = sorted({c["role"] for c in calls})
    order_path = os.path.join(orstudy.STUDY_DIR, "role_order.json")
    try:
        order = json.load(open(order_path))
    except Exception:
        order = []
    n_role = Counter(c["role"] for c in calls)
    for role in sorted(roles, key=lambda r: (n_role[r], r)):
        if role not in order:
            order.append(role)
    calls.sort(key=lambda c: (order.index(c["role"]), c.get("run_ts") or "", c["call_id"]))
    if write:
        os.makedirs(orstudy.STUDY_DIR, exist_ok=True)
        _write(order_path, order)
        _write(os.path.join(orstudy.STUDY_DIR, "population.json"), {c["call_id"]: c["role"] for c in calls})
    return calls, {"cut_off_prompts": cut_off, "left_out": left}


def _population() -> dict:
    from scout import orstudy
    try:
        return json.load(open(os.path.join(orstudy.STUDY_DIR, "population.json")))
    except Exception:
        return {}


def _median(v):
    v = sorted(v)
    return v[len(v) // 2] if v else None


def _table(rows: list, pop: dict, models: list) -> dict:
    """Per job: the models that have attempted at least COVERAGE of the job's calls, the calls all of
    them attempted, each one's same-decision count on exactly those calls, and what a constant answer
    would score on them."""
    from scout import orstudy
    out = {}
    for role in sorted(set(pop.values())):
        role_calls = {cid for cid, r in pop.items() if r == role}
        got = {m: {r["call_id"]: r for r in rows if r["backend"] == m and r["call_id"] in role_calls} for m in models}
        inn = [m for m in models if got[m] and len(got[m]) >= orstudy.COVERAGE * len(role_calls)]
        common = sorted(set.intersection(*[set(got[m]) for m in inn])) if inn else []
        cells = {}
        for m in inn:
            sc = [orstudy.score_row(got[m][cid]) for cid in common]
            cells[m] = {"same": sum(s["same"] for s in sc), "n": len(sc),
                        "no_answer": sum(1 for s in sc if s["why"].startswith("no answer")),
                        "unreadable": sum(1 for s in sc if s["why"] == "unreadable format"),
                        "missed": sum(1 for s in sc if s["why"] == "missed it"),
                        "false_alarm": sum(1 for s in sc if s["why"] == "false alarm"),
                        "found": sum(s.get("found") or 0 for s in sc), "loud": sum(s.get("loud") or 0 for s in sc)}
        base = orstudy.baselines(role, [got[inn[0]][cid] for cid in common]) if inn else {}
        out[role] = {"calls": len(role_calls), "common": len(common), "cells": cells, "baselines": base,
                     "behind": {m: len(got[m]) for m in models if m not in inn}}
    return out


def report(say=print) -> None:
    from scout import orstudy
    rows, pop = orstudy.load_results(), _population()
    hidden = sum(1 for r in rows if r["call_id"] not in pop)
    rows = [r for r in rows if r["call_id"] in pop]
    att = orstudy.load_attempts()
    models = sorted({r["backend"] for r in rows} | {"or:" + a["model"] for a in att})
    if not rows:
        say("no stored results yet"); return
    say(f"stored results: {len(rows)}  models tried: {len(models)}  calls in the study: {len(pop)}"
        + (f"  ({hidden} stored results belong to calls no longer in the study and are not shown)" if hidden else ""))
    say("\nSAME DECISION AS PRODUCTION, per job. Counted on the calls every compared model attempted;")
    say("a call the model failed to answer, or answered in an unreadable format, counts against it.")
    say("Read each score against the 'constant answer' lines: a model no better than those has learned nothing about the job.")
    for role, t in _table(rows, pop, models).items():
        say(f"\n  {role}: {t['calls']} calls in the study, {t['common']} attempted by all {len(t['cells'])} compared models"
            + ("  (too few calls to read much into)" if 0 < t["common"] < 20 else ""))
        for name, n in t["baselines"].items():
            say(f"    {'constant answer: ' + name:46} {(f'{100 * n / t['common']:.0f}%' if t['common'] else '--'):>4}  {n:3}/{t['common']:<3}")
        for m, c in sorted(t["cells"].items(), key=lambda kv: -kv[1]["same"]):
            pct = f"{100 * c['same'] / c['n']:.0f}%" if c["n"] else "--"
            extra = (f"; missed {c['missed']}, false alarm {c['false_alarm']}; named {c['found']} of the {c['loud']} findings production flagged"
                     if role in orstudy.LOUD else "")
            say(f"    {m[3:][:46]:46} {pct:>4}  {c['same']:3}/{c['n']:<3} same; no answer {c['no_answer']}, unreadable {c['unreadable']}{extra}")
        for m, n in sorted(t["behind"].items(), key=lambda kv: -kv[1]):
            say(f"    {m[3:][:46]:46} not compared yet: {n} of {t['calls']} calls attempted")
    say("\nPER MODEL: stored, answered, why not, median seconds, answers with a reasoning trace, how the answer arrived, who served it")
    for backend in models:
        cell = [r for r in rows if r["backend"] == backend]
        if not cell:
            why = Counter(a.get("reason") for a in att if "or:" + a["model"] == backend)
            say(f"  {backend[3:][:46]:46} never answered; requests: {dict(why)}")
            continue
        ok = [r for r in cell if r["status"] == "ok"]
        why = Counter(r.get("reason") for r in cell if r["status"] != "ok")
        via = Counter((r.get("backend_version") or {}).get("answer_via") for r in ok)
        served = Counter(f"{(r.get('backend_version') or {}).get('served_model')} via {(r.get('backend_version') or {}).get('served_by')}" for r in cell
                         if (r.get("backend_version") or {}).get("served_by"))
        med = _median([r["duration_ms"] for r in ok if r.get("duration_ms")])
        say(f"  {backend[3:][:46]:46} stored={len(cell)} answered={len(ok)} not_answered={dict(why)} "
            f"median={(med or 0) / 1000:.1f}s traces={sum(1 for r in ok if r.get('thinking'))} via={dict(via)} "
            f"billed=${sum(r.get('cost_usd') or 0 for r in cell):.4f}")
        if backend == "or:" + orstudy.ROUTER or len(served) > 1:
            for k, n in served.most_common(12):
                say(f"      {n:4}  {k}")
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
    for role, why in sorted(notes["left_out"].items()):
        say(f"  note  {role}: left out of the study: " + ", ".join(f"{n} calls ({w})" for w, n in why.items()))
    say(f"  note  {notes['cut_off_prompts']} captured calls left out: their stored prompt is cut off")
    # 1. the grader: a non-answer is never the same decision, on any job, whatever production decided
    wrong = []
    for role in roles:
        for c in [c for c in calls if c["role"] == role][:40]:
            for garbage in ("I am not sure what you want.", "{}", '{"note": "nothing to report"}', ""):
                cmp_ = modelcompare.compare_call(c, {"status": "ok", "text": garbage, "backend": "control"})
                if orstudy.score(cmp_, role, fmt_ok=orstudy.format_ok(role, garbage), text=garbage)["same"]:
                    wrong.append(f"{role} on {garbage!r}")
    check("the grader never scores a non-answer as the same decision", not wrong, ", ".join(sorted(set(wrong))))
    need = 40 if PAID["on"] else 60
    check("the study covers enough of Scout's jobs", len(roles) >= 2 and len(calls) >= need, f"{len(roles)} jobs, {len(calls)} calls: {', '.join(roles)}")
    tooled = [c for c in calls if c.get("tools")]
    check("every call production made with a forced tool is replayed with that tool", all(orstudy.forced_tool(c) for c in tooled), f"{len(tooled)} such calls")
    from scout import config
    check("the failure email can be sent", bool(config.ALERT_EMAIL_TO), "recipient configured" if config.ALERT_EMAIL_TO else "no recipient configured")
    if not rows:
        check("results exist", False, "nothing stored yet")
        return fails
    att = orstudy.load_attempts()
    # 2. money
    if PAID["on"]:
        d = orstudy.spend()
        logged = sum(a.get("cost") or 0 for a in att)
        check("spending is inside the limit", orstudy.spent(d) <= orstudy.PAID_BUDGET, f"${orstudy.spent(d):.2f} of ${orstudy.PAID_BUDGET:.0f}")
        check("the study's ledger matches the bills on its own replies", abs(d["billed"] - logged) < 0.01, f"ledger ${d['billed']:.4f}, replies ${logged:.4f}")
        acct = (d["account_now"] - d["account_start"]) if d["account_start"] is not None and d["account_now"] is not None else None
        check("OpenRouter's own figure for the key agrees with the ledger", acct is not None and abs(acct - d["billed"]) <= max(0.25, 0.05 * d["billed"]),
              f"OpenRouter ${acct:.4f}, ledger ${d['billed']:.4f}, unseen-bill allowance ${d['assumed']:.4f}" if acct is not None else "the account figure could not be read")
        over = [a for a in att if a.get("reason") == "billed_above_listed_price"]
        check("no reply was billed above the listed price", not over, f"{len(over)} such replies")
        ends = Counter(str((r.get("backend_version") or {}).get("endpoint")) for r in rows)
        want = {m["tag"] for m in orstudy.PAID_MODELS}
        check("every stored answer came through an approved endpoint", set(ends) <= want, str(dict(ends)))
    else:
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
        if r["status"] == "ok" and r["backend"] != "or:" + orstudy.ROUTER and not str(v.get("served_model") or "").startswith(str(v.get("model"))):
            off[f"{v.get('model')} answered by {v.get('served_model')}"] += 1
    check("each named model was answered by that model", not off, str(dict(off)) if off else "")
    # 5. settings, from the stored rows: no schema anywhere; reasoning actually happened where asked
    check("no output schema was sent to any model", not any(r.get("schema_enforced") for r in rows))
    for backend in sorted({r["backend"] for r in rows}):
        ok = [r for r in rows if r["backend"] == backend and r["status"] == "ok"]
        asked = [r for r in ok if (r.get("backend_version") or {}).get("reasoning_requested")]
        traced = sum(1 for r in asked if r.get("thinking") or ((r.get("tokens") or {}).get("thinking") or 0) > 0)
        say(f"        {backend[3:][:46]:46} answered={len(ok)} reasoning asked={len(asked)} seen={traced}")
    untooled = sum(1 for r in rows if r["status"] == "ok" and any(c["call_id"] == r["call_id"] and c.get("tools") for c in calls)
                   and not (r.get("backend_version") or {}).get("forced_tool"))
    check("no stored answer to a forced-tool call was made without the tool", untooled == 0, f"{untooled} such rows")
    # 6. nothing stored that is not a model result
    transient = Counter(r.get("reason") for r in rows if r.get("reason") in ("rate_limited", "network", "timeout", "broken_response", "provider_error", "upstream_error", "model_refused"))
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
    # the counter may run ahead (a request in flight when a run was stopped is counted, never logged);
    # it must never run behind, or the daily cap is not being enforced
    gap = orstudy.used_today() - sent
    check("every request sent today was counted against the daily cap", 0 <= gap <= 25, f"counter {orstudy.used_today()}, logged {sent}")
    # 9. is it getting anywhere
    tried = {a["model"] for a in att}
    answered = {r["backend"][3:] for r in rows if r["status"] == "ok"}
    check("at least half the models tried are answering", len(answered) * 2 >= len(tried), f"{len(answered)} of {len(tried)}")
    return fails


SEVERE = ("billed", "approved provider", "stopped for all", "could not start or died")


def _tell(lines: list) -> None:
    """One plain email a day, marked as sent only when it was. A charge, an unapproved provider, a
    stop for all models or a dead run is sent even if an ordinary failure was already sent today."""
    from scout import orstudy
    mark = os.path.join(orstudy.STUDY_DIR, "told.json")
    day = time.strftime("%Y-%m-%d")
    severe = any(k in line for line in lines for k in SEVERE)
    try:
        told = json.load(open(mark))
    except Exception:
        told = {}
    if told.get("day") == day and (told.get("severe") or not severe):
        return
    try:
        from scout import notify
        body = ("The OpenRouter study needs a look before its numbers are used.\n\n" + "\n".join("- " + f for f in lines)
                + "\n\nNothing was billed unless a line above says so. The log is ~/scout-replay/openrouter_study.log.")
        res = notify._dispatch("Scout: the OpenRouter study needs a look", body, dry_run=False)
        if isinstance(res, dict) and res.get("sent") is False:
            print(f"  the study email was not sent: {res.get('reason')}", file=sys.stderr); return
        _write(mark, {"day": day, "severe": severe or bool(told.get("day") == day and told.get("severe"))})
    except Exception as e:
        print(f"  could not send the study email ({type(e).__name__})", file=sys.stderr)


def router_check(n: int, say=print) -> None:
    """Harmless prompts only. Asks the free router for answers while allowing one provider; if every
    answer comes from that provider, the router honours the allow-list the study relies on."""
    from scout import orstudy
    if _other_run_alive(os.path.join(orstudy.STUDY_DIR, "running.pid")):
        say("a study run is active; the router check waits for it to end"); return
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
    cat = orstudy.paid_entries() if PAID["on"] else orstudy.catalogue()
    entries = cat["models"]
    if args.models:
        want = set(args.models.split(","))
        entries = [e for e in entries if e["model"] in want]
    if args.no_router:
        entries = [e for e in entries if not e.get("router")]
    calls, notes = _load_calls(args.since, write=args.run)
    left = max(0, orstudy.DAILY_CAP - orstudy.used_today())
    print(f"{datetime.now():%Y-%m-%d %H:%M} study: {len(entries)} models, {len(calls)} calls since {args.since}; "
          f"requests used today {orstudy.used_today()}, left {left}")
    print("  jobs: " + ", ".join(f"{r} {n}" for r, n in Counter(c["role"] for c in calls).items()))
    for d in cat["dropped"]:
        print(f"  left out: {d['model']}: {d['why']}")
    if PAID["on"]:
        d = orstudy.open_ledger(os.environ.get("OPENROUTER_API_KEY") or "") if args.run else orstudy.spend()
        print(f"  money: ${orstudy.spent(d):.2f} spent of the ${orstudy.PAID_BUDGET:.0f} limit "
              f"(replies ${d['billed']:.4f}, unseen-bill allowance ${d['assumed']:.4f}); OpenRouter's figure for the key "
              + ("read" if d["account_now"] is not None else "NOT READ"))
        if args.run and d["account_now"] is None:
            raise RuntimeError("OpenRouter's usage figure for the key could not be read; a paid run does not start without it")
    todo = {}
    for e in entries:
        have = orstudy.have(e["model"])
        todo[e["model"]] = sum(1 for c in calls if orstudy.slug(c["call_id"]) not in have)
        print(f"  {e['model'][:50]:50} to do {todo[e['model']]:4}  via {','.join(e['providers'])} "
              f"reasoning={'asked' if e['reasoning'] else 'n/a'} output room={min(orstudy.MAX_OUT, e['max_out']) if e.get('max_out') else 'provider default'}"
              + (f" price ${e['price'][0]:.2f}/${e['price'][1]:.2f}" if e.get("paid") else ""))
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
        resting, totals, crashed, sent = {}, Counter(), [], 0
        for rnd in (1, 2, 3):
            if rnd > 1 and any(w in ("rate_limited", "upstream_error", "network") for w in resting.values()):
                time.sleep(180)                                  # a rested model gets another go, a few minutes later
                resting = {m: w for m, w in resting.items() if w not in ("rate_limited", "upstream_error", "network")}
            active = [e for e in entries if e["model"] not in resting and todo[e["model"]] > 0]
            left = max(0, orstudy.DAILY_CAP - orstudy.used_today())
            if not active or orstudy.stop_reason() or should_stop():
                break
            quota = args.per_model if args.per_model is not None else (10 ** 6 if PAID["on"] else left // len(active))
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
                sent += r["attempts"]
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
        if why and not any(k in why for k in ("daily_cap", "account_daily_limit", "budget")):
            fails.append("stopped for all models: " + why)
        expected = ("rate_limited", "model_refused", "upstream_error")       # known, reported in the read-out, not an alarm
        odd = {m: w for m, w in resting.items() if w not in expected and not w.startswith("crashed")}
        if sum(totals.values()) == 0 and (odd or (sent == 0 and sum(todo.values()) > 0 and not why)):
            fails.append(f"this run stored nothing; requests sent {sent}; models rested: {odd or resting}")
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
    ap.add_argument("--since", default=None)
    ap.add_argument("--paid", action="store_true", help="the paid judging section (own folder, own model list, hard spending limit)")
    ap.add_argument("--max-minutes", type=int, default=240)
    ap.add_argument("--deadline", default=None, help="HH:MM local; stop cleanly")
    args = ap.parse_args()
    from scout import orstudy
    if args.paid:
        PAID["on"] = True
        orstudy.STUDY_DIR = orstudy.PAID_DIR
        orstudy.DAILY_CAP = 10 ** 6            # the 1,000-a-day limit is for free models only
        orstudy.MIN_GAP_S = 1.0
    args.since = args.since or (orstudy.PAID_SINCE if args.paid else STUDY_SINCE)
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
