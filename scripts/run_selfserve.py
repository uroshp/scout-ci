"""Self-serve Action runner: turn queued requests into PRIVATE user_reports/ cards.

Runs INSIDE the GitHub Action, on the repo checkout. For each queued request that has no
result yet: re-check the gate (authoritative — the app's gate is only advisory), generate via
the SDK pipeline with write=False (so it NEVER touches battlecards/), save the card under
user_reports/<job_id>/, and update selfserve/state.json (used += 1, spend += real cost). The
workflow commits + pushes whatever this writes; its `concurrency` group serializes runs, so the
state.json read-modify-write never races.

    python scripts/run_selfserve.py
"""
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scout import config, selfserve
from scout.generate import generate

REQ_DIR = selfserve.REQUESTS_DIR
RES_DIR = selfserve.RESULTS_DIR


def _save_result(job_id: str, record: dict, markdown=None, claims=None) -> None:
    d = os.path.join(RES_DIR, job_id)
    os.makedirs(d, exist_ok=True)
    if markdown is not None:
        with open(os.path.join(d, "card.md"), "w") as f:
            f.write(markdown)
    if claims is not None:
        with open(os.path.join(d, "claims.json"), "w") as f:
            json.dump(claims, f, indent=2, ensure_ascii=False)
    with open(os.path.join(d, "result.json"), "w") as f:
        json.dump(record, f, indent=2, ensure_ascii=False)


def _save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(selfserve.STATE_PATH), exist_ok=True)
    with open(selfserve.STATE_PATH, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def _pending_requests() -> list[dict]:
    """Queued requests with no result.json yet, oldest first (filename carries the timestamp)."""
    if not os.path.isdir(REQ_DIR):
        return []
    out = []
    for fname in sorted(os.listdir(REQ_DIR)):
        if not fname.endswith(".json"):
            continue
        job_id = fname[:-5]
        if os.path.exists(os.path.join(RES_DIR, job_id, "result.json")):
            continue  # already processed
        try:
            with open(os.path.join(REQ_DIR, fname)) as f:
                out.append(json.load(f))
        except Exception as e:
            print(f"skip unreadable request {fname}: {e}")
    return out


def main() -> None:
    reqs = _pending_requests()
    if not reqs:
        print("no pending self-serve requests")
        return
    print(f"{len(reqs)} pending request(s)")
    for req in reqs:
        job_id = req["job_id"]
        now = datetime.now().isoformat(timespec="seconds")

        # Authoritative gate, re-checked at the point of spend.
        g = selfserve.gate()
        if not g["open"]:
            _save_result(job_id, {
                "job_id": job_id, "status": "rejected", "reason": g["reason"],
                "finished_at": now,
                "message": ("The free launch window is full — DM for access."
                            if g["reason"] == "window_closed"
                            else "The spend ceiling has been reached — DM for access."),
            })
            print(f"{job_id}: REJECTED ({g['reason']})")
            continue

        try:
            res = generate(req["competitor"], req.get("my_company"), req.get("focus"), write=False)
        except Exception as e:
            _save_result(job_id, {"job_id": job_id, "status": "error", "finished_at": now,
                                  "message": f"generation failed: {e}"})
            print(f"{job_id}: ERROR {e}")
            continue

        gen_cost = (res.get("run") or {}).get("cost_usd") or 0.0
        retry_cost = ((res.get("retry") or {}).get("run") or {}).get("cost_usd") or 0.0
        total = round(gen_cost + retry_cost, 4)

        # Update BOTH gates after a successful spend (only successes count).
        state = selfserve.load_state()
        state["used"] = int(state["used"]) + 1
        state["spend_usd"] = round(float(state["spend_usd"]) + total, 4)
        _save_state(state)

        _save_result(job_id, {
            "job_id": job_id, "status": "done", "finished_at": now,
            "slug": res.get("slug"), "cost_usd": total,
            "kept_claims": len(res.get("kept", [])),
            "message": "Report ready.",
        }, markdown=res.get("markdown"), claims=res.get("kept"))
        print(f"{job_id}: DONE  cost=${total}  claims={len(res.get('kept', []))}  "
              f"(used={state['used']}/{state['free_limit']}, spend=${state['spend_usd']})")


if __name__ == "__main__":
    main()
