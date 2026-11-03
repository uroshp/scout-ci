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
- `apple_ondevice`: Apple's on-device model as `fm serve` exposes it (macOS 27, one fixed setting,
  no reasoning level, guardrails default), temperature 0, schema-constrained JSON; the served
  model's name is read from `fm models` and recorded in the version (see below).
- The Ollama arms (one backend NAME per model, each its own results folder, scorecard column and
  streak; run one after another, each alone on the box, unloaded before the next loads; the order
  rotates by weekday; per-arm cap 45 min inside the 05:00-08:30 window). Registry:
  `replaybackends.OLLAMA_MODELS`. Seed, generation bounded by the role's output reserve plus the
  arm's reasoning reserve (`num_predict`). Reasoning ON for every arm since 2026-11-02 (Uroš: a
  local arm that cannot reason is not a fair comparison against a cloud judge that does). Magistral:
  temperature 0, reserve 2048, `num_ctx` 49152 (raised from 16384 on 2026-09-29: Scout's largest
  prompt is ~15.6k tokens). Nemotron and Gemma 4 ran reasoning OFF from 2026-09-30 to 2026-11-02
  because, at temperature 0, both looped through the whole reasoning budget on a long prompt and
  answered nothing; the vendor cards say greedy decoding is for reasoning-off mode. Each now runs its
  vendor's recommended reasoning-mode sampling (NVIDIA Nano card: temperature 0.6, top_p 0.95;
  Gemma 4 card: temperature 1.0, top_p 0.95, top_k 64) with a reasoning reserve of 8192 (NVIDIA's RAG
  blueprint uses the same budget). A call still reasoning at the bound is continued ONCE: the trace,
  cut at its last newline, goes back as the assistant's thinking (the renderer puts it in the think
  block), reasoning off, and the model answers under the schema with the role's output reserve; the
  result records `budget_hit`, `continuation` and `done_reason`. Gemma at `num_ctx` 32768 (the 48k
  KV cache spilled off the GPU), Nemotron at 49152. The reasoning budget ADAPTS to the room a prompt
  leaves (`replaybackends.ollama_budget`, 2026-11-02, Uroš: no exclusions): a prompt that leaves
  the role's answer reserve plus 512 tokens runs, with a smaller budget when it must; only a prompt
  that leaves less is `context_exceeded`. Borderline prompts are counted exactly on the server (a
  one-token call whose evaluation the real call re-uses from the cache); the chars/3 estimate ran
  0.78 to 1.12 of the count on the 11/2 smoke. On the last 14 days of captures nothing is excluded on
  any arm (the longest prompt, a 28.7k-estimate materiality call, counts about 23k tokens).
  Smoke 2026-11-02 (reasoning on, nothing written): Nemotron 10 of 10 answered and parsed in 19 s to
  190 s a call, traces 1.1k to 7.5k characters; Gemma 9 of 10 (the skip above), 52 s to 258 s,
  traces 5k to 15k characters; no budget hit on either.
  - `ollama` = Mistral AI, Magistral Small 1.2 24B q4_K_M (the original arm; the name stays so
    its labels and streaks hold).
  - `ollama_nemotron` = NVIDIA, Nemotron 3 Nano 4B q8_0 (added 2026-09-30). NVIDIA's 30B MoE
    builds are 23-25 GB on Ollama and cannot sit fully on a 24 GB box, so the edge-class model
    represents the vendor; its arm is read as "edge model vs 24B-class", like Apple's.
  - `ollama_gemma4` = Google, Gemma 4 26B (26B/4B-active MoE, QAT build, 16 GB; added 2026-09-30).
  - `ollama_gptoss` = OpenAI, gpt-oss 20B (21B/3.6B-active MoE, MXFP4, 13.8 GB; added 2026-11-02
    as the strongest reasoning model that fits the box unchanged; reasoning level "medium", the
    model's default; temperature 1 from its own parameter file; `num_ctx` 49152).
  - `ollama_bolt` = NVIDIA, Nemotron 3.5 Bolt E4B (8B total, 4B effective, Gemma-4-E4B
    architecture, released 2026-10-23, q8_0 9.8 GB, 64k window; added 2026-11-02 with the Ollama
    0.40.13 upgrade, which 0.34.4 forced: it refused the pull). NVIDIA's current edge model and the
    Nano 4B's successor; reasoning on, temperature 1.0, top_p 0.95, top_k 64 per its card and its
    own parameter file. Smoke 11/2: 10 of 10 answered and parsed, 27 s to 196 s a call. The 74B Bolt
    (2-bit experts, 22 GB of weights, NVIDIA's recipes start at a 32 GB RTX card or a 64 GB Spark)
    does not fit a 24 GB Mac, whose GPU gets 17.8 GB by default and about 21.5 GB at most.
  - `ollama_nano30b` = NVIDIA, Nemotron 3 Nano 30B-A3B (30B total, ~3.5B active, hybrid Mamba-2
    MoE), the peer to Gemma 4 26B-A4B; unsloth's IQ4_XS GGUF, 18.2 GB (Ollama's own builds are 24 GB
    GGUF or MLX-only, and MLX needs a 32 GB Mac). Fits once macOS lets the GPU take 21 GB:
    `sudo sysctl iogpu.wired_limit_mb=21504` (Uroš, 2026-11-02 21:20 PT; resets at reboot, so a
    LaunchDaemon or /etc/sysctl.conf entry is the follow-up). Reasoning on, temperature 0.6, top_p
    0.95 per NVIDIA's card; `num_ctx` 49152. By-hand proof 2026-11-02 21:30 PT on the raised limit:
    17.95 GB fully in GPU memory ("offloaded 53/53 layers", KV 153 MiB at 49k; Metal working set
    22.5 GB); a judge call answered and parsed with a 28k-character trace at ~27 tok/s in 259 s; the
    forced cutoff rescued a judge answer and the challenger answered inside the bound. It reasons at
    length, so expect budget hits near 8192 on long prompts and about 4 min a call.
  - `ollama_nemotron` = NVIDIA, Nemotron 3 Nano 4B q8_0: RETIRED 2026-11-02 (ran 9/30 to 11/2,
    reasoning off). Its 1,117 results stay as history under the settings they ran with
    (`replaybackends.RETIRED_OLLAMA`); it is not in the rotation, the canary or the How panel.
  - Engine: Ollama 0.40.13 since 2026-11-02 (0.34.4 from 9/23). Every result records the server
    version (`backend_version.server`) for provenance; the engine is NOT part of the settings key,
    or no upgrade could happen without resetting every arm. Gate before the switch: three stored
    Magistral answers replayed on the new engine came back byte-identical.
  - Magistral's reasoning (found 2026-11-02): in all 910 stored answers the trace is empty. The
    model opens its <think> block only when its own system prompt asks for it (the text in the
    Ollama model's `system` layer), and Scout's prompts replace the system prompt; it also ran at
    temperature 0 against its own 0.7 / 0.95. So "thinking on" was nominal. The harness now takes a
    per-arm `system_prefix` (hashed into the settings key) for exactly this; Magistral runs with
    `MAGISTRAL_SYSTEM` + 0.7 / 0.95 since the night of 2026-11-02 (Uroš: "tonight"), a new period;
    the nightly read-out prints "reasoning traces N of M answers today" per arm and flags zero.
  - `apple_ondevice`'s version records the model `fm models` reports as "system" (2026-11-02:
    "AFM 3 Core Advanced", the 20B sparse model, since macOS 27.2; the arm had assumed the 3B
    "instruct_3b" from 9/28). Its window is still 8,192 tokens (probed 11/2: 8,070 answered,
    9,570 refused), so its coverage does not change.
  A swap of any arm's tag is a new period, never silent. An arm is "warming up" until it has
  attempted 20 calls in the period. The `common` population is per ROLE (2026-10-19): an arm joins
  a role's population once it attempted at least 90% of that role's calls (`COMMON_COVERAGE`); an
  arm under that reports its partial numbers marked "NOT comparable". The paid reference re-run
  (`anthropic`) is scored like any arm and never defines a population. Until 10/19 one intersection
  ran across every arm, so Apple's 8k context and the reference re-run emptied it for arms that had
  run the whole set.

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

- Every field is computed twice: on the role's `common` population (call_ids every comparable arm
  returned something other than `context_exceeded` for; see above) for the backend-vs-backend
  headline, and on `full` for each backend's own bar. Refusals and parse failures stay in and count
  against their backend. For the roles whose unit is a candidate the rater itself names (screen,
  screen_sample, materiality) a reference item the candidate never named counts as the quiet
  verdict (minor, immaterial): a disagreement when the reference said otherwise, an abstain when it
  agreed, and never an item for the arbiter (no verdict was written). The table prints these as
  "+N unnamed" next to the disagreements (2026-10-19: an empty candidate list read as agreement n/a).
- Period key = `(backend_version, reference_model)`. A new key resets the streak and prints "new
  period, no prior". Results are never pooled across periods: the scorecard keeps, per Ollama arm,
  only the rows whose recorded settings (tag, context, reasoning, sampling: `replaybackends.settings_key`)
  are the arm's current ones and reports the rest as `history` (enforced since 2026-11-02; before
  that the rule was documented, not applied). A settings change therefore starts the arm's cells
  empty ("warming up" until 20 calls); `scripts/replay_calls.py --redo-period` re-runs the window's
  calls whose stored result is from an earlier period (run it with the store mirror env so the
  per-result reads come from the clone, not the API).
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
