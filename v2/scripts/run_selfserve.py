"""Self-serve Action runner: turn queued requests into PRIVATE user_reports/ cards.

Runs INSIDE the GitHub Action, triggered by a workflow_dispatch the app POSTs on submit. All
reads/writes go through the selfserve backend (the GitHub API against the PRIVATE data repo when
SELFSERVE_GH_TOKEN+SELFSERVE_REPO are set; local FS in dev). For each queued request with no
result yet: re-check the gate (authoritative — the app's gate is only advisory), generate via the
SDK pipeline with write=False (so it NEVER touches battlecards/), save the card under
user_reports/<job_id>/, and advance the gate ledger. The workflow's `concurrency` group serializes
runs, so the state read-modify-write never races. The runner commits nothing to git — the backend
writes straight to the data repo via API.

    python scripts/run_selfserve.py
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scout import selfserve
from scout.generate import generate


def main() -> None:
    reqs = selfserve.list_pending_jobs()
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
            selfserve.save_result(job_id, {
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
            selfserve.save_result(job_id, {"job_id": job_id, "status": "error", "finished_at": now,
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
        selfserve.save_state(state)

        selfserve.save_result(job_id, {
            "job_id": job_id, "status": "done", "finished_at": now,
            "slug": res.get("slug"), "cost_usd": total,
            "kept_claims": len(res.get("kept", [])),
            "message": "Report ready.",
        }, markdown=res.get("markdown"), claims=res.get("kept"))
        print(f"{job_id}: DONE  cost=${total}  claims={len(res.get('kept', []))}  "
              f"(used={state['used']}/{state['free_limit']}, spend=${state['spend_usd']})")


if __name__ == "__main__":
    main()
