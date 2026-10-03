"""Monday audit nudge (2026-10-02): one email naming how many applied edits from the last week await
the owner's grade, so the authorship judge's precision keeps a pulse now that approval is automatic.
Runs on the mini (launchd com.urosh.scout-audit-nudge, Mondays 09:00 PT). Silent when there is
nothing to grade. `--dry` prints instead of sending."""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scout import adjudicate, notify


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    week = adjudicate.weekly_sample()
    backlog = len(adjudicate.digest().get("pending") or [])
    if not week:
        print("[audit] nothing applied in the last 7 days awaits a grade")
        return 0
    cards = sorted({x.get("slug", "") for x in week})
    body = (f"{len(week)} edits the judge approved and Scout applied in the last 7 days are waiting for your grade "
            f"(across {len(cards)} card{'s' if len(cards) != 1 else ''}). About ten minutes.\n\n"
            "On the mini:\n  scout-labels --queue authorship\n\n"
            "Enter = the judge was right. This keeps the authorship judge's precision reading live now that approval is automatic.\n"
            + (f"\nAlso pending beyond this week: {backlog - len(week)} older decisions (optional).\n" if backlog > len(week) else ""))
    subject = f"Scout audit: {len(week)} applied edits to grade this week"
    if a.dry:
        print(subject); print(body); return 0
    res = notify._dispatch(subject, body, dry_run=False)
    print(f"[audit] sent={res.get('sent')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
