"""Stamp `source_class` on every existing card (WS0, 2026-09-28). Deterministic, $0, no model.

    python scripts/backfill_source_class.py            # dry run: what would change, per card
    python scripts/backfill_source_class.py --write    # write claims.json (battlecards/ + archive/)

Runs on MAIN, never on rc (rc-guard refuses card edits there): this is a content commit to the
cards, like a monitor commit. It writes only claims.json (current.md and its Cut Log untouched) and
never makes a claim invalid (classify.stamp keeps the model's tier when the class's tier would
break the schema, recording `source_class_conflict` instead). Re-running is a no-op.
"""
import argparse
import copy
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scout import config, schema  # noqa: E402
from scout.sources import classify  # noqa: E402


def cards():
    for root in ("battlecards", "archive"):
        for f in sorted(glob.glob(os.path.join(config.APP_ROOT, root, "*", "claims.json"))):
            yield f


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()
    total_changed = 0
    for f in cards():
        meta = json.load(open(f.replace("claims.json", "meta.json")))
        before = json.load(open(f))
        after = classify.stamp_all(copy.deepcopy(before), meta)
        changed = sum(1 for a, b in zip(before, after) if a != b)
        tiers = sum(1 for a, b in zip(before, after) if a.get("source_tier") != b.get("source_tier"))
        conflicts = sum(1 for b in after if b.get("source_class_conflict"))
        newly_invalid = sum(1 for a, b in zip(before, after)
                            if b.get("status") != "retired" and schema.validation_errors(b) and not schema.validation_errors(a))
        counts = classify.class_counts(after)
        slug = f.split(os.sep)[-2]
        print(f"{slug:58} changed={changed:3} tiers={tiers} conflicts={conflicts} invalid={newly_invalid}  {counts}")
        if newly_invalid:
            print("  !! refusing: the stamp would invalidate a claim", file=sys.stderr)
            return 1
        total_changed += changed
        if args.write and changed:
            with open(f, "w") as fh:
                json.dump(after, fh, indent=2, ensure_ascii=False)
    print(f"\n{'wrote' if args.write else 'would change'} {total_changed} claims" + ("" if args.write else "  (add --write)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
