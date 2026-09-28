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


def build(now: datetime) -> tuple[dict, str]:
    from scout import modelcompare, selfserve
    from scripts.replay_calls import list_bundles, load_calls  # noqa
    results = modelcompare.load_results()
    labels = modelcompare.load_labels()
    sc = modelcompare.scorecard(results, labels)
    bundles = []
    for p in list_bundles(None)[-60:]:
        raw = selfserve.read_data(p)
        if raw:
            try:
                bundles.append(json.loads(raw))
            except json.JSONDecodeError:
                pass
    cost = modelcompare.cost_view(bundles)
    cells_out, lines = {}, []
    lines.append(f"# On-device model comparison check-in — {now.date()}")
    lines.append(f"results={len(results)}  labels={len(labels)}  backends={sc['backends']}  "
                 f"common population n={sc['common_n']}  bar={BAR - MARGIN} "
                 f"({'parity' if not MARGIN else f'margin -{MARGIN}'}); metric = candidate-right vs the live model on "
                 f"human-adjudicated disagreements\n")
    lines.append(f"| backend | role | pop | n | coverage | parse_ok | refusal | agree | kappa | disagreements | "
                 f"adjudicated | precision | CI95 | costly (cand/ref) | p50 ms | verdict |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for key, cell in sc["cells"].items():
        period_key = json.dumps(sorted((cell["full"].get("periods") or {}).keys()))
        prior = _prior(key)
        if prior and prior.get("period_key") != period_key:
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
    if sc.get("modes"):
        lines.append("\n## Tools-on roles: loop / frozen / repeat (never pooled with exact)")
        for k, c in sc["modes"].items():
            lines.append(f"- {k}: n={c['n_results']} coverage={c['coverage']} parse_ok={c['parse_ok']} "
                         f"agree={c['agreement_rate']} kappa={c['kappa_candidate_vs_reference']} "
                         f"disagreements={c['disagreements']} adjudicated={c['adjudicated']} precision={c['precision']}")
    lines.append("\n## Cost view (live $ per role, last window; local backends $0)")
    for r, usd in cost["projected_monthly_usd_by_role"].items():
        lines.append(f"- {r}: ~${usd}/month live")
    lines.append(f"\nreference self-agreement ceiling: unmeasured (optional paid re-run, on Uroš's go only)")
    lines.append("Verdict vocabulary shared with the other lanes; ELIGIBLE = non-inferior and sustained; "
                 "no production switch exists. Bars: docs/model-substitution-exit-criteria.md")
    snapshot = {"stamp": now.isoformat(timespec="seconds"), "bar": BAR - MARGIN, "margin": MARGIN,
                "results": len(results), "labels": len(labels), "common_n": sc["common_n"],
                "cells": cells_out, "modes": sc.get("modes"), "cost": cost}
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
