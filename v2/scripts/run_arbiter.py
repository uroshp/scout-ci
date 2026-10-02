"""Arbitrate pending on-device disagreements (the research layer, scout/arbiter.py). Runs on the
mini after the morning replay, or by hand. Caps: per-item (ARBITER_MAX_USD), per day (dollars and
items, research/arbiter_state.json). $ spend: Opus 5.5 via the API key.
Usage: python scripts/run_arbiter.py [--limit N] [--dry] [--role judge] [--searches 3]
"""
import argparse
import os
import sys

sys.path.insert(0, ".")
ENV_PATH = os.path.expanduser("~/scout-replay/env")
if os.path.exists(ENV_PATH):                       # the mini's creds (the replay runner's convention)
    for line in open(ENV_PATH):
        if "=" in line and not line.startswith("#"):
            k, v = line.strip().split("=", 1); os.environ.setdefault(k, v.strip().strip('"').strip("'"))
from scout import adjudicate_models, arbiter, ledger, modelcompare  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=arbiter.ARBITER_DAILY_ITEMS)
    ap.add_argument("--dry", action="store_true", help="list what would be arbitrated; no calls, no writes")
    ap.add_argument("--role", default="judge", help="role to arbitrate (default judge; 'all' for every role)")
    ap.add_argument("--searches", type=int, default=arbiter.ARBITER_SEARCHES)
    args = ap.parse_args()

    pend = adjudicate_models.pending(role=None if args.role == "all" else args.role)
    pend.sort(key=lambda p: (p.get("slug") or "", p.get("delta_id")))
    done = set()
    for p in pend:
        done |= arbiter.existing(str(p.get("run_ts") or "")[:7]) if p.get("run_ts") else set()
    results = modelcompare.load_results()
    bundles = adjudicate_models._bundle_index()
    todo = []
    for p in pend:
        rec = bundles.get(p["call_id"]) or {}
        month = str(rec.get("run_ts") or "")[:7]
        if p["delta_id"] in arbiter.existing(month):
            continue
        todo.append((p, rec))
    print(f"[arbiter] pending {len(pend)}, not yet arbitrated {len(todo)}, this run up to {args.limit}")
    if args.dry:
        for p, rec in todo[:args.limit]:
            print(f"  {p['delta_id']}  {p['role']}  {p.get('slug')}  arms={sorted(p['candidates'])}")
        return 0
    L = ledger.Ledger(arbiter.ARBITER_STATE, arbiter.ARBITER_DAILY_USD, arbiter.ARBITER_MAX_USD)
    n = 0
    for p, rec in todo[:args.limit]:
        if not rec:
            print(f"  {p['delta_id']}: no captured call; skipped"); continue
        ok, state = L.start(arbiter.ARBITER_MAX_USD)
        if not ok:
            print(f"[arbiter] daily ceiling reached (${state.get('spend_usd', 0):.2f}); stopping"); break
        try:
            r = arbiter.arbitrate(p, rec, results, searches=args.searches)
            L.settle(r["cost_usd"], arbiter.ARBITER_MAX_USD)
            n += 1
            print(f"  {p['delta_id']}: {r['verdict']} (conf {r.get('confidence')}), best={r.get('best')}, "
                  f"searches={r['searches_used']}, ${r['cost_usd']:.2f}, {r['duration_ms'] // 1000}s")
        except Exception as e:
            L.settle(arbiter.ARBITER_MAX_USD, arbiter.ARBITER_MAX_USD)      # unknown cost: charge the cap
            print(f"  {p['delta_id']}: FAILED ({type(e).__name__}: {str(e)[:160]})", file=sys.stderr)
    print(f"[arbiter] {n} arbitrated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
