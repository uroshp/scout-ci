"""Human adjudication surface for the v3.5 shadow-eval CHALLENGER (decision-log §11, sibling of
scout/adjudicate.py which does the AUTHORSHIP judge).

The challenger (scout/challenger.py) mines disagreements with the deterministic code grader into
shadow_eval/<slug>/*.json. A human adjudicates each disagreement: was the challenger RIGHT? For a
slop candidate (champion KEPT, challenger would CUT), 'agree' = the challenger caught real slop the
grader admitted (the challenger adds value); 'disagree' = the challenger over-cut a sound claim.
Labels are appended to shadow_eval/challenger_labels.jsonl (keyed by delta_id). Pure reader/append
surface — NO model calls, no card touched.

The operative promotion signal here is DISAGREEMENT PRECISION (of the challenger's flags, the share
the human confirms), not a kappa: every disagreement so far is one-directional (challenger always
'cut'), so a Cohen's kappa over disagreements-only degenerates. A full challenger-vs-human kappa
would need human labels on a sample of AGREEMENTS too — a later, more rigorous step.

CLI (from v2/):
    python -m scout.adjudicate_challenger                            # digest + pending disagreements
    python -m scout.adjudicate_challenger label <delta_id> agree     # challenger was RIGHT (real slop)
    python -m scout.adjudicate_challenger label <delta_id> disagree "why it was wrong (over-cut)"
"""
import json
import sys

from scout import challenger, selfserve


def load_results() -> list:
    """Every persisted challenger result, across all cards (shadow_eval/<slug>/*.json)."""
    out = []
    for slug in selfserve.list_data(challenger.SHADOW_EVAL_DIR, include_dirs=True):
        if "." in slug:                 # skip root-level files (e.g. challenger_labels.jsonl); slugs are dirs
            continue
        for fn in selfserve.list_data(f"{challenger.SHADOW_EVAL_DIR}/{slug}"):
            if not fn.endswith(".json"):
                continue
            raw = selfserve.read_data(f"{challenger.SHADOW_EVAL_DIR}/{slug}/{fn}")
            if not raw:
                continue
            try:
                out.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
    return out


def label(delta_id: str, human_verdict: str, note: str = "") -> str:
    """Append a human adjudication (append-only). 'agree' = the challenger was right (the flagged
    claim really should be cut / the recovery is real); anything else = 'disagree' (challenger wrong)."""
    hv = "agree" if str(human_verdict).strip().lower() in ("agree", "right", "correct", "y", "yes") else "disagree"
    raw = selfserve.read_data(challenger.LABELS_PATH) or ""
    entry = json.dumps({"delta_id": delta_id, "human_verdict": hv, "note": note}, ensure_ascii=False)
    selfserve.write_data(challenger.LABELS_PATH, (raw.rstrip("\n") + "\n" + entry).lstrip("\n"),
                         f"adjudicate-challenger: {delta_id} {hv}")
    return hv


def _print_digest() -> None:
    results = load_results()
    if not results:
        print("No challenger results in the store yet — run scripts/run_challenger.py --run --write first.")
        return
    sc = challenger.scorecard(results)
    adj = sc["adjudication"]
    precision = (f"{adj['challenger_right']}/{adj['adjudicated']} "
                 f"({round(adj['challenger_right'] / adj['adjudicated'], 2)})"
                 if adj["adjudicated"] else "0/0 (—)")
    print("=== Shadow-eval challenger adjudication (decision-log §11) ===")
    print(f"records: {sc['records']}   claims judged: {sc['items_judged']}   "
          f"agreement: {sc['agree']}/{sc['items_judged']} ({sc['agreement_rate']})")
    print(f"kappa (challenger vs code grader): {sc['kappa_champion_vs_challenger']}   "
          f"[alignment, not the gate]")
    print(f"disagreements: {sc['disagreements']}  (recovery {sc['recovery_candidates']} / "
          f"slop {sc['slop_candidates']})   total challenger cost: ${sc['cost_usd']}")
    print(f"adjudicated: {adj['adjudicated']}/{sc['disagreements']}   "
          f"challenger RIGHT (slop-catch precision): {precision}   "
          f"wrong/over-cut: {adj['challenger_wrong']}")
    pend = sc["pending_disagreements"]
    print(f"\npending ({len(pend)}):")
    for d in pend:
        print(f"  [{d.get('delta_id')}] {d.get('direction')}  {d['slug'][:30]}  "
              f"champ={d['champion']} / chal={d['challenger']}")
        print(f"       claim: {str(d.get('claim'))[:150]}")
        if d.get("challenger_reason"):
            print(f"       challenger: {str(d['challenger_reason'])[:150]} "
                  f"(conf {d.get('challenger_confidence')})")
    if not pend:
        print("  (all adjudicated)")
    print("\nlabel:  python -m scout.adjudicate_challenger label <delta_id> agree|disagree [note...]")
    print("        agree = challenger was right to cut (real slop) | disagree = it over-cut a sound claim")


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "label" and len(args) >= 3:
        print("recorded:", label(args[1], args[2], " ".join(args[3:])))
    else:
        _print_digest()
