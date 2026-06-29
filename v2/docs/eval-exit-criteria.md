# Exit criteria: when a model judge may take over from the code

> Decided with Uroš 2026-06-28. Governs BOTH model judges Scout validates before trusting them to act:
> the **verification challenger** (does the evidence SUPPORT a claim — the v3.5 support layer on top of
> the code grounder) and the **consequentiality filter** (does a change matter enough to publish/spend
> on). Written to be defensible to an expert, not just convenient.

## The decision, in one line

A model judge may take over (or gate) a deterministic step only when it is **non-inferior to the
incumbent on what the incumbent does well, superior on what the incumbent structurally can't, and that
holds up over time and on the rare/nuanced cases**, measured against human adjudication, not against
the code.

## Why "match the code" is the wrong bar (the v3.5 reframe)

The code grounder is the **incumbent baseline, not the gold standard.** It does a shallow presence
check (is the excerpt verbatim on the page), not a support check (does the excerpt back the claim) —
the exact gap that let a true-but-irrelevant excerpt pass live (the OpenAI-pricing contamination).
Training or scoring a model to **agree** with the code teaches it to inherit that blind spot; a model
at 100% agreement would wave the same contamination through. So **agreement-with-the-code is a
worthless target.** The model is a complementary layer, and the operative question is whether its
judgment is reliable enough to act on, **especially where it overrules the code.**

## How the field actually frames "can a model take over" (the defensible vocabulary)

Uroš's instinct maps cleanly onto established practice. Named, so it's interview-defensible:

| What Uroš said | The established frame |
|---|---|
| "not accuracy, accuracy **over time**" | **Online / longitudinal evaluation + drift monitoring + regression gates.** Point-in-time accuracy is insufficient under distribution shift. |
| "agreement in **niche cases**" | **Slice / subgroup evaluation** (worst-case, not aggregate). Aggregate metrics hide failures on rare strata ("hidden stratification"); the rare cases are where a deterministic-replacement fails silently. |
| "high kappa overall + **very high** in niche cases" | **Inter-rater agreement (Cohen's κ) vs human labels**, reported **per slice**, with Landis–Koch bands (0.61–0.80 substantial, 0.81–1.0 almost perfect). κ is bounded by human–human agreement, so we also measure that ceiling. |
| "not **dropping anything important**" | **Error-cost asymmetry.** A false-negative (drop a true/important claim) costs far more than a false-positive. The bar is set on the costly error: bounded false-negative rate / high recall on high-cost cases (**cost-sensitive evaluation**). |
| "improve at 14-day check-ins, else diagnose or conclude it can't" | A **pre-registered go/no-go decision rule with a stopping/kill criterion.** Fix the rule before the data (no moving goalposts), and keep an explicit "abandon" branch. |
| "enough time" | **Statistical sufficiency / power**, especially for the rare stratum, which accumulates slowly — hence weeks, not a snapshot. |
| "can it **take over**" | **Non-inferiority (equivalence) testing**: the replacement need not beat the incumbent everywhere; it must be non-inferior within a margin on the incumbent's strengths AND add the capability the incumbent lacks, with confidence. |

Supporting practices an expert would also expect, which complete the picture:
- **Staged rollout:** shadow / silent deployment → canary → gated → autonomous. (Scout is in shadow.)
- **A held-out gold set + a clear adjudication protocol** (who labels, blind where possible).
- **Calibration** (if we act on the model's confidence, is it right at the rate it claims?).
- **Guardrail metrics + rollback after promotion** — promotion is revocable, not permanent.
- Both error directions tracked separately: **recovery** (code cut, model keep) and **slop** (code keep, model cut); and for the filter, **missed-consequential** vs **over-flagged**.

## The bar for Scout (operationalized)

1. **Ground truth = human adjudication**, not the code. Promotion metric is **disagreement precision**:
   when the model overrules the incumbent, is the model right (human-confirmed)? Plus **κ vs human**,
   overall and per slice.
2. **Slices that must each clear the bar**, not just the aggregate: by claim type (fact vs
   interpretation), by section (e.g. objection_handling, the historically hard one), and by the rare
   "overrule" cases. Niche cases get the **higher** threshold.
3. **Error asymmetry:** the binding constraint is **never dropping something important** — bounded
   false-negatives on high-cost cases. We would rather tolerate over-flagging than a silent miss.
4. **Cadence: a check-in every 14 days**, over multiple weeks, until the rare-case sample is large
   enough to conclude. Each check-in reads the trend, not a single snapshot.
5. **Pre-registered decision rule at each check-in:**
   - κ (overall and per slice) **at or above bar AND still improving / stable at ceiling**, and
     missed-important ≈ 0 across the window → eligible to promote (shadow → gate, gate → autonomous).
   - κ **below bar and NOT improving** vs the prior check-in → **diagnose** the cause (capture gap,
     prompt, model). If fixable, fix and reset the window. If not fixable after a bounded number of
     check-ins → **conclude the model can't take over here**, and keep the code in charge. This kill
     branch is a valid, expected outcome, not a failure.
6. **Sample sufficiency:** enough adjudicated cases, especially in the rare stratum, that the per-slice
   numbers aren't noise. (This is why time matters: the niche cases trickle in.)

## Applies to both judges

- **Verification challenger:** incumbent = code grounder; bar = non-inferior on presence + superior on
  support (the contamination class), disagreement precision vs human, per-slice κ, sustained.
- **Consequentiality filter:** incumbent = "propagate everything act-grade" (today's behavior); bar =
  never suppresses a change that turns out consequential (the costly error), disagreement precision vs
  our own adjudication of consequential-vs-routine, sustained over check-ins. Same shadow → gate →
  (eventually) trusted path.

## Honest scope note

The named frameworks above (non-inferiority testing, slice-based eval, LLM-as-judge agreement, staged
rollout, pre-registered gates, drift monitoring) are standard methodology. "A model taking over a
*deterministic* check" is a specific, less-templated case; the framing here (non-inferiority on the
incumbent + superiority on its blind spot, validated longitudinally on slices against human ground
truth) is the principled adaptation. Current lab sources can be cited alongside for an interview.
