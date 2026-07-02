# Spec: the consequentiality filter (one gate) + thorough retrieval, eval-safe

> Status: SPEC, not built. Agreed with Uroš 2026-06-28. Build behind flags, shadow-first.
> Prereq shipped: monitor is now **one daily run, Mon–Sat, per-card cadence** (commit 29575ea) and
> **per-run cost logging** (commit c4bd556). This spec covers the next two pieces:
> **#2 thorough retrieval** and **#3 the consequentiality/strategic filter**, plus how both stay
> compatible with the v3.5 shadow eval.

## Why

Scout's monitor is good at "did a fact change?" (material) but spends the same money rewriting
rep-facing prose whether the change moves a deal or not. Most material changes are **not
consequential** ("Anthropic's own employees can access Mythos," "Supergirl grossed $X today"); a few
are ("the government is thawing"). The goal: **keep the card factually current cheaply, and spend the
expensive retrieval + rewrite only on the changes that actually shift the rep's play or the thesis.**

Since Scout is a **showcase with no reps**, the judgment is the model's to make (no rep-feedback
signal), and the only ground truth is our own spot-check. That shapes the eval (below).

## Confirmed decisions (do not relitigate)

1. **One filter per card per run, not per fact.** A single call sees all of today's grounded facts
   plus the current card and decides which are fact-patches vs the one consequential update. This is
   **the existing strategic pass (`scout/strategy.py::strategic_lead`) used as the gate**, not a new
   concept. Consequential and strategic are the same judgment.
2. **Thorough retrieval (#2) runs only inside the consequential (YES) branch.** We never pay for a
   retrieval hedge just to then decide a change wasn't worth it.
3. **Non-consequential material facts still patch the card and show in its history**, with no rewrite
   and no push. The card itself is the "developing-situation thread"; accumulated patches let a later
   run's filter promote a story once it tips (promotion for free).
4. **Capture-before-filter ordering (eval safety).** `shadow.capture` (the grounding keep/cut the
   v3.5 champion/challenger eval studies) MUST run upstream of the filter, exactly where it is today
   (`monitor.py` ~L627, after grounding, before propagation). Do not move it.
5. **Capture the filter's own decisions from day one**, reusing the shadow/adjudicate infra, so the
   filter is itself evaluable. Full kappa-scoring is optional until labels exist; periodic human
   spot-check is the minimum.

## The pipeline (authoritative)

```
DAILY RUN, one card  (monitor.check / run_card)
│
│  ── cheap, every run ──────────────────────────────────────────────────────
├─ 1. DETECT            triage: new signals since last_checked?  none → DONE (~free)
├─ 2. MATERIAL + GROUND materiality judge + grounding → grounded act-facts
│        │                 (immaterial/unverifiable → REJECTED, logged)
│        └─ shadow.capture(kept, cut, grounding)   ← v3.5 eval record. STAYS HERE, upstream of #3.
│
│  ── one cheap Opus call per card, only when step 2 produced act-facts ──────
├─ 3. FILTER (= strategy.strategic_lead, as the gate)
│      input:  meta + current claims (the accumulated card) + today's grounded act-facts
│      output: { consequential: bool, rationale, lead | null, fact_patches: [subject_key…] }
│      └─ filter_capture(...)   ← NEW eval record (fold B), written regardless of verdict
│        │
│        ├─ NOT consequential → FACT-PATCH (cheap, deterministic):
│        │     update the underlying claim value; surface in the card's history/change-feed.
│        │     NO propose, NO best-of-N, NO push.  → "kept current, not published as a change"
│        │
│        └─ consequential → EXPENSIVE branch (the only place money goes):
│              4. THOROUGH RETRIEVAL (#2): run propose/search N× (default 2), judge-pick the most
│                 complete. (Deliberate version of what run #2 did by luck on the Mythos saga.)
│              5. REWRITE: draft the rep-facing change / strategic lead from the best retrieval.
│              6. VERIFY: the existing adversarial propagation judge confirms accurate + supported.
│                    ├─ confirm → PUBLISHED (queued for approval in review; applied in live) + push
│                    └─ reject  → REJECTED (logged with reason, card untouched)
│
└─ 7. COST LEDGER (already shipped): per-phase spend for the run.
```

## #3 — the filter

- **Placement.** In `monitor.run_card` (inside `check`), after the grounding/`shadow.capture` block
  and BEFORE the propagation block. Today propagation runs unconditionally on `act_facts`; wrap that
  block so it runs only when the filter returns `consequential: true`.
- **Implementation.** Refactor the existing strategic pass. Today `strategy.strategic_lead(meta,
  claims)` runs in `run_all` AFTER propagation and emails a "STRATEGIC SHIFT." Move it to be the
  **pre-propagation gate** in `run_card`, taking the day's grounded act-facts as input, and have it
  return both the gate verdict and (if consequential) the lead. One call does the gate + the
  strategic selection.
- **Contract (JSON):**
  `{ "consequential": bool, "rationale": str, "lead": {headline, proof, soundbite, move, …} | null,
    "fact_patches": [subject_key, …] }`
- **Model / cost.** `ORCHESTRATOR_MODEL` (Opus), tools-off, via the existing `_drive` plumbing +
  `JUDGE_MAX_TURNS`/`JUDGE_MAX_BUDGET_USD` guards. One call per card per run, only when there are
  grounded act-facts (most days: none → free). ≈ $0.27–0.40 on act-fact days (ledger tracks it).
- **Conservative bias.** When unsure, return `consequential: true` (a false positive costs a rewrite;
  a false negative silently buries a real shift). Low stakes for a showcase, but bias this way.

## #2 — thorough retrieval (best-of-N)

- **Placement.** Only in the consequential branch, at the propose step (`propagate.propose`, and/or
  the lead-drafting retrieval).
- **What.** Run propose/retrieval `N` times (config `SCOUT_RETRIEVAL_N`, default 2), judge-pick the
  most complete/accurate via the existing Opus judge. `N=1` disables the hedge.
- **Justification.** The run#1-vs-run#2 Mythos gap was **retrieval variance** (run #2 found the
  Annex A source), not reasoning. Best-of-N hedges that variance deliberately, only where it matters.
  It is NOT a reasoning lever, do not apply it to non-consequential changes.
- **Cost.** `(N-1)` extra propose calls + one judge-pick, only on consequential changes.

## Non-consequential path

- The fact is already grounded + captured (step 2). The filter says not-consequential.
- **Action:** a cheap, deterministic fact patch (update the claim value via a fact-only update / a
  narrowed `apply_ops`), surfaced in the card's existing history/change-feed. No propose, no
  best-of-N, no push/email.
- **Card-as-accumulator:** patched facts stay on the card, so the next run's filter sees them in
  context and can promote the accumulated story to consequential when it tips. No separate thread
  data structure.

## Eval integration

### Fold A — capture-before-filter (preserve the v3.5 shadow eval)
- The champion/challenger eval studies the **claim keep/cut** grounding decision, captured by
  `shadow.capture`. It is upstream of, and independent from, the filter and best-of-N (which live in
  propagation). Keeping `shadow.capture` where it is means the eval sees the exact same decisions it
  sees today.
- **Pin it:** add a code comment at the capture site and a unit test asserting `shadow.capture` is
  invoked for material changes **regardless of the filter verdict**. Never move capture below #3.
- Cadence note: 1×/day + skip-Sunday slows live capture (~6 runs/wk vs ~14). Use
  `scripts/backfill_shadow.py` to top up the corpus offline if the eval needs more volume. The
  offline challenger run is decoupled from monitor cadence and is unaffected.

### Fold B — capture the filter's decisions
- **New record** `filter_capture(slug, run_ts, facts_summary, verdict)` → a shadow-style log
  (e.g. `shadow_eval/filter/<slug>/<ts>.json`, or `shadow.capture` extended with a `kind="filter"`),
  written **regardless of verdict**, gated on `SHADOW_EVAL_ENABLED`. Records: each grounded act-fact,
  a compact card-context summary, the `consequential` verdict + rationale, and the chosen lead.
- **Adjudication:** extend `scout/adjudicate.py` to surface filter decisions for human spot-check
  (consequential vs not, was it right?), reusing the existing deltas/labels store. Metric later:
  `kappa_filter_vs_human` once labels exist; for now, periodic eyeball.
- **Do NOT** build a parallel eval pipeline; reuse shadow/adjudicate.

## Config / flags

- `SCOUT_CONSEQUENTIAL_FILTER` — `shadow` | `gate` | `off`. **Default `shadow` first:** the filter
  runs and logs its verdict but does NOT yet gate (behavior unchanged; everything still propagates),
  so we can compare what it WOULD suppress against what propagation actually did. Flip to `gate` once
  a week of verdicts spot-checks clean.
- `SCOUT_RETRIEVAL_N` — best-of-N for the consequential branch. Default `2`. `1` disables.
- Reuse `PROPAGATE_MODE`, `STRATEGIC_PASS`, `ORCHESTRATOR_MODEL`, the JUDGE budget guards,
  `SHADOW_EVAL_ENABLED`.

## Build phases (shadow-first, cost-bounded)

0. **Eval safety.** Pin capture-before-filter (comment + test). No behavior change.
1. **Filter in shadow.** Add the filter call (refactor `strategic_lead` into the pre-propagation
   gate), log its verdict per card per run via `filter_capture`. NO gating yet. Spot-check a week.
2. **Gate.** Flip `SCOUT_CONSEQUENTIAL_FILTER=gate`: non-consequential → fact-patch path;
   consequential → propagation. Watch the cost ledger before/after.
   *Piping SHIPPED 2026-07-02 (dormant):* the branch sits in `propagate()` right after `route()` —
   an explicit routine verdict skips author/judge/rewrite, records every routed op as
   `gated_routine` in the decision log, and a digest audit line ("N routed updates deferred")
   makes the deferral visible; fail-open on a missing/empty verdict. The flip is one env line in
   monitor.yml. Shadow evaluation continues; decision checkpoint Friday 2026-07-10.
3. **Thorough retrieval.** Add best-of-N in the consequential branch (`SCOUT_RETRIEVAL_N`).
4. **Filter eval.** Adjudication surface for filter decisions; kappa once labels exist.

## Risks / open questions

- **Filter false-negative** buries a consequential change silently. Mitigation: shadow-first
  validation, conservative bias, the cost ledger, spot-check. Low stakes (showcase, no real deal).
- **Fact/narrative drift:** a patched fact with no prose change could desync the card's facts from
  its narrative. Mitigation: surface patches in the history feed; the next filter run sees them.
- **Filter cost** (~$0.30) on act-fact days — bounded (only those days), tracked by the ledger.
- **Promotion timing:** "when does an accumulating story tip?" is a gradient the filter judges each
  run against the whole card. Accept some imprecision; it's a showcase.

## What NOT to do

- Don't move `shadow.capture` below the filter (breaks the v3.5 eval's input).
- Don't run best-of-N outside the consequential branch.
- Don't push/email non-consequential changes.
- Don't build a second eval framework; reuse shadow/adjudicate.
- Don't ship the gate before the shadow phase spot-checks clean.
