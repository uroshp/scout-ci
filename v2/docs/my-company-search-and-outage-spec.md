# Spec — `my_company` strengths search + outage discipline (propagation §17 follow-on)

Status: PROPOSED (2026-06-22), not built. Author: drafted with Uroš from the first authorship-judge
adjudication (decision-log §12). Resolves the propagation P0 (`[[project-living-battlecard-propagation]]`)
and the "outage catch-22" surfaced by that adjudication.

## 1. Problem (precisely, from the code)

`monitor.check()` assembles `act_facts = [competitor act-facts] + [my_company act-facts]`
(`monitor.py:589`) and hands them to `propagate(meta, act_facts, claims, ...)`. Propagation then runs
propose (Sonnet) → floor → judge (Opus). For a **back-foot** fact (a competitor's strong move, OR our
own stumble — outage, price hike, security incident), the proposer must author an **objection** whose
rebuttal **pivots to a genuine, currently-true strength** (`_PROPOSE_SYSTEM`: a rebuttal that only
restates the constraint is a FAIL). The judge then checks that pivot **only against `facts`** —
`_JUDGE_SYSTEM`: "the GROUNDED FACTS (already verified TRUE — the ONLY admissible evidence)."

The gap: the only my_company facts in the pool are **recent my_company developments** (what
`_my_company_facts` grounds — front-foot *play* material). A back-foot rebuttal needs a **standing
strength** to pivot to (e.g. "Claude is GA on Vertex AI + Bedrock with enterprise SLAs", "99.9% 90-day
uptime"). A standing strength is rarely "recent news", so it is **not in `act_facts`**. The proposer is
allowed to pivot to an *existing card claim* (`_PROPOSE_SYSTEM`), but the judge does **not** treat card
claims as admissible evidence — so the pivot reads as an invented/ungrounded capability and is rejected
(FACTS-ONLY / HOLLOW-REBUTTAL).

**The catch-22:** outage (and other back-foot) objections can't win. Pivot → "ungrounded capability"
reject. Don't pivot → "hollow rebuttal" reject. **Canonical evidence: adjudication #5
(`a_229fc150e195`)** — the Vertex/Bedrock-SLA pivot the judge killed is *true and already on the live
card*; the judge just couldn't see it because the standing strength wasn't a grounded fact in the pool.
Across the 28 captured decisions, the genuinely status/outage-driven ones (~7–8) skew heavily reject for
this structural reason.

## 2. Root cause (one sentence)

Propagation's admissible-evidence pool carries my_company *news* but never my_company *standing
strengths*, so a back-foot rebuttal has nothing grounded to pivot to.

## 3. Design

Two coupled components + one prompt clarification. All reuse existing machinery; the net new surface is
small.

### Component A — `my_company` strengths search (the linchpin)

**Trigger:** runs inside propagation (or just before it) **only when `act_facts` contains ≥1 back-foot
fact** (`valence == "back_foot"`). No back-foot trigger → does not run → $0.

**What it grounds:** for each back-foot trigger, derive the *strength dimension* the rebuttal will need
from the trigger's topic, and ground the my_company standing strength on that dimension into a **fact**
(`claim_type: "fact"`, `about: "my_company"`, `valence: "front_foot"`, real `source_url` +
`evidence_excerpt`). Dimension mapping (seed; extend as needed):
- outage / downtime / availability → my_company multi-cloud availability, deployment surfaces, SLA,
  uptime track record.
- price hike / cost → my_company pricing/value, plan structure.
- security incident → my_company security posture, certifications (SOC 2, FedRAMP, ISO).
- model pulled / restricted → my_company GA model availability to standardize on.

**How it grounds:** reuse the existing grounding discipline — the same ground+retry the competitor arm
and `_my_company_facts` already use (web search → single verbatim excerpt → `claim_type: fact`). This
is NOT a new grounding mechanism; it is the same one, seeded by "the strength relevant to THIS
objection" instead of "recent news."

**RECOMMENDED variant — cached standing strengths, not per-fire search.** Rather than search fresh on
every back-foot fire (cost + latency + nondeterminism), maintain a small **per-card cache of grounded
standing-strength facts** (`strengths/<slug>.json` in the private store), one per dimension, refreshed
on a slow cadence (e.g. weekly, or when stale > N days). At propagation time we **select** the cached
strength fact(s) matching the trigger dimension and add them to `act_facts` — a lookup, not a search.
The search only runs to (re)build the cache. This bounds cost to ~1 refresh/card/week regardless of how
often objections fire. (Alternative: per-fire search — simpler, no cache, but pays a search every
back-foot escalation. Pick cached unless freshness on a specific dimension matters more than cost.)

**Output:** the selected/grounded strength fact(s) are appended to `act_facts` before `propagate()`.
The proposer's op still `derived_from` = the **trigger** (the outage); the **pivot** cites the strength
fact, now admissible. `floor_check` is unchanged (derived_from still resolves to a surviving fact — the
trigger).

### Component B — propose + judge prompt clarification

Today both prompts read as if a single fact must license the whole op. Make explicit, in
`_PROPOSE_SYSTEM` and `_JUDGE_SYSTEM`, that **for a back-foot objection the pivot strength may be
grounded by ANY fact in the admissible set** (typically a `my_company` strength fact), distinct from the
`derived_from` trigger. `derived_from` anchors WHAT raises the objection; the rebuttal may cite a sibling
grounded fact for the pivot. The judge still rejects a pivot grounded by *no* admissible fact (invented
capability) and a hollow rebuttal — those rules are unchanged. This is the change that lets a
correctly-grounded pivot (like #5's) pass.

### Component C — outages are NOT special; reuse the existing materiality gate (simplified 2026-06-22)

REVISED per Uroš: do NOT build outage-specific machinery (no typed gate, no incident ledger, no
pattern computation). The question for an outage is the *same* question already asked of every fact:
**does this change what a rep says/does in a live deal NOW?** That gate already exists —
`_MATERIALITY_SYSTEM` (monitor.py:169/359) classifies each fact `act` vs `watch`, and **watch-grade
never reaches propagation** (monitor.py:588, `act_facts` at :589). An outage just rides it.

- **The only change is one line of calibration** in the materiality prompt(s): a routine or
  partial/single-region cloud-provider outage is **`watch`** (recorded for situational awareness in the
  feed, never propagated); only a **broad/sustained/material** outage is `act`. The data showed the
  GCP-India *regional* incident over-escalated to `act` (propagated, judge rejected hollow) — this
  demotion prevents that wasted cycle. (The prompt already lists "our outage" as an `act` example;
  this just adds the not-every-outage qualifier.)
- **Recurrence/pattern is NOT tracked by us.** A deal-relevant series of outages becomes major news →
  groundable from a source next cycle. A series that never makes news isn't deal-relevant → drop it.
  This is *more* facts-only-consistent than a self-maintained ledger: every claim stays anchored to an
  external source; we never assert a model/code-minted "Nth outage" count a buyer can't verify. Uroš's
  call: "these outages happen day to day and aren't deal-relevant; if it's a real series we pick it up
  as news next cycle."
- **Pivot-or-tracked-fact still holds, for free.** An `act`-grade outage for which Component A finds no
  grounded pivot produces no objection (the existing HOLLOW-REBUTTAL judge rule kills it); it stays a
  `recent_moves` entry. No new mechanism — A + the existing judge rule already guarantee this.

## 4. Reuse vs build (keep it cheap)

REUSE (already exists): triage dual-scope detection; `_my_company_facts` grounding pattern;
propose/floor/judge/apply; the `act_facts` plumbing into `propagate()`; `about`/`valence`/`claim_type`
fields; `_is_act`.

BUILD (the actual new surface):
1. strengths grounding + dimension mapping + the per-card strengths cache (Component A).
2. one calibration line in the materiality prompt(s): routine/partial-region cloud outages are `watch`,
   only broad/sustained ones are `act` (Component C — no new mechanism).
3. two prompt clarifications (Component B) — small.
4. unit tests: pivot-grounded-by-sibling-fact passes the judge; un-pivotable outage degrades to a
   recent-move (C); a regional/routine outage classifies `watch` (never propagates).

## 5. Cost

The strengths search/refresh fires only on back-foot escalations (rare; most checks are quiet or
front-foot). Cached variant: ~1 grounded refresh per card per week ≈ cents/card/week; per-fire variant:
~1–2 search calls per back-foot escalation. Per-run propagation cost is otherwise unchanged. **Estimate
+ confirm the exact per-run number before building** (`[[feedback-estimate-spend-before-multiagent]]`,
`[[feedback-guard-api-spend]]`).

## 6. Validation & rollout

BUILT + VERIFIED 2026-06-22 (v1 = deterministic re-ground; cold search NOT built). `scout/strengths.py`
+ propagate floor exclusion (`_trigger_fact_ids`) + both prompt clarifications + monitor wiring + the
Component C calibration + the fail-closed fix. 102 unit tests green.

LIVE PROOF on the real Opus judge — the SAME #5-style revise (pivots to "Vertex/Bedrock SLAs today"),
judged with the multicloud capability NOT on the card as a play:
- WITHOUT a strength fact in the pool → **reject**: "the rebuttal pivot is INVENTED ... a capability NO
  admissible fact supports ... Pivot rests on no admissible fact." (reproduces #5 exactly)
- WITH the standing-strength fact in the pool → **confirm**: "pivot ... is grounded in standing-strength
  fact c_85d... invents no capability."
A clean reject→confirm flip driven solely by the strength fact: **Component B is proven.**

TWO findings that bound v1's value (be honest about them):
1. The Opus judge ALSO credits an active where_we_win CARD PLAY as pivot grounding (not strictly
   facts-only): when the multicloud capability was present as a play, the judge confirmed the pivot with
   or without the strength fact. So v1 (re-ground existing plays into facts) HARDENS play-backed pivots
   and makes the grounding explicit, but for those it is partly belt-and-suspenders.
2. v1 does NOT fix the real #5. The live anthropic card has 4 grounded where_we_win plays, NONE about
   availability/multi-cloud/SLA — that capability lives only inside objection prose, never as a play. So
   `build_from_claims` yields 0 strengths covering the #5 pivot. **Closing the actual #5-class gap
   requires the COLD-SEARCH enrichment** (ground "Claude on Bedrock/Vertex with SLAs" as a fact even when
   it is not a card play). That is the necessary next build, gated on a back-foot trigger + a cost
   estimate. v1 is the proven plumbing the cold search feeds.

Rollout: shadow-first behind `PROPAGATE_MODE` (unchanged); the changes are backward-compatible (strengths
only enter when propagation is enabled and act_facts is non-empty). Promote the authorship judge on data
after the cold search closes the availability blind spot the one over-strict adjudication (#5) exposed.

## 7. Folded-in bug (decision-log §12)

The 5 fail-closed "rejects" (judge returned no verdict → defaulted to reject) pollute the adjudication
gate and dropped a material positive update (Trump national-security reversal). Fix alongside: exclude
fail-closes from `adjudicate._JUDGE_VERDICTS` (route to a re-run, not the human queue) and run them
through the no-drop hold path (`scout/reformat.py`). Small, independent of A–C; do it in the same PR.

## 8. Open questions

- Strengths cache location/cadence: `strengths/<slug>.json` refreshed weekly vs on-staleness vs per-fire.
- Should the strengths cache seed from the card's *existing* where_we_win claims (re-grounding them as
  facts) rather than a cold search? Cheaper, and it directly grounds the pivots already on cards.
  (Uroš leaning yes — recommended in §3 A.)
