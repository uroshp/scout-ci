"""Interim human-review surface for propagation AUTHORSHIP decisions (spec §17 step 6).

The authorship judge (propagation's propose->judge) is one of Scout's two shadow-qualified model
judgments. It earns autonomy the same way the verification judge does: run in shadow, a human
adjudicates each decision (was the judge right?), and promote only on data — never calendar. This is
the surface for that weekly (~Friday) adjudication.

It READS what propagation already captured (`propagation/<slug>/<stamp>.json`, written by
propagate.log_decisions on real shadow/live runs) and the human LABELS recorded here
(`adjudication/authorship_labels.jsonl`). Both live in the PRIVATE data store, via selfserve — never
the public repo. A pure reader/append surface: it triggers no model call and never touches a card.

Only the JUDGE's calls (confirm/reject) are adjudicatable deltas — a floor_reject is the deterministic
model-free floor, not a judgment under review, so it is logged but never queued for a human.

CLI (run from v2/):
    python -m scout.adjudicate                         # print the digest + pending deltas
    python -m scout.adjudicate label <delta_id> agree  # the judge was right
    python -m scout.adjudicate label <delta_id> disagree "why the judge was wrong"
"""
import hashlib
import json
import sys

from scout import config, selfserve

PROP_DIR = "propagation"
LABELS_PATH = "adjudication/authorship_labels.jsonl"
# Promotion checkpoint: evaluate the authorship judge once ~this many of its calls are human-
# adjudicated (count-gated, NOT calendar — authorship fires rarely). Net-positive on deltas + slop≈0
# into a bounded role; promote operations safest-first (retire-on-falsified-fact -> revise -> add).
AUTHORSHIP_GATE = int(config.__dict__.get("PROPAGATE_AUTHORSHIP_GATE", 20))
_JUDGE_VERDICTS = ("confirm", "reject")  # adjudicatable; floor_reject is deterministic, excluded
# A judge that returned NO verdict is defaulted to 'reject' (propagate._decision_records, the fail-closed
# guard). That is a judge HICCUP, not a judgment under review — it must not count toward the promotion
# gate (it would let an infra error masquerade as a "correct reject") and the dropped op needs a re-run,
# not a human label. Detected by the canonical fail-closed reason string and excluded below.
_FAIL_CLOSED_MARK = "fail-closed"


def _is_fail_closed(d: dict) -> bool:
    return (d.get("judge_verdict") == "reject"
            and _FAIL_CLOSED_MARK in str(d.get("judge_reason", "")).lower())


def _delta_id(slug: str, rec: dict) -> str:
    """Stable id for one judged op, reproducible across reads (the label key)."""
    key = f"{slug}|{rec.get('run_ts')}|{rec.get('operation')}|{rec.get('subject_key')}|{rec.get('derived_from')}"
    return "a_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def load_deltas() -> list:
    """Every captured propagation op decision, flattened, each tagged with slug, run_ts, delta_id.
    Tolerates a backend that lists files only (GitHub) or dirs+files (local FS)."""
    out = []
    for slug in selfserve.list_data(PROP_DIR, include_dirs=True):  # propagation/ holds per-card SUBDIRS
        if slug.endswith(".json"):           # a file at the top level is not a slug dir; skip
            continue
        for fn in selfserve.list_data(f"{PROP_DIR}/{slug}"):
            if not fn.endswith(".json"):
                continue
            raw = selfserve.read_data(f"{PROP_DIR}/{slug}/{fn}")
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            run_ts = payload.get("run_ts")
            sl = payload.get("slug", slug)
            for rec in payload.get("decisions", []) or []:
                r = dict(rec)
                r["slug"], r["run_ts"] = sl, run_ts
                r["delta_id"] = _delta_id(sl, r)
                out.append(r)
    return out


def load_labels() -> dict:
    """delta_id -> latest human label (last line wins). {} if none recorded yet."""
    raw = selfserve.read_data(LABELS_PATH)
    labels = {}
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            labels[d["delta_id"]] = d
        except (json.JSONDecodeError, KeyError):
            continue
    return labels


def label(delta_id: str, human_verdict: str, note: str = "") -> str:
    """Record a human adjudication (append-only): did the judge get this op right?
    human_verdict -> 'agree' (judge right) | 'disagree' (judge wrong)."""
    hv = "agree" if str(human_verdict).strip().lower() in ("agree", "right", "correct", "y", "yes") else "disagree"
    entry = json.dumps({"delta_id": delta_id, "human_verdict": hv, "note": note}, ensure_ascii=False)
    # append_data is conflict-safe (re-reads + retries on a 409); plain read+write raced and dropped a
    # label when review.apply auto-logged two approvals back-to-back. load_labels is last-wins, so a
    # duplicate from a retry is harmless.
    selfserve.append_data(LABELS_PATH, entry, f"adjudicate: {delta_id} {hv}")
    return hv


def digest() -> dict:
    """The Friday read: captured/adjudicated/pending counts + promotion-gate progress."""
    deltas = load_deltas()
    labels = load_labels()
    fail_closed = [d for d in deltas if _is_fail_closed(d)]
    # judge_unavailable = the judge (and its fallback) never ruled — an infra event, not a judgment:
    # excluded from the gate like a fail-close; listed for manual review / next-run re-judge.
    unavailable = [d for d in deltas if d.get("judge_verdict") == "judge_unavailable"]
    def _is_fallback(d):
        return str(d.get("judged_by") or "").startswith("fallback:")
    # A FALLBACK-model verdict is a real judgment but not the OPUS judge's — it must not score the
    # promotion gate (a Sonnet stand-in would pollute "can the Opus judge be trusted").
    judged = [d for d in deltas
              if d.get("judge_verdict") in _JUDGE_VERDICTS and not _is_fail_closed(d)
              and not _is_fallback(d)]
    pending = [d for d in judged if d["delta_id"] not in labels]
    adjudicated = [d for d in judged if d["delta_id"] in labels]
    right = sum(1 for d in adjudicated if labels[d["delta_id"]]["human_verdict"] == "agree")
    wrong = len(adjudicated) - right
    return {
        "captured": len(deltas),
        "by_verdict": {v: sum(1 for d in deltas if d.get("judge_verdict") == v)
                       for v in ("confirm", "reject", "floor_reject", "judge_unavailable",
                                 "gated_routine")},   # gated = the conseq gate's deferrals, not judgments
        "fail_closed": fail_closed,                       # judge hiccups, excluded from the gate; re-run
        "judge_unavailable": unavailable,                 # outage drafts, excluded; manual review
        "fallback_judged": sum(1 for d in deltas if _is_fallback(d)),  # real verdicts, off the gate
        "adjudicated": len(adjudicated), "judge_right": right, "judge_wrong": wrong,
        "pending": pending,
        "gate": {"target": AUTHORSHIP_GATE, "progress": len(adjudicated),
                 "net_positive": right > wrong,
                 "ready": len(adjudicated) >= AUTHORSHIP_GATE and right > wrong},
    }


def _print_digest() -> None:
    d = digest()
    g = d["gate"]
    print("=== Propagation authorship adjudication (spec §17 step 6) ===")
    print(f"captured op decisions: {d['captured']}   {d['by_verdict']}")
    print(f"adjudicated: {d['adjudicated']}   judge right {d['judge_right']} / wrong {d['judge_wrong']}")
    print(f"promotion gate: {g['progress']}/{g['target']} adjudicated  net_positive={g['net_positive']}  "
          f"READY={g['ready']}")
    fc = (d.get("fail_closed") or []) + (d.get("judge_unavailable") or [])
    if fc:
        print(f"\njudge fail-closes / outages EXCLUDED from the gate ({len(fc)} — need a re-run or "
              f"manual review, not a label):")
        for x in fc:
            print(f"  [{x['delta_id']}] {x['slug']}  {str(x.get('operation','?')).upper()} "
                  f"{x.get('section','')}  judge={x.get('judge_verdict')}  "
                  f"({x.get('trigger_source_url')})")
    if d.get("fallback_judged"):
        print(f"\nfallback-judged verdicts (real, but excluded from the Opus gate): {d['fallback_judged']}")
    print(f"\npending ({len(d['pending'])}):")
    for x in d["pending"]:
        print(f"  [{x['delta_id']}] {x['slug']}  {str(x.get('operation','?')).upper()} "
              f"{x.get('section','')}  judge={x.get('judge_verdict')}")
        print(f"       trigger {x.get('derived_from')}  ({x.get('trigger_source_url')})")
        if x.get("old_text"):
            print(f"       old: {str(x['old_text'])[:140]}")
        if x.get("new_text"):
            print(f"       new: {str(x['new_text'])[:220]}")
        if x.get("judge_reason"):
            print(f"       judge: {str(x['judge_reason'])[:180]}")
    if not d["pending"]:
        print("  (queue empty — skip this week)")
    print("\nlabel:  python -m scout.adjudicate label <delta_id> agree|disagree [note...]")


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "label" and len(args) >= 3:
        print("recorded:", label(args[1], args[2], " ".join(args[3:])))
    else:
        _print_digest()
