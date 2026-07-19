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

from scout import config, notify, selfserve, shadow
from scout.generate import generate

MAX_ATTEMPTS = 2   # one requeue, then an honest visitor-facing error (never a raw exception)


def _handle_failure(req: dict, job_id: str, attempts: int, err: Exception, now: str) -> None:
    """The 7/18-incident contract: a failed run (1) counts an ESTIMATED spend against the ceiling
    (a crash still billed — the exact cost is unknowable in-run), (2) emails the OWNER the raw
    error immediately, (3) requeues once, and only after MAX_ATTEMPTS shows the visitor an honest,
    internals-free message. The raw error never reaches a visitor's browser."""
    selfserve.update_request(job_id, attempts=attempts)
    try:  # ledger honesty — an estimate beats the $0 the 7/18 failure recorded
        state = selfserve.load_state()
        state["spend_usd"] = round(float(state["spend_usd"])
                                   + config.SELFSERVE_FAILED_RUN_SPEND_EST, 4)
        selfserve.save_state(state)
    except Exception as e:
        print(f"{job_id}: failed-spend ledger update skipped ({e})")
    try:  # the owner hears about EVERY failure, with the internals the visitor never sees
        notify._dispatch(
            f"Scout: self-serve generation FAILED (attempt {attempts}) — {job_id}",
            (f"Job: {job_id}\nAttempt: {attempts}/{MAX_ATTEMPTS}\nError: {type(err).__name__}: "
             f"{err}\n\n" + ("Left QUEUED — the next dispatch retries it (gh workflow run "
                             "selfserve.yml), or cancel via the request's status field."
                             if attempts < MAX_ATTEMPTS else
                             "Attempts exhausted — the visitor now sees the honest error page. "
                             "To retry: reset attempts + delete the result.json.")
             + f"\n~${config.SELFSERVE_FAILED_RUN_SPEND_EST} estimated spend was added to the ledger."),
            dry_run=False)
    except Exception as e:
        print(f"{job_id}: owner failure-alert skipped ({e})")
    if attempts < MAX_ATTEMPTS:
        print(f"{job_id}: FAILED attempt {attempts} — left queued for one retry. {err}")
        return
    selfserve.save_result(job_id, {
        "job_id": job_id, "status": "error", "finished_at": now,
        "message": ("We hit a snag generating your report and it's been flagged to the owner. "
                    "Check back in a while — this page will update if it's rerun."),
        "detail_internal": f"{type(err).__name__}: {err}",   # owner-only; never rendered
    })
    print(f"{job_id}: ERROR after {attempts} attempts — honest error page shown. {err}")


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

        attempts = int(req.get("attempts") or 0) + 1
        try:
            res = generate(req["competitor"], req.get("my_company"), req.get("focus"), write=False,
                           on_stage=lambda s, j=job_id: selfserve.write_progress(j, s))
        except Exception as e:
            _handle_failure(req, job_id, attempts, e, now)
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

        # Shadow-eval observer (v3.5): a self-serve card is a REAL paid generation, so record its
        # champion decisions too. generate(write=False) here means "don't touch battlecards/", NOT
        # "dry run" — so we capture explicitly at the caller that knows the spend was real (the
        # generate() hook only fires for write=True roster baselines). No-op unless
        # SCOUT_SHADOW_EVAL=1; never raises (scout/shadow.py).
        shadow.capture(res.get("slug"), "selfserve", kept=res.get("kept", []),
                       cut=res.get("cut_log", []), grounding=res.get("grounding", {}),
                       competitor=req.get("competitor"), my_company=req.get("my_company"),
                       focus=req.get("focus"))

        # Optional "your report is ready" email — only if the user left an address AND Resend is
        # configured in this Action. Best-effort: a mail failure must never fail a paid-for job.
        if req.get("notify_email"):
            comp = req.get("competitor") or ""
            mine = req.get("my_company")
            label = f"{comp} vs {mine}" if mine else comp
            try:
                r = notify.send_selfserve_ready(req["notify_email"], job_id, label or None)
                print(f"{job_id}: email {'sent' if r.get('sent') else 'skipped'}"
                      f" ({r.get('reason', r.get('status'))})")
            except Exception as e:   # belt-and-suspenders; send_selfserve_ready already guards
                print(f"{job_id}: email error {e}")


if __name__ == "__main__":
    main()
