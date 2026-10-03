"""On-device model comparison check-in (2026-09-28, plan section E; bars in
docs/model-substitution-exit-criteria.md).

Prints the backend x role table (full + common populations), slices, loop/frozen/repeat rows, the
cost view, and a verdict per cell using the SAME rule as the other lanes (scout/evalrule.py) with
this lane's own bar (0.50 parity, margin printed if one is ever written down). It prints on every
run; it SNAPSHOTS (advances the trend/streak) only with --snapshot, which the 1st/15th launchd entry
passes and the nightly run.sh never does.

    python scripts/model_checkin.py                 # print
    python scripts/model_checkin.py --snapshot      # print + snapshot to model_checkin/<stamp>.json
    python scripts/model_checkin.py --email         # also email the report (creds permitting)
"""
import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

CHECKIN_DIR = "model_checkin"
BAR = 0.50
MARGIN = 0.0                      # a $0-justified margin is a number Uroš writes here (e.g. 0.10)
MIN_ADJUDICATED = 15
ELIGIBLE_COVERAGE = 0.9     # an arm must run at least this share of the exact calls to be compared


def _load_env():
    p = os.path.expanduser("~/scout-replay/env")
    if os.path.exists(p):
        for line in open(p):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _prior(key: str) -> dict | None:
    from scout import selfserve
    files = sorted(f for f in (selfserve.list_data(CHECKIN_DIR) or []) if f.endswith(".json"))
    for f in reversed(files):
        raw = selfserve.read_data(f"{CHECKIN_DIR}/{f}")
        if not raw:
            continue
        try:
            snap = json.loads(raw)
        except json.JSONDecodeError:
            continue
        cell = (snap.get("cells") or {}).get(key)
        if cell:
            return cell
    return None


def _recent_bundles(limit: int = 60) -> list:
    """The last `limit` call bundles (for the cost view). Sibling-script import: scripts/ is on
    sys.path both when run directly and when eval_checkin imports this module."""
    from scout import selfserve
    try:
        from replay_calls import list_bundles
    except ImportError:
        from scripts.replay_calls import list_bundles  # noqa
    bundles = []
    for p in list_bundles(None)[-limit:]:
        raw = selfserve.read_data(p)
        if raw:
            try:
                bundles.append(json.loads(raw))
            except json.JSONDecodeError:
                pass
    return bundles


def _normalize_period_key(key: str | None, role: str) -> str | None:
    """Periods are keyed per role on the instructions a call received (2026-10-03). A snapshot
    written before that carries keys without the suffix; a baseline suffix (the role's recorded
    baseline fingerprint, or 'baseline') means the same period as those, so it is dropped before
    comparing. A changed-instructions suffix stays, and opens a new period, as intended."""
    if not key:
        return key
    try:
        parts = json.loads(key)
    except Exception:
        return key
    from scout import modelcompare as _mc
    base = _mc._baseline_instructions().get(role or "")
    out = []
    for p in parts:
        if "+" in p:
            head, suffix = p.rsplit("+", 1)
            if suffix == "baseline" or (base and suffix == f"instr:{base}"):
                p = head
        out.append(p)
    return json.dumps(sorted(out))


def _same_period(prior_key, period_key, role) -> bool:
    return _normalize_period_key(prior_key, role) == _normalize_period_key(period_key, role)


def build(now: datetime) -> tuple[dict, str]:
    from scout import modelcompare
    results = modelcompare.load_results()
    labels = modelcompare.load_labels()
    sc = modelcompare.scorecard(results, labels)
    cost = modelcompare.cost_view(_recent_bundles())
    cells_out, lines = {}, []
    lines.append(f"# On-device model comparison check-in — {now.date()}")
    lines.append(f"results={len(results)}  labels={len(labels)}  backends={sc['backends']}  "
                 f"common population n={sc['common_n']}  bar={BAR - MARGIN} "
                 f"({'parity' if not MARGIN else f'margin -{MARGIN}'}); metric = candidate-right vs the live model on "
                 f"human-adjudicated disagreements\n")
    # Arms (2026-09-30): every configured local arm, its vendor + tag, how many results it left
    # TODAY, and whether it is still warming up (below the common-population threshold). An arm
    # with nothing today reads "did not run"; the lanes canary looks for that line.
    from scout import replaybackends as _rb
    today = now.date().isoformat()
    lines.append("## Arms")
    arms = [("apple_ondevice", "Apple", "SystemLanguageModel")] + \
           [(name, cfg["vendor"], cfg["tag"]) for name, cfg in _rb.OLLAMA_MODELS.items()]
    for name, vendor, tag in arms:
        n_all = sum(1 for r in results if r.get("backend") == name)
        n_today = sum(1 for r in results if r.get("backend") == name and str(r.get("replayed_at", ""))[:10] == today)
        state = "did not run today" if n_today == 0 else f"{n_today} today"
        warm = "  (warming up: below the common-population threshold)" if name in (sc.get("warming_up") or []) else ""
        lines.append(f"- {name}: {vendor} {tag}: {n_all} results, {state}{warm}")
    lines.append("")
    # brief-ready summary (2026-10-01): what the executive brief needs, without re-reading results
    brief_arms = []
    for name, vendor, tag in arms:
        rows = [r for r in results if r.get("backend") == name]
        exact = [r for r in rows if r.get("mode", "exact") == "exact" and not r.get("rep")]
        ok = [r for r in exact if r.get("status") == "ok"]
        # Comparable numbers come from the COMMON population (the calls every established arm
        # attempted, one settings period each); an arm that ran under ELIGIBLE_COVERAGE of them is
        # ineligible for comparison (Uroš 2026-10-01: "different dates, different sets are not
        # eligible"; Apple, at ~38%, is out until Private Cloud Compute).
        judge = sc["cells"].get(f"{name}|judge", {}).get("common") or {}
        cov = round(len(ok) / len(exact), 2) if exact else None
        brief_arms.append({"backend": name, "vendor": vendor, "tag": tag, "results": len(rows),
                           "today": sum(1 for r in rows if str(r.get("replayed_at", ""))[:10] == today),
                           "exact_coverage": cov,
                           "judge_agree": judge.get("agreement_rate"), "judge_n": judge.get("n_results"),
                           "judge_p50_ms": judge.get("latency_ms_p50"),
                           "warming_up": name in (sc.get("warming_up") or []),
                           "eligible": bool(cov is not None and cov >= ELIGIBLE_COVERAGE and name not in (sc.get("warming_up") or []))})
    brief = {"results": len(results), "labels": len(labels), "common_n": sc["common_n"], "arms": brief_arms,
             "eligible_coverage": ELIGIBLE_COVERAGE}
    lines.append(f"| backend | role | pop | n | coverage | parse_ok | refusal | agree | kappa | disagreements | "
                 f"adjudicated | precision | CI95 | costly (cand/ref) | p50 ms | verdict |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for key, cell in sc["cells"].items():
        period_key = json.dumps(sorted((cell["full"].get("periods") or {}).keys()))
        prior = _prior(key)
        if prior and not _same_period(prior.get("period_key"), period_key, cell["role"]):
            prior = None
            new_period = True
        else:
            new_period = False
        v = modelcompare.verdict_for_cell(cell, prior, bar=BAR - MARGIN, min_adjudicated=MIN_ADJUDICATED)
        for pop in ("full", "common"):
            c = cell[pop]
            lines.append(f"| {cell['backend']} | {cell['role']} | {pop} | {c['n_results']} | {c['coverage']} | "
                         f"{c['parse_ok']} | {c['refusal_rate']} | {c['agreement_rate']} | "
                         f"{c['kappa_candidate_vs_reference']} | {c['disagreements']} | {c['adjudicated']} | "
                         f"{c['precision']} | {c['precision_ci95']} | {c['costly_error_rate']}/{c['reference_costly_error_rate']} | "
                         f"{c['latency_ms_p50']} | {v['status'] if pop == 'full' else ''}"
                         f"{' (new period, no prior)' if pop == 'full' and new_period else ''} |")
        cells_out[key] = {"backend": cell["backend"], "role": cell["role"], "period_key": period_key,
                          "adjudicated": cell["full"]["adjudicated"], "precision": cell["full"]["precision"],
                          "coverage": cell["full"]["coverage"], "parse_ok": cell["full"]["parse_ok"],
                          "verdict": v["status"], "note": v["note"], "no_improve_streak": v["no_improve_streak"],
                          "coverage_ok": v["coverage_ok"], "reliability_ok": v["reliability_ok"]}
    floor = modelcompare.repeat_floor(results)
    if sc.get("modes"):
        lines.append("\n## Tools-on roles: loop / frozen / repeat (never pooled with exact)")
        lines.append("frozen = judgment only on the live run's retrieval (comparable to exact-mode kappa); "
                     "loop = the local model searched and fetched itself; loop minus frozen = retrieval attribution.")
        for k, c in sc["modes"].items():
            lines.append(f"- {k}: n={c['n_results']} coverage={c['coverage']} parse_ok={c['parse_ok']} "
                         f"agree={c['agreement_rate']} kappa={c['kappa_candidate_vs_reference']} "
                         f"disagreements={c['disagreements']} adjudicated={c['adjudicated']} precision={c['precision']}"
                         + (f" tool_failure={c.get('tool_failure_rate')} misses={c.get('miss_kinds')}" if "|loop" in k else ""))
            for sl_name in ("by_tool_protocol", "by_replay_lag"):
                for sk, sv in (c.get(sl_name) or {}).items():
                    lines.append(f"    {sl_name[3:]}={sk}: n={sv['n_results']} agree={sv['agreement_rate']} "
                                 f"kappa={sv['kappa_candidate_vs_reference']} precision={sv['precision']}")
        if floor:
            lines.append("  local-vs-local repeat floor (a loop-vs-live gap inside this is not a model effect):")
            for k, f in floor.items():
                lines.append(f"    {k}: self-agreement {f['self_agreement']} over {f['pairs']} item pairs")
    lines.append("\nLoop-mode floor: human-confirmed material items the candidate missed ≈ 0 "
                 "(live-scored miss rate above is a labelled proxy until adjudicated).")
    lines.append("\n## Cost view (live $ per role, last window; local backends $0)")
    for r, usd in cost["projected_monthly_usd_by_role"].items():
        lines.append(f"- {r}: ~${usd}/month live")
    lines.append(f"\nreference self-agreement ceiling: unmeasured (optional paid re-run, on Uroš's go only)")
    lines.append("Verdict vocabulary shared with the other lanes; ELIGIBLE = non-inferior and sustained; "
                 "no production switch exists. Bars: docs/model-substitution-exit-criteria.md")
    snapshot = {"brief": brief, "stamp": now.isoformat(timespec="seconds"), "bar": BAR - MARGIN, "margin": MARGIN,
                "results": len(results), "labels": len(labels), "common_n": sc["common_n"],
                "cells": cells_out, "modes": sc.get("modes"), "repeat_floor": floor, "cost": cost}
    return snapshot, "\n".join(lines)


def main():
    _load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", action="store_true")
    ap.add_argument("--email", action="store_true")
    args = ap.parse_args()
    from scout import selfserve
    if not selfserve.use_github():
        sys.exit("CREDS MISSING: SELFSERVE_GH_TOKEN / SELFSERVE_REPO not set")
    now = datetime.now()
    snap, body = build(now)
    print(body)
    if args.snapshot:
        path = f"{CHECKIN_DIR}/{now.strftime('%Y%m%dT%H%M%S')}.json"
        selfserve.write_data(path, json.dumps(snap, indent=1), f"model-checkin: snapshot {now.date()}")
        print(f"\nsnapshot -> {path}")
    if args.email:
        from scout import notify
        notify._dispatch(f"Scout on-device model check-in {now.date()}", body, dry_run=False)


if __name__ == "__main__":
    main()
