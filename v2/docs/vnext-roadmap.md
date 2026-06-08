# Scout vNext — v3 and Beyond

> Forward-looking direction, written June 2026, after v2 shipped and went on the resume / LinkedIn.
>
> This is a deliberately different document from [`../ROADMAP.md`](../ROADMAP.md). That one catalogs
> v2's *known limitations* (the gold-plating I chose not to do). This one is the *next-generations*
> map: what to build after v2, and why.
>
> **Framing.** Scout is a **learning showcase, not a customer-facing product.** The goal of each
> future version is to learn-by-building one frontier agentic-AI capability *and* produce an artifact
> that reinforces AI PMM/GTM credibility. Depth on the thing that's uniquely mine (verification)
> first; breadth on the flashy frontiers (swarm, browser, memory) after, and honest about which is
> which.

---

## The through-line: where the frontier went, and the one thread that's mine

Between when Scout started and mid-2026, the agentic-AI frontier moved *toward* the instinct Scout
was built on. The two-pass generate-then-verify pipeline — "the trust-critical layer is model-free
by construction" — is now the industry's named discipline: **eval-driven development, verifiers,
reward models, trace-based evals.** Observability adoption is high (~89%) but *evals* lag (~52%),
meaning the exact discipline Scout already has an opinion about is the one most teams are still bad
at. (Sources at the bottom; current as of June 2026.)

So the next versions are not a "what to build" problem. They're a "which frontier capability to
learn by building, in what order" problem — and the answer leads with the thread that's already
mine.

---

## The thesis, stated precisely (load-bearing for v3)

Scout's defining claim, said loosely, is *"the arbitration is not dependent on model judgment."*
The precise, defensible version — the one v3 is built to honor and demonstrate — is:

> **No ungrounded model judgment ever sits in the seat of final authority.**
> A model may *run* a check; it may not *be the source of truth.*

- In **production**, the un-fakeable source of truth is the **real citation URL** (a model can't
  fabricate a web_search citation object).
- In the **eval harness** (v3), the equivalent un-fakeable ground truth is a **human-labeled gold
  set**: briefs where a real analyst has marked, by hand, which claims should survive and which
  should be cut.

A model is an *instrument* in both layers; the *authority* is always something external it cannot
forge. This is why v3 does not contradict the thesis — it applies the thesis one level up. v3 is
**the Cut Log of the Cut Log.**

---

## v3 — The Eval Harness (the depth bet)

**One sentence:** turn Scout's verification discipline into a real, trace-based eval system that
measures how well Scout itself performs — grounded in a human gold set, not in a model's opinion.

This is the highest-leverage next move because it is (1) most continuous with what already makes
the project distinctive, (2) aimed at the market's weakest spot (52% eval adoption), and (3) the
deepest concepts — graders, calibration, traces, the generate-verify-score loop — which transfer to
every later version.

### Two layers, never conflated

- **Layer 1 — production arbitration (untouched).** What a *user* sees stays model-free / grounded.
  No LLM-judge ever enters this path. v3 changes zero lines here.
- **Layer 2 — the eval harness (new, offline, developer-facing).** Judges *Scout itself*. Nothing
  it produces is shown to a user or alters a brief. A lab bench, not the product.

### Grader taxonomy, ranked by thesis-purity

1. **Code-based / deterministic graders** — pure, model-free, *maximize these:*
   - every surviving claim carries a real (non-fabricated) citation URL;
   - source-tier labels are internally consistent (no Tier-1A audited-fact label on a self-positioning
     claim);
   - Cut-Log entries are well-formed and the CUT vs REVISED distinction is preserved;
   - no fact/company-claim/estimate/sentiment blurring (the `SOURCE_HIERARCHY` rules, checked).
2. **Grounded model checks** — what the verifier already is: a model runs a disconfirmation
   re-search, but truth comes from external sources, not the model's opinion. Instrument, not judge.
3. **Model-based judge (LLM-judge)** — admissible **only** as a *subordinate instrument measured
   against the gold set*, never the authority. Report its agreement-rate with the human labels. The
   gold set is on the bench; the judge is on trial. **Optional** — v3 can ship fully thesis-pure
   with zero LLM-judge. Including it (clearly subordinated) makes the demo argue the harder,
   more interesting case: *when is a model-judge admissible, and when isn't it?*

### Scope: weekend vs. stretch

**Weekend (the MVP that proves the idea):**
- A small **gold set**: ~10–20 saved briefs (reuse `v1/reports/` + v2 battlecards) with hand-labeled
  keep/cut decisions per claim.
- A **code-based grader suite** (the Layer-1 list above), run over the gold set, emitting a scorecard:
  precision/recall of Scout's cuts vs. the human labels, plus invariant pass/fail counts.
- A **trace** per run: for each claim, what the verifier searched, what it found, why it survived or
  was cut — dumped as inspectable JSON/markdown. (Scout already logs much of this; surface it.)

**Stretch (the part that touches the actual research edge):**
- The **calibrated LLM-judge**: add the subjective grader, then *measure it against the gold set* and
  publish the agreement number. The deliverable is the calibration story, not the judge.
- **Sample-and-score (the verifier/reward-model pattern by hand):** run N independent verification
  passes per claim, score each trajectory, keep the best. This is literally the reward-model/verifier
  loop the frontier is built on — implementing it once makes it speakable.
- **Regression mode:** wire the harness into CI so a prompt or model change that lowers the score
  fails the build. This is eval-driven development in practice — the thing 48% of teams don't do.

**What v3 proves (the showcase line):** *"I don't just build an agent — I built the harness that
tells you, with a number calibrated against human ground truth, when it's right and when it's
lying. And I can show you exactly where a model-judge is allowed to vote and where it isn't."*

### v3.5 — Shadow-mode qualification of the model-judge (champion-challenger)

The model-judge is **not promoted on assumption** — it earns its way in by experiment. This is the
safe answer to "what if the model-judge doesn't work and slop starts showing up?": in shadow mode
the judge has **zero production authority**, so it cannot introduce slop during the trial. Slop can
only reach a reader on promotion, and promotion is gated on data.

- **Champion = the code grader** (in production, authoritative, what ships).
- **Challenger = the calibrated model-judge** — runs alongside on every generated/updated card,
  logs what it *would* have decided, **acts on nothing.** Cheap: both decision streams already
  exist; just log the challenger's column.
- **The unit of study is the disagreement, not the claim.** Agreements only feed the rate. Analyze:
  - *code-cut / model-kept* → potential **recovery** (model surfaced a claim that shouldn't have
    been cut — the upside);
  - *code-kept / model-cut* and *model-flags-a-kept-claim* → potential **slop catch** or **over-cut**
    (the downside directions).
- **A human adjudicates each disagreement.** The model *finding* a recoverable claim is a hypothesis,
  not a verdict — letting the judge's own opinion count as proof puts it back in the authority seat
  (it would be grading itself). The analyst labels who was actually right on each delta; that human
  label is the un-fakeable ground truth — the thesis applied to the experiment. Deltas are few (only
  disagreements), so it's minutes a week.
- **Avoid calibration leakage:** calibrate the judge on the gold set, but *evaluate* it on the fresh
  live stream — never measure it on the data it was tuned on.
- **Asymmetric loss → conservative promotion.** One slop claim reaching a reader is reputational
  poison; over-cutting a few legit claims is merely conservative. Weight precision. Promote only if,
  over the window, the judge is **net-positive on adjudicated deltas AND slop-admission ≈ 0** — and
  even then into a **bounded** role (flag-a-code-cut-claim-for-human-review, or tie-breaker on one
  narrow claim class), **never** wholesale replacement. The code grader stays the floor permanently;
  the model earns a seat at the table, never the gavel.
- **Cost note:** shadow-judging *cut* claims first is the cheap way to answer "does the model add
  value"; add a sample of *kept* claims to catch slop-admission. (He is cost-sensitive — see
  [`../ROADMAP.md`] cost figures.)

This is how reward models / verifiers actually get qualified at the frontier, and it's a stronger
*showcase* than a feature: *"I ran the challenger in shadow for N weeks, mined and adjudicated every
disagreement, and here's the data that did — or didn't — justify promoting it."* An experiment, not
a vibe.

#### Running shadow eval alongside live v2 without disrupting it

v2 is live (on LinkedIn + resume) and must run uninterrupted. The design rides the runs already
being paid for and is split in two so a shadow failure can never touch production:

- **v2 already emits both decision streams** — no new pipeline:
  - `grounding.ground_claims()` *is* the code grader in production; it returns kept + CUT entries
    with reasons, and `GroundingResult.best_ratio` is already computed. The 0.80–0.92 fuzzy band is
    the high-signal target ("watch the 0.80–0.92 band for true-claim cuts") — true-but-cut claims
    where the model-judge might recover value. Free stratification.
  - `generate()`'s `cut_log = model_cut + final_cut` is the verifier CUT/REVISED stream.
- **(A) Capture — rides existing paid runs.** Three real-run sites drop ONE JSON record
  `{slug, source, run_ts, kept[...], cut[...], grounding_results[... best_ratio]}` into a
  **separate** store: `generate()` (write=True roster baselines), the monitor's post-verify point
  (live checks), and the self-serve runner (a real paid card — captured explicitly there because
  self-serve runs `generate(write=False)`, which means "don't touch battlecards/", not "dry run").
  Pure logging: milliseconds, `try/except`-swallowed, gated behind `SCOUT_SHADOW_EVAL=1`. Cannot
  alter or crash the production run. NOTE: the monitor workflow must carry the same
  `SELFSERVE_GH_TOKEN`/`SELFSERVE_REPO` as self-serve, or capture falls back to local-FS in the
  runner and is discarded (gitignored).
- **(B) Judge — a separate scheduled Action (`shadow-eval.yml`).** Own cron; reads unprocessed
  capture records; runs the challenger (Haiku, cheap) over the **captured evidence** — no
  re-research, batched one call per card; appends `{champion, challenger, best_ratio}` rows to an
  append-only log. If it's slow / fails / is turned off, v2 never notices.
- **Non-disruption guarantees:** (1) shadow writes to the private `SELFSERVE_REPO` via the existing
  `selfserve.py` GitHub-API machinery, NEVER the public `battlecards/` — the user-facing card is
  byte-identical with shadow on or off; (2) judging is a separate workflow on a separate schedule;
  (3) the prod hook is capture-only, guarded, feature-flagged; (4) the challenger is read-only over
  captured data — no regenerate, no claim write; (5) the log is append-only + idempotent, keyed by
  `(slug, run_ts, content_hash)`.
- **Cost:** generation/verification (~$8/report, ~$1–1.9/check) is already spent and already yields
  the evidence. The challenger re-judges existing claims against captured evidence → cents per card.
  Ride the existing cadence so the dataset accumulates itself over weeks at zero added generation
  cost. Judge CUT claims first (esp. the 0.80–0.92 band); sample KEPT claims to measure
  slop-admission.

---

## v4 and beyond — the breadth parking lot

These are the fun, demo-able frontiers. Lower credibility-per-hour than v3, higher visual wow.
Parked here so they survive the console. Honest framing: these are **breadth**, and should be
presented as such.

### v4 — Swarm / agent-teams monitoring (orchestration patterns)
Replace v2's single daily re-check with a **supervisor** coordinating per-competitor **specialist
agents** on isolated branches — the production-settled pattern (supervisor first; fan-out where
subtasks are genuinely independent; debate only when the ~2.5× cost buys real multi-perspective
validation). Models now ship this natively (Opus 4.6+ agent teams + 1M context, Feb 2026), so it's
cheap to try, and the diff vs. v2's monitor is a clean before/after story. Teaches multi-agent
orchestration directly.

### v5 — Browser / computer-use deep-research agent (the dessert)
The most demo-able item on the list. Point it at the same CI problem Scout solves, but let it
actually **drive a browser** to gather sources instead of using the search tool. Computer-use went
from research preview to commodity in ~15 months (CUA ~87% on complex JS sites; Project Mariner
~83.5% on WebVoyager), and browser agents are now the backbone of deep-research workflows. Highest
visual impact; weakest credibility-per-hour — which is exactly why it's dessert, not the main.
**Thesis note:** a browser fetcher is also the natural fix for the *"~33% of cited sources are
unreachable by the plain-HTTP grounding fetcher"* limitation already logged in `ROADMAP.md` — used
*only* to confirm grounding on otherwise-cut claims, never to expand the claim set unverified.

### v6 — Long-horizon memory / context engineering (build only on real pain)
The live research problem: agents are far less reliable over 100-step trajectories than 10-step ones
(error accumulation, goal drift, context degradation). The frontier response is *managing* context,
not just enlarging it — memory hierarchies, "demand paging" for context windows, parallel context
routing. **Don't build memory for its own sake.** Build it when the v4 swarm work surfaces a real
context-degradation failure in front of you — then it's motivated, not speculative.

---

## Sequencing rationale

One **deep flag** (v3 evals — the thing already mine, the thing the market is worst at) plus a
couple of **fun breadth projects** I'm honest about. Depth is what a hiring manager can't
fake-detect; breadth keeps it fun and keeps the surface-area current. The trap to avoid is five
shallow trend-demos that all read as "kept up" — v3 is the antidote because it deepens the one
position no one else can credibly take from this profile.

---

## Frontier context (sources, June 2026)

- Multi-agent patterns (fan-out/pipeline/debate/supervisor/swarm; supervisor-first):
  [digitalapplied](https://www.digitalapplied.com/blog/multi-agent-orchestration-5-patterns-that-work),
  [codebridge](https://www.codebridge.tech/articles/mastering-multi-agent-orchestration-coordination-is-the-new-scale-frontier)
- Eval-driven development, graders, verifiers, reward models:
  [Latitude](https://latitude.so/blog/build-eval-driven-ai-observability-for-agents),
  [NVIDIA](https://developer.nvidia.com/blog/mastering-agentic-techniques-ai-agent-evaluation/),
  [Confident AI](https://www.confident-ai.com/blog/llm-agent-evaluation-complete-guide),
  [EDD reference architecture (arXiv)](https://arxiv.org/html/2411.13768v3),
  [LangChain: State of Agent Engineering](https://www.langchain.com/state-of-agent-engineering)
- Context engineering & long-horizon memory:
  [Anthropic](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents),
  [Missing Memory Hierarchy (arXiv)](https://arxiv.org/pdf/2603.09023),
  [Memory in the Age of AI Agents (arXiv)](https://arxiv.org/pdf/2512.13564)
- Computer-use / browser / deep-research agents:
  [Zylos](https://zylos.ai/research/2026-02-08-computer-use-gui-agents),
  [Prosus: State of AI Agents 2026](https://www.prosus.com/news-insights/2026/state-of-ai-agents-2026-autonomy-is-here)
