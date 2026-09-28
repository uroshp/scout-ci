"""Human adjudication for the on-device model comparison (2026-09-28, R4): BLINDED, TRUTH-FIRST.

For classification roles the human never sees which model said what. `pending()` prints each
disagreement item ONCE (keyed by item, not backend) with the prompt excerpt and the item text; the
human types the TRUTH in the role's own vocabulary (confirm|reject, keep|cut, material|immaterial,
consequential|routine, escalate|quiet, a persona id, a margin). The tool derives candidate-right
and reference-right for every backend and for the live reference from that one label, then reveals
the outputs. Rows go to shadow_models/labels.jsonl via selfserve.append_data (conflict-safe).

Prose roles (author/rewrite/reformat) are a blinded pairwise preference: A|B|tie, with the A/B slot
decided by the pair_delta_id's first hex byte parity and stored in the row.

CLI (from v2/):
    python -m scout.adjudicate_models                      # digest + pending items (blinded)
    python -m scout.adjudicate_models show <delta_id>      # the item with its prompt excerpt
    python -m scout.adjudicate_models label <delta_id> <truth> [note...]
    python -m scout.adjudicate_models prefer <pair_id> a|b|tie [note...]
    python -m scout.adjudicate_models reveal <delta_id>    # after labeling: who said what
No email ever carries this queue; Uroš pulls it when he wants it.
"""
import json
import sys
from datetime import datetime

from scout import modelcompare, rolespecs, selfserve


def _load(backend=None):
    return modelcompare.load_results(backend)


def _bundle_index():
    """call_id -> (record) from the captured bundles, for prompt excerpts and item texts."""
    from scout import calllog
    idx = {}
    for month in (selfserve.list_data(calllog.CALLS_DIR, include_dirs=True) or []):
        if "." in month:
            continue
        for fn in (selfserve.list_data(f"{calllog.CALLS_DIR}/{month}") or []):
            if not fn.endswith(".json"):
                continue
            raw = selfserve.read_data(f"{calllog.CALLS_DIR}/{month}/{fn}")
            if not raw:
                continue
            try:
                for c in json.loads(raw).get("calls") or []:
                    idx[c["call_id"]] = c
            except Exception:
                continue
    return idx


def pending(backend=None, role=None) -> list:
    """Item-keyed pending disagreements, each listing every backend that disagreed on it."""
    labels = modelcompare.load_labels()
    by_delta = {}
    for r in _load(backend):
        if r.get("mode", "exact") != "exact" or r.get("rep") or r.get("status") != "ok":
            continue
        if role and r.get("role") != role:
            continue
        for it in (r.get("comparison") or {}).get("items") or []:
            did = it.get("delta_id")
            if it.get("status") != "disagree" or not did or did in labels:
                continue
            row = by_delta.setdefault(did, {"delta_id": did, "call_id": r["call_id"], "role": r["role"],
                                            "slug": r.get("slug"), "item_id": it["item_id"],
                                            "reference": it["reference"], "candidates": {}})
            row["candidates"][r["backend"]] = it["candidate"]
    return sorted(by_delta.values(), key=lambda x: (x["role"], x["slug"] or "", x["delta_id"]))


def _item_text(record: dict, role: str, item_id: str) -> str:
    """The thing being judged, from the captured prompt: the op (judge), the claim (challenger), the
    candidate (materiality), or the whole user prompt, trimmed."""
    user = record.get("user") or ""
    if role == "judge":
        marker = "PROPOSED OPS TO JUDGE"
        i = user.find(marker)
        block = user[i:] if i >= 0 else user
        try:
            ops = json.loads(block[block.index("["):])
            for o in ops:
                if str(o.get("op_index")) == str(item_id):
                    return json.dumps(o, ensure_ascii=False, indent=1)
        except Exception:
            pass
        return block[:3000]
    if role == "challenger":
        try:
            i = user.index("[")
            items = json.loads(user[i:])
            for it in items:
                if str(it.get("item_id")) == str(item_id):
                    return json.dumps(it, ensure_ascii=False, indent=1)
        except Exception:
            pass
    return user[:3000]


def show(delta_id: str) -> None:
    rows = [p for p in pending() if p["delta_id"] == delta_id] or _labeled_row(delta_id)
    if not rows:
        print("unknown delta_id"); return
    row = rows[0]
    rec = _bundle_index().get(row["call_id"]) or {}
    spec = rolespecs.spec(row["role"]) or {}
    print(f"=== {row['delta_id']}  role={row['role']}  card={row['slug']}  item={row['item_id']}")
    print(f"label set: {' | '.join(spec.get('label_set') or ())}")
    print("--- what is being judged (from the captured prompt) ---")
    print(_item_text(rec, row["role"], row["item_id"]))
    print("--- system prompt excerpt ---")
    sysd = rec.get("system") or {}
    print((sysd.get("text") or sysd.get("append") or "")[:600])
    print("\n(outputs are hidden until you label; `reveal` afterwards)")


def _labeled_row(delta_id):
    lab = modelcompare.load_labels().get(delta_id)
    return [lab] if lab and lab.get("call_id") else []


def label(delta_id: str, truth: str, note: str = "") -> dict:
    rows = [p for p in pending() if p["delta_id"] == delta_id]
    if not rows:
        raise SystemExit(f"{delta_id}: not a pending disagreement (already labeled, or unknown)")
    row = rows[0]
    spec = rolespecs.spec(row["role"]) or {}
    truth = truth.strip().lower()
    allowed = tuple(spec.get("label_set") or ())
    if allowed and truth not in allowed:
        raise SystemExit(f"truth must be one of {allowed}")
    entry = {"delta_id": delta_id, "call_id": row["call_id"], "role": row["role"], "slug": row["slug"],
             "item_id": row["item_id"], "truth": truth,
             "reference_right": row["reference"] == truth,
             "candidates_right": {b: (c == truth) for b, c in row["candidates"].items()},
             "human_verdict": {b: ("agree" if c == truth else "disagree") for b, c in row["candidates"].items()},
             "note": note, "labeled_at": datetime.now().isoformat(timespec="seconds")}
    selfserve.append_data(modelcompare.LABELS_PATH, json.dumps(entry, ensure_ascii=False),
                          f"shadow-models: label {delta_id} {truth}")
    return entry


def reveal(delta_id: str) -> None:
    lab = modelcompare.load_labels().get(delta_id)
    if not lab:
        print("not labeled yet"); return
    print(f"truth={lab['truth']}  reference_right={lab['reference_right']}")
    for b, ok in (lab.get("candidates_right") or {}).items():
        print(f"  {b:16} {'RIGHT' if ok else 'wrong'}")


def prefer(pair_id: str, pick: str, note: str = "") -> dict:
    """Prose roles: blinded A|B|tie. Slot parity from the id's first hex byte (stored)."""
    pick = pick.strip().lower()
    if pick not in ("a", "b", "tie"):
        raise SystemExit("pick must be a | b | tie")
    a_is_candidate = int(pair_id[2:4], 16) % 2 == 0
    winner = ("tie" if pick == "tie" else
              ("candidate" if (pick == "a") == a_is_candidate else "reference"))
    entry = {"delta_id": pair_id, "kind": "preference", "blind": "A=candidate" if a_is_candidate else "A=reference",
             "pick": pick, "winner": winner, "note": note,
             "labeled_at": datetime.now().isoformat(timespec="seconds")}
    selfserve.append_data(modelcompare.LABELS_PATH, json.dumps(entry, ensure_ascii=False),
                          f"shadow-models: prefer {pair_id} {pick}")
    return entry


def pair_texts(pair_id: str) -> tuple[str, str] | None:
    """(A, B) texts for a prose pair, blinded by parity."""
    for r in _load():
        for it in (r.get("comparison") or {}).get("items") or []:
            if it.get("pair_delta_id") == pair_id:
                rec = _bundle_index().get(r["call_id"]) or {}
                spec = rolespecs.spec(r["role"]) or {}
                ref = spec["parse"](((rec.get("result") or {}).get("text")) or "") or {}
                cand = spec["parse"](r.get("text") or "") or {}
                rt = (ref.get("extra") or {}).get("texts", {}).get(it["item_id"], "")
                ct = (cand.get("extra") or {}).get("texts", {}).get(it["item_id"], "")
                a_is_candidate = int(pair_id[2:4], 16) % 2 == 0
                return (ct, rt) if a_is_candidate else (rt, ct)
    return None


def _print_digest(backend=None, role=None):
    labels = modelcompare.load_labels()
    pend = pending(backend, role)
    print(f"=== on-device model comparison: adjudication (blinded, truth-first) ===")
    print(f"labels so far: {len(labels)}   pending items: {len(pend)}")
    for p in pend[:40]:
        print(f"  [{p['delta_id']}] {p['role']:14} {str(p['slug'])[:28]:28} item={str(p['item_id'])[:30]}  "
              f"(disagreed by: {', '.join(sorted(p['candidates']))})")
    if len(pend) > 40:
        print(f"  ... {len(pend) - 40} more")
    print("\nshow:   python -m scout.adjudicate_models show <delta_id>")
    print("label:  python -m scout.adjudicate_models label <delta_id> <truth> [note...]")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        _print_digest()
    elif args[0] == "show" and len(args) >= 2:
        show(args[1])
    elif args[0] == "label" and len(args) >= 3:
        e = label(args[1], args[2], " ".join(args[3:]))
        print(f"recorded: truth={e['truth']}  reference_right={e['reference_right']}  "
              f"candidates={e['candidates_right']}")
    elif args[0] == "reveal" and len(args) >= 2:
        reveal(args[1])
    elif args[0] == "prefer" and len(args) >= 3:
        e = prefer(args[1], args[2], " ".join(args[3:]))
        print(f"recorded: pick={e['pick']} -> {e['winner']}")
    elif args[0] == "pair" and len(args) >= 2:
        t = pair_texts(args[1])
        print("(A)\n" + (t[0] if t else "?") + "\n\n(B)\n" + (t[1] if t else "?"))
    else:
        print(__doc__)
