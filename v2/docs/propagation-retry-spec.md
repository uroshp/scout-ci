# Propagation retry — spec

**Status: SPEC ONLY, not built.** Written 2026-08-31 after the 8/31 incident. Owner approves
before implementation.

## The gap

`monitor.check()` runs detection first and propagation second. Detection's writes are durable:
grounded facts are patched into `claims.json`, alerts land in `alerts.jsonl`, and every alert's
fingerprint is stamped into `meta.alerted_fingerprints` — which is exactly what makes the same
development invisible to every later run. Propagation's worklist (`act_pairs`, monitor.py ~786) is
built ONLY from facts detected in the same run. So when `propagate()` raises after detection has
landed, the failure path (monitor.py ~845) records `propagation_error`, emails pipeline_health, and
that is the end: the facts are permanently "old news" that no run will ever route into plays,
objections or the lead.

Observed 2026-08-31: the propose/author stage hit its $0.60 budget cap on a two-fact day on the
Mistral card. Two act-grade facts (OpenAI's Nov-12 Cursor cutoff; the METR/Redwood breach
postmortem) reached the feed and snapshot but will never reshape the rep-facing prose. The budget
cap was raised to $1.00 the same day, which makes the crash rarer — this spec removes the
permanent-loss failure mode itself.

## Design principle

Mirror the existing unresolved-DETECTION window (`unresolved_since` / `unresolved_subjects` /
`unresolved_attempts`, `_hold_window` / `_resolve_or_hold`): bounded retries, loud abandonment,
state on the card's `meta`. Propagation gets the same discipline one stage downstream. No new
storage, no new email type, no model calls added on quiet days.

## State (meta.json, per card)

On propagation failure, inside the existing `except` (monitor.py ~845):

```json
"propagation_retry": {
  "since": "<checked_at of the failed run>",
  "attempts": 0,
  "error": "Exception: ... (truncated, for the audit trail)",
  "pairs": [
    {"fact_id": "c_...", "fingerprint": "f_..."}
  ]
}
```

`pairs` stores REFERENCES, not copies: the fact claim is already durably on the card (patched by
`_apply_updates` before propagation ran), and the alert record is already in `alerts.jsonl` keyed
by fingerprint. Reconstruction is a lookup, not a re-derivation. If a retry window already exists,
merge new pairs in (dedupe by fingerprint) and keep the older `since` — never lose a prior failure
to a newer one.

All three write branches at the bottom of `check()` persist `meta`, and the failure path always has
`new_alerts` non-empty (act-grade pairs imply alerts), so the state rides the existing
`store.write_baseline` — no extra write.

## Replay (next run)

Injection point: in `check()`, immediately after the `act_pairs` build (~786), BEFORE the
`PROPAGATE_MODE` gate:

1. Load `meta.propagation_retry`. For each pair, resolve `fact_id` against the current claims and
   `fingerprint` against `alerts.jsonl`. A pair whose fact no longer resolves (retired, pulled) is
   dropped with a log line — its subject moved on.
2. Prepend the resolved pairs to this run's `act_pairs` (dedupe by fingerprint against fresh
   pairs). `strength_facts` need nothing: they are recomputed deterministically every run
   (`strengths.get`, no spend).
3. **The quiet-day path must replay too.** The early return at "not substantial and not do_my"
   (~737) currently skips propagation entirely; a retry window must divert that return into the
   propagation block (with today's own `act_pairs` empty). Rationale: the whole point is that the
   news day already happened — waiting for the next news day is exactly the bug. This is the one
   structural change in the diff; everything else is additive.
4. On a `propagate()` call that RETURNS (regardless of verdicts — reject-all, `gated_routine`, and
   zero-confirmed are all legitimate outcomes, not failures): clear `propagation_retry`. The
   decision log is the durable record, as it is for first-try runs.
5. On a `propagate()` call that RAISES: increment `attempts`. At
   `SCOUT_PROPAGATION_RETRY_MAX` (config, default **2**), clear the window and put an ABANDONED
   line into `pipeline_health` (same email path as today: "propagation abandoned on <slug> after N
   retries: <error> — facts <subject_keys> remain on the card unrouted"). Loud, never silent,
   mirroring `abandoned_window`.

`log_decisions` gets `source="monitor-retry"` on replayed runs — auditable in the store, and
`review._latest_log` only skips `manual*` sources, so the proposals email and `scout-proposals`
pick the results up exactly like a normal run.

## Interactions checked

- **Conseq gate**: a replay that comes back `gated_routine` counts as processed (the gate's
  deferral is its own recorded decision) — window cleared.
- **Lead election**: rides `propagate()` unchanged.
- **Fallback judge**: unchanged; a fallback-judged confirm still gates email-only in live mode.
- **Cost**: a replay costs the same as the propagation that should have happened (~$1.50–2.50 on a
  two-fact day: route ~$0.90 + author ≤$1.00 + judge ~$0.90), bounded at 2 attempts. Quiet-day
  replays add zero detection cost (triage already ran; strengths are model-free).
- **Double-propagation risk**: none — the window is cleared on any successful return, and pairs
  are deduped by fingerprint on merge and on replay.
- **`unresolved_since` windows**: orthogonal (detection-stage vs propagation-stage) and can
  coexist; neither clears the other.

## Tests (tests/test_monitor_propagation.py conventions: mock `_run_triage`, `propagate`,
`store.*`, `_append_alerts`, `_current_md`)

1. Propagation raises → `meta.propagation_retry` written with the failed pairs' fact_id +
   fingerprint; alerts still land (regression on the non-disruption contract).
2. Next run, quiet day → `propagate` called with the reconstructed pairs; window cleared on
   return; decision log `source="monitor-retry"`.
3. Next run, news day → retried pairs prepended to fresh ones, deduped by fingerprint.
4. Reject-all / `gated_routine` return → window cleared (a verdict is not a failure).
5. Raise at `attempts == SCOUT_PROPAGATION_RETRY_MAX - 1` → window cleared +
   `pipeline_health` carries the ABANDONED line.
6. Pair whose fact was retired/pulled between runs → dropped, no crash, logged.

## Out of scope

Re-detecting the facts (they are already grounded and on the card), retrying `route`/`author`/
`judge` individually inside one run (the budget cap is the per-stage control), and any backfill of
the 8/31 Mistral pair (owner declined the manual re-run on 2026-08-31; this spec is forward-only).
