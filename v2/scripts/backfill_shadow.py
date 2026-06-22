#!/usr/bin/env python3
"""One-off backfill: reconstruct the v3.5 shadow-eval CHAMPION record for each already-generated
battlecard, so the offline challenger has a dataset to score against NOW instead of waiting weeks
for live material-change captures to trickle in (docs/vnext-roadmap.md §v3.5).

It rebuilds, per card, the same record scout.shadow.capture() logs at generation time, from what
the card actually persisted:
  - kept              <- claims.json (the surviving claims)
  - cut               <- the "## Cut Log" prose in current.md, parsed back to {action, claim, reason}
  - grounding_results <- each kept claim's persisted `grounding` subkey (method/status)

It does NOT re-ground or re-search: that would measure the page as it is NOW, not the champion
decision as it was made at generation — so this is pure reconstruction from persisted artifacts,
zero model/API spend. RECONSTRUCTION LIMIT (recorded in every file under "backfill"): best_ratio is
UNRECOVERABLE — the numeric grounding ratio only ever lived in the ephemeral generation result, never
in the card, so the 0.80-0.92 stratification band is absent for these historical records. The
challenger can still judge slop-admission (over kept) and recovery (over cut); it just can't
pre-stratify by ratio here. Schema matches scout.shadow (reuses its row helpers) so a future
challenger reads backfill and live records uniformly; `source` is "backfill" to tell them apart.

Reads the LOCAL public battlecards; writes shadow/<slug>/backfill.json to the PRIVATE data store via
selfserve (idempotent — fixed filename, re-runnable). Needs the SELFSERVE creds (in v2/.env) to write.

Run from v2/:  python scripts/backfill_shadow.py [--write] [slug ...]
Without --write it prints what would be captured and changes nothing.
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scout import config, selfserve
from scout.shadow import SCHEMA_VERSION, SHADOW_DIR, _cut_rows, _ground_rows, _kept_rows

# The Batman-vs-Superman card is a deliberate nonsense stress-test (the easter egg), not real CI,
# so it's excluded by default — fictional claims/cuts would pollute the challenger's precision/recall.
# Name it explicitly on the command line to include it anyway.
EXCLUDE = {"batman__vs__superman__general"}

# Cut Log entry:  - **CUT — <claim>:** <reason>   (also REVISED). The dash is an em-dash in the
# rendered card; accept a hyphen too. Non-greedy up to the first ":**" so a colon inside the claim
# text doesn't split it early.
_CUT_RE = re.compile(r"^\s*-\s*\*\*(CUT|REVISED)\s*[—-]\s*(.*?):\*\*\s*(.*)$")


def _unescape(s: str) -> str:
    """clean_output escapes `$`->`\\$` for Streamlit's LaTeX guard; undo it for the raw record."""
    return (s or "").replace("\\$", "$")


def parse_cut_log(md: str) -> list[dict]:
    """Parse the '## Cut Log' section of a stored card back to [{action, claim, reason}]. A reason
    that wrapped onto following non-bullet lines is stitched back on (defensive; saved cards are
    usually pre-stitched by format_report)."""
    i = md.find("## Cut Log")
    if i < 0:
        return []
    section = md[i + len("## Cut Log"):]
    nxt = section.find("\n## ")          # stop at the next H2 (e.g. Sources), if any
    if nxt >= 0:
        section = section[:nxt]
    entries: list[dict] = []
    for line in section.splitlines():
        m = _CUT_RE.match(line)
        if m:
            entries.append({"action": m.group(1),
                            "claim": _unescape(m.group(2).strip()),
                            "reason": _unescape(m.group(3).strip())})
        elif entries and line.strip() and not line.lstrip().startswith(("#", "-")):
            entries[-1]["reason"] = (entries[-1]["reason"] + " " + line.strip()).strip()
    return entries


def synth_grounding(kept: list[dict]) -> dict:
    """Rebuild a grounding-results dict (the shape _ground_rows expects) from kept claims' persisted
    `grounding` subkey. These all SURVIVED, so status reflects the match; best_ratio is None (never
    persisted). Cut claims have no persisted grounding rows, so they can't appear here."""
    results = []
    for c in kept:
        g = c.get("grounding") or {}
        if g.get("checked") is False:
            status = "unchecked"          # e.g. self-positioning claims that bypass grounding
        elif g.get("match"):
            status = "grounded"
        else:
            status = "kept_ungrounded"    # shouldn't occur for a kept claim; recorded honestly if so
        results.append({
            "claim_id": c.get("id"),
            "subject_key": c.get("subject_key"),
            "url": c.get("source_url"),
            "status": status,
            "method": g.get("method"),
            "best_ratio": None,           # UNRECOVERABLE — not persisted to the card
            "http_status": None,
            "excerpt": c.get("evidence_excerpt"),
        })
    return {"results": results}


def _card_dir(slug: str) -> str:
    return os.path.join(config.APP_ROOT, "battlecards", slug)


def list_card_slugs() -> list[str]:
    root = os.path.join(config.APP_ROOT, "battlecards")
    out = []
    for slug in sorted(os.listdir(root)):
        if os.path.isfile(os.path.join(root, slug, "claims.json")):
            out.append(slug)
    return out


def build_record(slug: str) -> dict:
    cdir = _card_dir(slug)
    meta = json.load(open(os.path.join(cdir, "meta.json")))
    kept = json.load(open(os.path.join(cdir, "claims.json")))
    current_path = os.path.join(cdir, "current.md")
    md = open(current_path).read() if os.path.exists(current_path) else ""
    cut = parse_cut_log(md)
    return {
        "schema_version": SCHEMA_VERSION,
        "slug": slug,
        "source": "backfill",
        "run_ts": meta.get("baseline_date"),          # the original generation date (honest, not now)
        "competitor": meta.get("competitor"),
        "my_company": meta.get("my_company"),
        "focus": meta.get("focus"),
        "kept": _kept_rows(kept),
        "cut": _cut_rows(cut),
        "grounding_results": _ground_rows(synth_grounding(kept)),
        "backfill": {
            "reconstructed_at": datetime.now().isoformat(timespec="seconds"),
            "kept_from": "claims.json",
            "cut_from": "current.md '## Cut Log' (parsed prose)",
            "grounding_from": "kept-claim grounding subkey (method/status)",
            "best_ratio": "UNRECOVERABLE: never persisted to the card; absent for historical records",
            "regrounded": False,
        },
    }


def _summarize(rec: dict) -> str:
    cuts = rec["cut"]
    n_cut = sum(1 for e in cuts if e.get("action") == "CUT")
    n_rev = sum(1 for e in cuts if e.get("action") == "REVISED")
    sample = (cuts[0]["claim"][:80] + "…") if cuts else "(none)"
    return (f"  {rec['slug']:60} run_ts={rec['run_ts']}  "
            f"kept={len(rec['kept']):>3}  cut={len(cuts):>2} (CUT {n_cut}/REVISED {n_rev})  "
            f"grounding_rows={len(rec['grounding_results']):>3}\n"
            f"      e.g. cut: {sample}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill v3.5 shadow-eval champion records from saved cards.")
    ap.add_argument("--write", action="store_true", help="actually write to the private store (default: dry run)")
    ap.add_argument("slugs", nargs="*", help="specific card slugs (default: all real cards)")
    args = ap.parse_args()

    if args.slugs:
        slugs = args.slugs
    else:
        slugs = [s for s in list_card_slugs() if s not in EXCLUDE]
        excluded = [s for s in list_card_slugs() if s in EXCLUDE]
        if excluded:
            print(f"Excluded (non-CI stress-test; pass explicitly to include): {', '.join(excluded)}\n")

    backend = "PRIVATE GitHub store" if selfserve.use_github() else "LOCAL filesystem (no SELFSERVE creds!)"
    print(f"Backfill target: {backend}   mode: {'WRITE' if args.write else 'DRY RUN'}\n")

    totals = {"cards": 0, "kept": 0, "cut": 0}
    for slug in slugs:
        try:
            rec = build_record(slug)
        except Exception as e:
            print(f"  {slug:60} SKIPPED ({type(e).__name__}: {e})")
            continue
        totals["cards"] += 1
        totals["kept"] += len(rec["kept"])
        totals["cut"] += len(rec["cut"])
        print(_summarize(rec))
        if args.write:
            path = f"{SHADOW_DIR}/{slug}/backfill.json"
            selfserve.write_data(path, json.dumps(rec, indent=2, ensure_ascii=False),
                                 f"shadow: backfill champion record for {slug}")
            print(f"      -> wrote {path}")

    print(f"\n{'WROTE' if args.write else 'WOULD WRITE'} {totals['cards']} records  "
          f"({totals['kept']} kept claims, {totals['cut']} cut entries total).")
    if not args.write:
        print("Re-run with --write to persist to the private store.")


if __name__ == "__main__":
    main()
