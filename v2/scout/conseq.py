"""Conseq. track review helpers. The consequentiality filter logs a verdict per act-grade run to
filter/<slug>/ (shadow.filter_capture, shadow mode). This reads them back for the SPOT-CHECK that
decides whether to flip the filter from shadow to gate (production). Separate, faster lane from the
v3.5 model-eval track (docs/eval-exit-criteria.md): a short human spot-check, not weeks of κ.

The monitor calls maybe_notify_ready() at the end of a run: once enough verdicts exist, it emails a
one-time digest so the owner knows the track is ready to review. Best-effort; never breaks the run.
"""
import json
import sys

from scout import config, selfserve, shadow

NOTIFY_MARKER = f"{shadow.FILTER_DIR}/_review_notified.json"   # written once, so we email only once


def load_verdicts() -> list:
    """Every persisted filter verdict across all cards (filter/<slug>/*.json). Robust to an empty or
    unreachable store (returns [])."""
    out = []
    try:
        for slug in selfserve.list_data(shadow.FILTER_DIR, include_dirs=True):
            if "." in slug:                      # skip root-level files (e.g. _review_notified.json)
                continue
            for fn in selfserve.list_data(f"{shadow.FILTER_DIR}/{slug}"):
                if not fn.endswith(".json"):
                    continue
                raw = selfserve.read_data(f"{shadow.FILTER_DIR}/{slug}/{fn}")
                if not raw:
                    continue
                try:
                    out.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        print(f"[conseq] load_verdicts skipped ({type(e).__name__}: {e})", file=sys.stderr)
    return out


def readiness(verdicts=None, threshold=None) -> dict:
    v = load_verdicts() if verdicts is None else verdicts
    th = threshold if threshold is not None else config.CONSEQ_REVIEW_MIN
    cons = sum(1 for r in v if r.get("consequential") is True)
    routine = sum(1 for r in v if r.get("consequential") is False)
    return {"count": len(v), "consequential": cons, "routine": routine,
            "threshold": th, "ready": len(v) >= th}


def review_digest(verdicts=None) -> tuple[str, str]:
    """Subject + body listing each verdict for the spot-check (card, consequential?, rationale)."""
    v = load_verdicts() if verdicts is None else verdicts
    r = readiness(v)
    subject = (f"Scout conseq. track ready to review — {r['count']} verdicts "
               f"({r['consequential']} consequential / {r['routine']} routine)")
    lines = [
        "The consequentiality filter has logged enough shadow verdicts to spot-check.",
        "For each: would you have made the same call? If it's reading them right, flip",
        "SCOUT_CONSEQUENTIAL_FILTER=gate to take it to production.", "",
        f"Verdicts ({r['count']}; {r['consequential']} consequential / {r['routine']} routine):", "",
    ]
    for d in sorted(v, key=lambda x: x.get("run_ts") or ""):
        card = (f"{d.get('my_company')} vs {d.get('competitor')}"
                if d.get("my_company") else d.get("slug", "?"))
        tag = {True: "CONSEQUENTIAL", False: "routine"}.get(d.get("consequential"), "?")
        lines.append(f"- [{tag}] {card} ({(d.get('run_ts') or '')[:10]})")
        if d.get("consequence_rationale"):
            lines.append(f"    why: {d['consequence_rationale']}")
        if d.get("lead_headline"):
            lines.append(f"    lead: {d['lead_headline']}")
    return subject, "\n".join(lines)


def maybe_notify_ready(send: bool = True) -> dict:
    """If the track has crossed the review threshold and we haven't emailed yet, email the digest and
    drop a marker so it fires only once. Best-effort: never raises into the monitor path."""
    try:
        if config.CONSEQUENTIAL_FILTER == "off":
            return {"notified": False, "reason": "filter off"}
        if selfserve.read_data(NOTIFY_MARKER):
            return {"notified": False, "reason": "already notified"}
        v = load_verdicts()
        r = readiness(v)
        if not r["ready"]:
            return {"notified": False, "reason": f"{r['count']}/{r['threshold']} verdicts"}
        subject, body = review_digest(v)
        from scout import notify
        res = notify._dispatch(subject, body, dry_run=not send)
        if res.get("sent"):
            selfserve.write_data(NOTIFY_MARKER, json.dumps({"count": r["count"]}),
                                 "conseq: review-ready notified")
        return {"notified": bool(res.get("sent")), "count": r["count"], "email": res}
    except Exception as e:
        print(f"[conseq] notify skipped ({type(e).__name__}: {e})", file=sys.stderr)
        return {"notified": False, "reason": f"{type(e).__name__}: {e}"}


if __name__ == "__main__":
    rr = readiness()
    print(f"verdicts: {rr['count']} ({rr['consequential']} consequential / {rr['routine']} routine)  "
          f"threshold {rr['threshold']}  ready={rr['ready']}")
    if rr["count"]:
        print("\n" + review_digest()[1])
