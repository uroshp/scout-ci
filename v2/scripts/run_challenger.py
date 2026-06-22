#!/usr/bin/env python3
"""Run the v3.5 shadow-eval CHALLENGER over captured champion records (docs/vnext-roadmap.md §v3.5,
decision-log §11). For each substantive shadow/<slug>/*.json record it runs the Sonnet challenger
(scout.challenger) over the captured evidence, compares to the code-grader champion, mines the
disagreements, and prints a scorecard (agreement, Cohen's kappa, recovery/slop candidates).

SPEND-SAFE by default: with no flags it ESTIMATES cost and runs NO model calls (guard-API-spend).
Pass --run to actually call the challenger, --write to persist results to the private store.

Run from v2/:
    python scripts/run_challenger.py                      # estimate only — no API spend
    python scripts/run_challenger.py --run --limit 1      # judge ONE record (cheap verification)
    python scripts/run_challenger.py --run --write        # judge all, persist results
    python scripts/run_challenger.py --run --write --slug perplexity__vs__google__general
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scout import challenger, config, selfserve, shadow

# Batman/Superman is the deliberate nonsense stress-test, not real CI — keep it out of the eval set.
EXCLUDE = {"batman__vs__superman__general"}
# Rough Sonnet ($3/$1M in, $15/$1M out) estimate, CALIBRATED to a measured run (~$0.27 for 33 claims
# = ~$0.008/claim; the SDK system prompt + adaptive thinking dominate, so a naive token count is ~7x
# low). Per-item figures back into ~$0.008/claim; the real cost is always read off the SDK per run.
_IN_PER_ITEM, _OUT_PER_ITEM, _FIXED_IN = 600, 400, 2000


def _est_usd(n_items: int) -> float:
    return ((_FIXED_IN + n_items * _IN_PER_ITEM) * 3.0 + n_items * _OUT_PER_ITEM * 15.0) / 1_000_000


def list_records(slug_filter: str | None) -> list[dict]:
    """Every substantive captured champion record (kept or cut non-empty), newest first within card.
    Uses include_dirs=True so the per-card subdirs are visible on the GitHub backend."""
    out = []
    slugs = [slug_filter] if slug_filter else selfserve.list_data(shadow.SHADOW_DIR, include_dirs=True)
    for slug in slugs:
        if slug in EXCLUDE or slug.endswith(".json"):
            continue
        for fn in sorted(selfserve.list_data(f"{shadow.SHADOW_DIR}/{slug}")):
            if not fn.endswith(".json"):
                continue
            raw = selfserve.read_data(f"{shadow.SHADOW_DIR}/{slug}/{fn}")
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if (rec.get("kept") or rec.get("cut")):     # skip empty shells (quiet monitor checks)
                rec.setdefault("slug", slug)
                out.append(rec)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the shadow-eval challenger (Sonnet) over captured champion records.")
    ap.add_argument("--run", action="store_true", help="actually call the challenger (spends API budget)")
    ap.add_argument("--write", action="store_true", help="persist results to the private store (implies --run)")
    ap.add_argument("--limit", type=int, default=None, help="judge at most N records")
    ap.add_argument("--slug", default=None, help="only this card slug")
    ap.add_argument("--force", action="store_true", help="re-judge records that already have a result")
    ap.add_argument("--variant", choices=["adversarial", "neutral"], default="neutral",
                    help="challenger system prompt; neutral is the default (decision-log §11), adversarial is the A/B alternate")
    args = ap.parse_args()
    run = args.run or args.write

    records = list_records(args.slug)
    if not args.force:
        records = [r for r in records if not challenger.already_judged(r["slug"], challenger._content_hash(r))]
    if args.limit is not None:
        records = records[:args.limit]

    n_items = sum(len(r.get("kept") or []) + len(r.get("cut") or []) for r in records)
    print(f"Challenger model: {config.CHALLENGER_MODEL}   backend: "
          f"{'PRIVATE store' if selfserve.use_github() else 'LOCAL fs'}")
    print(f"Records to judge: {len(records)}   total claims: {n_items}   "
          f"rough estimate: ${_est_usd(n_items):.2f}  (real cost read from the SDK per run)\n")
    for r in records:
        print(f"  {r['slug']:60} src={r.get('source'):9} kept={len(r.get('kept') or []):>3} "
              f"cut={len(r.get('cut') or []):>2}  ~${_est_usd(len(r.get('kept') or []) + len(r.get('cut') or [])):.3f}")

    if not run:
        print("\nESTIMATE ONLY — no model calls made. Re-run with --run (and --write to persist).")
        return

    print(f"\n--- running challenger (variant: {args.variant}) ---")
    results = []
    for r in records:
        judged = challenger.judge_record(r, variant=args.variant)
        comparison = challenger.compare(r, judged)
        result = challenger.result_record(r, judged, comparison)
        results.append(result)
        s = result["summary"]
        print(f"  {r['slug']:50} cost=${(judged.get('cost_usd') or 0):.3f}  "
              f"agree={s['agree']}/{s['judged']} κ={s['kappa_champion_vs_challenger']}  "
              f"recoveries={s['recovery_candidates']} slop={s['slop_candidates']}")
        if args.write:
            challenger.persist(result)

    sc = challenger.scorecard(results)
    print(f"\n=== SCORECARD ({sc['records']} records, {sc['items_judged']} claims judged) ===")
    print(f"  agreement: {sc['agree']}/{sc['items_judged']} ({sc['agreement_rate']})   "
          f"kappa (champion vs challenger): {sc['kappa_champion_vs_challenger']}")
    print(f"  disagreements: {sc['disagreements']}  "
          f"(recovery candidates {sc['recovery_candidates']}, slop candidates {sc['slop_candidates']})")
    print(f"  total cost: ${sc['cost_usd']}")
    print(f"  adjudication (PROMOTION metric): {sc['adjudication']['adjudicated']}/{sc['disagreements']} "
          f"disagreements human-labeled; kappa (challenger vs human): "
          f"{sc['adjudication']['kappa_challenger_vs_human']}  <- needs human adjudication")
    if sc["pending_disagreements"]:
        print(f"\n  DISAGREEMENTS awaiting human adjudication ({len(sc['pending_disagreements'])}):")
        for d in sc["pending_disagreements"][:25]:
            print(f"    [{d.get('delta_id')}] {d.get('direction',''):18} {(d.get('slug') or '?')[:28]:28} "
                  f"champ={d.get('champion')}/chal={d.get('challenger')}  {str(d.get('claim'))[:70]}")
    if not args.write:
        print("\n(results NOT persisted — re-run with --write to save them to the private store)")


if __name__ == "__main__":
    main()
