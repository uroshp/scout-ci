#!/usr/bin/env python3
"""One-off: seed the authorship-judge adjudication store from Uros's REAL approve/reject decisions
over the launch fortnight (docs/vnext-roadmap.md §v3.5, spec §17 step 6).

Until now review.py applied an approved propagation proposal to the card but logged NOTHING back, so
adjudication/authorship_labels.jsonl stayed EMPTY despite ~2 weeks of genuine human adjudication.
This recovers that signal. Each verdict was reconstructed from Uros's OWN git commits (two
independent passes agreed) and CONFIRMED by him on 2026-06-21:

  8 model-CONFIRMED proposals (the ones emailed for approval) -> human verdict:
    7 approved  -> 'agree'    (the judge's confirm was right)
    1 rejected  -> 'disagree' (judge confirmed the weaker 17:04 govt-disable variant; Uros kept the
                               11:04 one — per commit daf84f4 body 'the weaker 17:04 variant was rejected')

It matches each verdict to the LIVE propagation decision by (slug, subject_key, operation) and writes
via the canonical delta_id (adjudicate._delta_id), so adjudicate.py reads the labels back correctly.
Idempotent (last-write-wins per delta_id). Reads/writes the PRIVATE store via selfserve.

NOTE (separate bucket, NOT written here): commit 486bfd4 ('reframe EU sovereignty as proposed, not
enacted') was Uros correcting an over-broad, ungrounded claim from GENERATION — a VERIFICATION-slop
example for the gold set, not an authorship-judge decision (no propagation proposal behind it).

Run from v2/:  python scripts/backfill_adjudication.py [--write]
Without --write it prints the match + intended labels and changes nothing.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scout import adjudicate, selfserve

# (slug, subject_key, operation) -> (human_verdict, note). Verdicts are git-attested + human-confirmed.
VERDICTS = {
    ("anthropic__vs__openai__enterprise-coding-developers",
     "objection | model-govt-disable-fable-mythos | current", "add"):
        ("agree", "Approved: applied verbatim in commit daf84f4 (the 11:04 govt-disable variant Uros kept)."),
    ("anthropic__vs__openai__enterprise-coding-developers",
     "objection | fable-mythos-access-suspension | current", "add"):
        ("disagree", "Rejected: judge confirmed the weaker 17:04 variant; Uros kept the 11:04 one. "
                     "Per commit daf84f4 body 'the weaker 17:04 variant was rejected'; never landed (git -S). "
                     "A judge over-confirm / slop direction."),
    ("anthropic__vs__openai__enterprise-coding-developers",
     "objection | export-ban-international-defection | current", "add"):
        ("agree", "Approved: applied verbatim in commit f2704f5."),
    ("cursor__vs__cognition__general",
     "objection | cursor-spacex-ownership | cursor-adverse", "revise"):
        ("agree", "Approved: applied in commit 5825559."),
    ("google-cloud__vs__aws__ai-ml-infrastructure",
     "objection | gcp-india-network-incident-june-2026 | current", "add"):
        ("agree", "Approved: applied in commit 5825559 (one inline de-dash, style only; substance verbatim)."),
    ("mistral__vs__openai__enterprise-and-sovereign-ai-for-developers-and-agents",
     "mistral | battlecard-vibe-agent | where-we-win", "add"):
        ("agree", "Approved: applied in commit 6e5bd5a (one of the 2 approved Vibe proposals)."),
    ("mistral__vs__openai__enterprise-and-sovereign-ai-for-developers-and-agents",
     "mistral | objection-ecosystem | current", "revise"):
        ("agree", "Approved: applied in commit 6e5bd5a (one of the 2 approved Vibe proposals)."),
    ("salesforce__vs__hubspot__ai-agents-agentforce-vs-breeze",
     "salesforce | layoffs | agentforce-2026", "revise"):
        ("agree", "Approved: applied in commit f7c1ed0 (follow-up 76ed555 was a de-dash only)."),
}

PROVENANCE = "git-reconstruction; human-confirmed 2026-06-21"


def main() -> None:
    ap = argparse.ArgumentParser(description="Seed authorship adjudication labels from Uros's git-attested verdicts.")
    ap.add_argument("--write", action="store_true", help="persist labels to the private store (default: dry run)")
    args = ap.parse_args()

    deltas = adjudicate.load_deltas()
    confirms = [d for d in deltas if d.get("judge_verdict") == "confirm"]
    by_key = {(d["slug"], d.get("subject_key"), d.get("operation")): d for d in confirms}

    print(f"Model-confirmed proposals in store: {len(confirms)}   verdicts to apply: {len(VERDICTS)}\n")
    entries, missing = [], []
    for key, (verdict, note) in VERDICTS.items():
        d = by_key.get(key)
        if not d:
            missing.append(key)
            print(f"  !! NO MATCH for {key}")
            continue
        entries.append({"delta_id": d["delta_id"], "human_verdict": verdict,
                        "note": note, "source": PROVENANCE})
        print(f"  [{d['delta_id']}] {verdict.upper():8} {key[0][:22]:22} {key[1]}")

    unmatched_confirms = [k for k in by_key if k not in VERDICTS]
    if unmatched_confirms:
        print(f"\n  (confirmed proposals with NO verdict supplied: {unmatched_confirms})")
    if missing:
        print(f"\nABORT: {len(missing)} verdict(s) did not match a live decision; not writing. Fix the keys.")
        sys.exit(1)

    n_agree = sum(1 for e in entries if e["human_verdict"] == "agree")
    print(f"\n{len(entries)} labels ready ({n_agree} agree / {len(entries) - n_agree} disagree).")

    if not args.write:
        print("Re-run with --write to persist to adjudication/authorship_labels.jsonl.")
        return

    # Merge with any existing labels (last-write-wins per delta_id), write once.
    existing = adjudicate.load_labels()                     # {delta_id: {...}}
    for e in entries:
        existing[e["delta_id"]] = e
    body = "\n".join(json.dumps(v, ensure_ascii=False) for v in existing.values()) + "\n"
    selfserve.write_data(adjudicate.LABELS_PATH, body,
                         f"adjudicate: backfill {len(entries)} git-attested human verdicts")
    print(f"-> wrote {len(existing)} total labels to {adjudicate.LABELS_PATH}")


if __name__ == "__main__":
    main()
