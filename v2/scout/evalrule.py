"""The pre-registered promotion rule, shared by every eval lane (2026-09-28).

Lifted verbatim from scripts/eval_checkin.py so the on-device model comparison judges with the SAME
state machine (ACCUMULATE / BASELINE / ELIGIBLE / WATCH / DIAGNOSE / KILL?) while passing its OWN
bar and sample-sufficiency constants. The check-in script keeps its constants and calls this with
them, so its behaviour is unchanged (tests/test_eval_checkin.py + test_model_checkin.py pin parity).
"""
import json

from scout import selfserve


def verdict(kind: str, cur: dict, prior: dict | None, *, bar: float, min_adjudicated: int,
            kill_streak: int = 3, bar_label: str | None = None) -> dict:
    """Returns {status, note, no_improve_streak}. `cur` needs adjudicated, precision, pending;
    `prior` (the previous snapshot for this kind) needs precision, no_improve_streak."""
    prev_streak = (prior or {}).get("no_improve_streak", 0)
    adj, prec = cur["adjudicated"], cur["precision"]
    prior_prec = (prior or {}).get("precision")
    label = bar_label or f"bar {bar}"

    if adj < min_adjudicated or prec is None:
        return {"status": "ACCUMULATE",
                "note": f"{adj}/{min_adjudicated} adjudicated — adjudicate the {cur.get('pending', 0)} "
                        f"pending before this can be judged.", "no_improve_streak": 0}
    if prec >= bar:
        if prior_prec is None:
            return {"status": "BASELINE", "note": f"precision {prec} at/above {label}; "
                    "need one more check-in to confirm it's sustained.", "no_improve_streak": 0}
        if prec >= prior_prec:
            return {"status": "ELIGIBLE", "note": f"precision {prec} >= prior {prior_prec}, at/above "
                    f"{label} and sustained. Promotable if it holds next check-in.", "no_improve_streak": 0}
        return {"status": "WATCH", "note": f"precision {prec} above {label} but DOWN vs prior {prior_prec}; "
                "confirm next check-in.", "no_improve_streak": 0}
    if prior_prec is None or prec > prior_prec:
        return {"status": "DIAGNOSE", "note": f"precision {prec} below {label} but improving "
                f"(prior {prior_prec}); investigate the misses and continue.", "no_improve_streak": 0}
    streak = prev_streak + 1
    if streak >= kill_streak:
        return {"status": "KILL?", "note": f"precision {prec} below {label} and NOT improving for {streak} "
                "check-ins. Conclude the model can't take over here unless a fixable cause is found; "
                "keep the code in charge.", "no_improve_streak": streak}
    return {"status": "DIAGNOSE", "note": f"precision {prec} below {label}, not improving (streak {streak}/"
            f"{kill_streak}); find the cause or conclude.", "no_improve_streak": streak}


def load_label_rows(path: str) -> dict:
    """Last-wins loader for a jsonl label file: delta_id -> the full row. Used by the model lane
    (shadow_models/labels.jsonl); the two older lanes keep their own loaders untouched."""
    out = {}
    raw = selfserve.read_data(path) or ""
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("delta_id"):
            out[row["delta_id"]] = row
    return out
