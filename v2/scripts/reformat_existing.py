#!/usr/bin/env python3
"""One-off cleanup: reformat the existing malformed (markerless) block claims already on the cards
via the no-drop repair path (scout.reformat) — the blob-rendering objections/plays published before
the render gate existed (decision-log §11). Substance unchanged: only the required **So what:** /
**Soundbite:** block is added. A claim that can't be repaired is HELD + flagged, never dropped.

Run from v2/:  python scripts/reformat_existing.py [--write] [slug ...]
Without --write it reformats and reports (still makes the model calls) but does not save.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scout import config, reformat, schema, store
from scout.render import claims_to_markdown, clean_output, extract_cut_log, format_report

EXCLUDE = {"batman__vs__superman__general"}


def _title(meta: dict) -> str:
    me, comp = meta.get("my_company"), meta.get("competitor")
    return (f"# Competitive Intelligence Brief: {me} vs {comp}" if me
            else f"# Competitive Intelligence Brief: {comp}")


def _slugs(args_slugs):
    if args_slugs:
        return args_slugs
    root = os.path.join(config.APP_ROOT, "battlecards")
    return [s for s in sorted(os.listdir(root))
            if s not in EXCLUDE and os.path.isfile(os.path.join(root, s, "claims.json"))]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="persist the reformatted cards")
    ap.add_argument("slugs", nargs="*")
    args = ap.parse_args()

    n_bad = sum(1 for slug in _slugs(args.slugs) for c in store.load_claims(slug)
                if c.get("status", "active") == "active" and schema.render_structure_errors(c))
    print(f"Malformed block claims to repair: {n_bad}   (~${n_bad * 0.02:.2f} in reformat calls)   "
          f"mode: {'WRITE' if args.write else 'DRY'}\n")

    held_total = 0
    for slug in _slugs(args.slugs):
        claims = store.load_claims(slug)
        bad = [c for c in claims if c.get("status", "active") == "active" and schema.render_structure_errors(c)]
        if not bad:
            continue
        print(f"{slug}")
        changed = False
        for c in bad:
            status, c2 = reformat.repair_or_hold(slug, c)
            print(f"   {str(c.get('subject_key'))[:46]:46} -> {status}")
            if status == "repaired":
                c["claim"] = c2["claim"]
                changed = True
            elif status == "held":
                held_total += 1   # left as-is on the card, flagged — NEVER dropped
        if changed and args.write:
            meta = store.load_meta(slug) or {}
            body = claims_to_markdown(claims, _title(meta), my_company=meta.get("my_company"),
                                      competitor=meta.get("competitor"))
            cur_path = os.path.join(store.battlecard_dir(slug), "current.md")
            cut = extract_cut_log(open(cur_path).read()) if os.path.exists(cur_path) else ""
            if cut:
                body = body.rstrip() + "\n\n" + cut
            store.write_baseline(slug, claims, meta, format_report(clean_output(body)))
            print("   -> wrote baseline")

    # final assertion: no malformed block claims remain (held ones are flagged, the rest repaired)
    remaining = sum(1 for slug in _slugs(args.slugs) for c in store.load_claims(slug)
                    if c.get("status", "active") == "active" and schema.render_structure_errors(c))
    print(f"\nremaining malformed on disk: {remaining}  (held + flagged: {held_total})")
    if not args.write:
        print("DRY — re-run with --write to persist.")


if __name__ == "__main__":
    main()
