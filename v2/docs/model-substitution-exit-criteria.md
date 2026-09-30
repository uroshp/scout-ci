# Model-substitution exit criteria (pre-registered 2026-09-28, before any replay data was read)

Sibling of [eval-exit-criteria.md](eval-exit-criteria.md), which governs the two takeover judges
(verification challenger, authorship judge) and deliberately scopes itself to them. This document
governs a THIRD incumbent: the **live paid model per role**, and asks whether an on-device model
could hold that role. Nothing here changes the older lanes' bars.

## The two questions

1. **Judgment (exact replay).** On identical inputs, does the candidate decide the same way the live
   model did, and when they disagree, who is right? Roles: route, author, judge, lead_election,
   rewrite, reformat, gate_judge, challenger, persona.
2. **Outcome (own tools).** If the candidate ran the whole step with its own search and fetch, would
   it reach the same result? Roles: triage, materiality, my_facts. Three modes: `loop`
   (retrieval-inclusive), `frozen` (judgment-only: the live run's recorded tool results inlined; the
   number comparable to question 1), `--repeat` (a later re-run of the same loop call, the $0
   variance floor).

## Arms

- Reference: the live paid model as it ran (one sample; its self-agreement ceiling is `unmeasured`
  until the optional paid re-run is authorised).
- `apple_ondevice`: Apple's on-device `SystemLanguageModel` (macOS 27, one fixed setting, no
  reasoning level, guardrails default), via `fm serve`, temperature 0, schema-constrained JSON.
- The Ollama arms (one backend NAME per model, each its own results folder, scorecard column and
  streak; run one after another, each alone on the box, unloaded before the next loads; the order
  rotates by weekday; per-arm cap 45 min inside the 05:00-08:30 window). Registry:
  `replaybackends.OLLAMA_MODELS`. All thinking on where the model supports it, `num_ctx` 49152
  (raised from 16384 on 2026-09-29: Scout's largest prompt is ~15.6k tokens), temperature 0, seed.
  - `ollama` = Mistral AI, Magistral Small 1.2 24B q4_K_M (the original arm; the name stays so
    its labels and streaks hold).
  - `ollama_nemotron` = NVIDIA, Nemotron 3 Nano 4B q8_0 (added 2026-09-30). NVIDIA's 30B MoE
    builds are 23-25 GB on Ollama and cannot sit fully on a 24 GB box, so the edge-class model
    represents the vendor; its arm is read as "edge model vs 24B-class", like Apple's.
  - `ollama_gemma4` = Google, Gemma 4 26B (26B/4B-active MoE, QAT build, 16 GB; added 2026-09-30).
  A swap of any arm's tag is a new period, never silent. An arm joins the `common` population
  once it has attempted 20 calls in the period; until then it reports `full` only ("warming up").

## Ground truth and the metric (same as the older lanes)

- Ground truth is **human adjudication**, blinded, truth-first: for classification roles the human
  types the truth in the role's own vocabulary without seeing which model said what; candidate-right
  and reference-right are derived from that one label for every arm. Prose roles use a blinded
  pairwise pick (A|B|tie; ties never count as wins).
- The metric is **disagreement precision**: of the adjudicated items where the candidate overruled
  the reference, the share where the candidate was right. Agreement with the live model is reported
  as a sanity number only; matching the incumbent is not the goal (its blind spots would be inherited).
- Kappa (`challenger.cohens_kappa`) is computed only for CLASSIFICATION-family roles, on pooled
  paired labels, and only as alignment. Prose and set-valued roles report their own measure.

## Bars (per backend × role)

| Gate | Bar |
|---|---|
| Coverage (attempted, not `context_exceeded`) | ≥ 0.95 of the role's eligible live calls, printed as an eligibility precondition; never fed to the streak |
| Parse reliability | `parse_ok` ≥ 0.98; `parse_slip` ≤ 0.02; `refusal + guardrail` ≤ 0.01 |
| Headline | candidate-right on adjudicated disagreements ≥ **0.50** (non-inferiority parity). Any $0-justified margin is a number Uroš writes here and the check-in prints as "margin −x". Current margin: none. |
| Sufficiency | ≥ 15 adjudicated AND ≥ 5 distinct calls AND ≥ 2 cards per (backend, role) |
| Slices (prompt-size bucket, card, mode) | each must clear the bar once it has ≥ 5 adjudicated, or the row is capped at WATCH |
| Costly error | the candidate's costly-error rate ≤ the reference's on the identical adjudicated set |
| Sustained | two consecutive check-ins; KILL? after 3 non-improving |
| Loop mode floor | human-confirmed material items the candidate missed ≈ 0 (the live-scored miss rate printed as a labelled proxy) |

Costly directions: judge wrong confirm; route missed consequential; challenger slop; election
wrong promotion; reformat/rewrite a confirmed update lost or held; gate_judge false pass; my_facts
fact broader than source; persona wrong class; author op failing the floor; triage local quiet on a
live escalation; materiality missed material.

## Populations and periods

- Every field is computed twice: on the `common` population (call_ids where every enabled backend
  returned something other than `context_exceeded`) for the backend-vs-backend headline, and on
  `full` for each backend's own bar. Refusals and parse failures stay in and count against their
  backend.
- Period key = `(backend_version, reference_model)`. A new key resets the streak and prints "new
  period, no prior". Results are never pooled across periods.
- Reference eligibility: `status == ok`, parses with the live parser, `record.model` equals the
  role's primary model. Fallback-judged and unparsed references are tallied `reference_unusable`.

## Verdict vocabulary

Shared with the older lanes via `scout/evalrule.py`: ACCUMULATE, BASELINE, ELIGIBLE, WATCH,
DIAGNOSE, KILL?. ELIGIBLE here means "non-inferior and sustained; no production switch exists". The
model check-in snapshots only on the 1st and 15th; nightly replays print but do not advance the
streak.

## What this lane does NOT do

No production switch, no fallback-to-local, no auto-promotion. The first 14 nights produce a
BASELINE at most. This lane is blinded; the two older lanes were not. What "blinded" covers: the
labelling surface (`adjudicate_models` show/label/prefer) never names an arm; outputs are shown
without their source and prose pairs sit in A/B slots by pair-id parity. The scorecard and the
1st/15th email name arms, because they report outcomes after labelling, not during it.

## Addendum 2026-09-28: Ask Scout roles (pre-registered before the first capture)

`ask_research` (generative, tools-on; facts judged like `my_facts`), `ask_verify` (classification,
unit = answer sentence, confirm | reject; costly direction = a wrong confirm, the same as `judge`),
`ask_rewrite` (generative, tools-off). Same bars, same populations, same period rule. The Ask
loop's model-free floor runs before `ask_verify` in production, so a replay of `ask_verify` sees
only sentences that already passed the floor: the comparison is of judgment, not of the floor.
