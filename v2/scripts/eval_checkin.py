"""14-DAY EVAL CHECK-IN for the v3.5 shadow-eval judges that may TAKE OVER the deterministic code:
the VERIFICATION challenger (support-judge over grounding) and the AUTHORSHIP judge (propose->judge).

This is the promotion instrument for `docs/eval-exit-criteria.md`: each run reports the current
metrics, the TREND vs the prior check-in, and applies the PRE-REGISTERED go/no-go rule (promote /
continue / diagnose / kill). NOT the consequentiality filter — that is a separate, faster track
(additive, not a code-takeover; spot-checked over days, not weeks).

Reporting is FREE — it reads the stored challenger results + human labels. The challenger MODEL refresh
(spends API budget, bounded) is a SEPARATE step run before this in the scheduled job:
    python scripts/run_challenger.py --run --write --limit 25

    python scripts/eval_checkin.py                 # print the check-in (no spend, no write)
    python scripts/eval_checkin.py --snapshot      # also persist this check-in (enables next trend)
    python scripts/eval_checkin.py --snapshot --email   # also email the report
"""
import argparse
import json
import sys
from datetime import datetime

sys.path.insert(0, ".")
from scout import adjudicate, adjudicate_challenger, challenger, config, notify, selfserve

EVAL_DIR = "eval_checkin"                 # per-check-in snapshots in the private store

# Pre-registered bars (docs/eval-exit-criteria.md). Disagreement precision = when the judge overrules
# the incumbent, is it right (human-confirmed). The niche/overrule cases carry the higher bar.
PRECISION_BAR = 0.80                       # share of overrules a human confirms
MIN_ADJUDICATED = {"verification": 15, "authorship": adjudicate.AUTHORSHIP_GATE}  # sample sufficiency
KILL_STREAK = 3                           # check-ins below bar with no improvement -> conclude it can't take over


def _precision(right: int, adjudicated: int):
    return round(right / adjudicated, 3) if adjudicated else None


def verification_metrics() -> dict:
    sc = challenger.scorecard(adjudicate_challenger.load_results())
    adj = sc.get("adjudication", {})
    return {
        "adjudicated": adj.get("adjudicated", 0),
        "right": adj.get("challenger_right", 0),
        "wrong": adj.get("challenger_wrong", 0),
        "precision": _precision(adj.get("challenger_right", 0), adj.get("adjudicated", 0)),
        "kappa_vs_code": sc.get("kappa_champion_vs_challenger"),
        "disagreements": sc.get("disagreements", 0),
        "pending": len(sc.get("pending_disagreements", [])),
    }


def authorship_metrics() -> dict:
    d = adjudicate.digest()
    return {
        "adjudicated": d.get("adjudicated", 0),
        "right": d.get("judge_right", 0),
        "wrong": d.get("judge_wrong", 0),
        "precision": _precision(d.get("judge_right", 0), d.get("adjudicated", 0)),
        "net_positive": d.get("gate", {}).get("net_positive"),
        "pending": len(d.get("pending", [])),
    }


def verdict(kind: str, cur: dict, prior: dict | None) -> dict:
    """The pre-registered rule. Returns {status, note, no_improve_streak}."""
    prev_streak = (prior or {}).get("no_improve_streak", 0)
    adj, prec = cur["adjudicated"], cur["precision"]
    prior_prec = (prior or {}).get("precision")

    if adj < MIN_ADJUDICATED[kind] or prec is None:
        return {"status": "ACCUMULATE",
                "note": f"{adj}/{MIN_ADJUDICATED[kind]} adjudicated — adjudicate the {cur['pending']} "
                        f"pending before this can be judged.", "no_improve_streak": 0}
    if prec >= PRECISION_BAR:
        if prior_prec is None:
            return {"status": "BASELINE", "note": f"precision {prec} at/above bar {PRECISION_BAR}; "
                    "need one more check-in to confirm it's sustained.", "no_improve_streak": 0}
        if prec >= prior_prec:
            return {"status": "ELIGIBLE", "note": f"precision {prec} >= prior {prior_prec}, at/above "
                    "bar and sustained. Promotable if it holds next check-in.", "no_improve_streak": 0}
        return {"status": "WATCH", "note": f"precision {prec} above bar but DOWN vs prior {prior_prec}; "
                "confirm next check-in.", "no_improve_streak": 0}
    # below bar
    if prior_prec is None or prec > prior_prec:
        return {"status": "DIAGNOSE", "note": f"precision {prec} below bar {PRECISION_BAR} but improving "
                f"(prior {prior_prec}); investigate the misses and continue.", "no_improve_streak": 0}
    streak = prev_streak + 1
    if streak >= KILL_STREAK:
        return {"status": "KILL?", "note": f"precision {prec} below bar and NOT improving for {streak} "
                "check-ins. Conclude the model can't take over here unless a fixable cause is found; "
                "keep the code in charge.", "no_improve_streak": streak}
    return {"status": "DIAGNOSE", "note": f"precision {prec} below bar, not improving (streak {streak}/"
            f"{KILL_STREAK}); find the cause or conclude.", "no_improve_streak": streak}


def _load_prior() -> dict | None:
    try:
        files = sorted(f for f in selfserve.list_data(EVAL_DIR) if f.endswith(".json"))
        if not files:
            return None
        return json.loads(selfserve.read_data(f"{EVAL_DIR}/{files[-1]}"))
    except Exception as e:
        print(f"[eval] prior snapshot unreadable ({type(e).__name__}: {e})", file=sys.stderr)
        return None


def build(now: datetime) -> tuple[dict, str]:
    prior = _load_prior()
    ver, auth = verification_metrics(), authorship_metrics()
    v_ver = verdict("verification", ver, (prior or {}).get("verification"))
    v_auth = verdict("authorship", auth, (prior or {}).get("authorship"))
    snapshot = {
        "stamp": now.isoformat(timespec="seconds"),
        "prior_stamp": (prior or {}).get("stamp"),
        "verification": {**ver, **{"no_improve_streak": v_ver["no_improve_streak"]}},
        "authorship": {**auth, **{"no_improve_streak": v_auth["no_improve_streak"]}},
        "verdicts": {"verification": v_ver["status"], "authorship": v_auth["status"]},
    }

    def block(name, m, vd):
        return (f"## {name}\n"
                f"- precision (overrule correctness): **{m['precision']}** (bar {PRECISION_BAR})  "
                f"[{m['right']} right / {m['wrong']} wrong of {m['adjudicated']} adjudicated]\n"
                f"- pending to adjudicate: {m['pending']}\n"
                f"- **VERDICT: {vd['status']}** — {vd['note']}\n")

    body = (f"# 14-day eval check-in — {now.date()}\n\n"
            f"Prior check-in: {(prior or {}).get('stamp', 'none (first run)')}\n\n"
            + block("Verification challenger (support over grounding)", ver, v_ver)
            + f"  - alignment κ vs code grader: {ver['kappa_vs_code']} (sanity, not the gate); "
              f"open disagreements: {ver['disagreements']}\n\n"
            + block("Authorship judge (propose→judge)", auth, v_auth)
            + "\nPer-slice κ (by claim type / section) is the next refinement; v1 reports aggregate "
              "disagreement precision. Bar + rule: docs/eval-exit-criteria.md.\n"
            + "\nNOTE: the consequentiality filter is a SEPARATE, faster track — not in this check-in.\n")
    return snapshot, body


def main() -> None:
    ap = argparse.ArgumentParser(description="14-day eval check-in for the v3.5 takeover judges.")
    ap.add_argument("--snapshot", action="store_true", help="persist this check-in (enables next trend)")
    ap.add_argument("--email", action="store_true", help="email the report (needs SMTP creds)")
    args = ap.parse_args()

    now = datetime.now()
    snapshot, body = build(now)
    print(body)

    if args.snapshot:
        try:
            stamp = now.strftime("%Y%m%dT%H%M%S")
            selfserve.write_data(f"{EVAL_DIR}/{stamp}.json",
                                 json.dumps(snapshot, indent=2, default=str, ensure_ascii=False),
                                 f"eval: check-in {stamp}")
            print(f"\n[eval] snapshot persisted -> {EVAL_DIR}/{stamp}.json")
        except Exception as e:
            print(f"[eval] snapshot write failed ({type(e).__name__}: {e})", file=sys.stderr)

    if args.email:
        v = snapshot["verdicts"]
        subject = f"Scout eval check-in {now.date()} — verif {v['verification']} / authorship {v['authorship']}"
        res = notify._dispatch(subject, body, dry_run=False)
        print(f"[eval] email: {res.get('sent')} ({res.get('via') or res.get('reason')})")


if __name__ == "__main__":
    main()
