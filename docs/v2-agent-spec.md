# Scout v2 — Agent Spec (the Living Battlecard)

**Status:** direction doc, written before building. Claude Code builds against this.
**Date:** June 2026.

**One line:** Scout is a self-verifying living battlecard — it generates a sourced competitive brief, monitors for material changes, and updates only when something changes the GTM story.

-----

## 1. Why this is an agent (not a button)

v1 is a fixed pipeline: generate → verify → render, the same two calls in the same order every time. It produces a document. A document is a snapshot — true the day it’s made, stale the week after.

v2 is an agent because it has a *recurring job that requires judgment on every cycle*: watch a set of competitors, decide what has genuinely changed, decide whether each change is **material** (does it move a battlecard zone, a price, a positioning claim, or introduce a risk?), and surface only the signal — with a “so what” — while staying silent when nothing matters.

> Re-running a report on a schedule is a cron job; judging materiality and maintaining a living document is an agent.

The agentic loop, stated plainly: **monitor → detect change → assess materiality → decide signal vs. noise → update the battlecard + alert with a so-what.** The cuts and the silence are as much the product as the alerts.

-----

## 2. Architecture: two runtimes, one git-committed store

```mermaid
flowchart TD
    subgraph INT["Interactive runtime — Streamlit Cloud (reads the store, never writes it)"]
        UI["User: generate on demand (download) · view living battlecards · read alert log"]
    end
    subgraph SCHED["Scheduled / headless runtime — GitHub Actions"]
        TRACK["Generate + commit a tracked baseline (CLI now; workflow_dispatch later)"]
        MON["Monitoring agent (cron, per tracked competitor)"]
        SIDE["Side-effects in code: git commit + email"]
    end
    STORE["Git-committed store<br/>battlecards/&lt;slug&gt;/ claims.json · meta.json · current.md · alerts"]
    TRACK --> STORE
    MON --> STORE
    MON --> SIDE --> MAIL["Email digest"]
    STORE -->|repo updates -> app redeploys| UI
    UI -.->|download only, NOT tracked| DL["User's local copy"]
```

**Write path is unambiguous: only the scheduled/headless runtime writes the store.**

- **Interactive runtime (Streamlit Cloud)** — generates a brief on demand and offers it for **download**, and **reads** the store to display living battlecards and the alert log. It does **not** commit. (Streamlit Cloud can’t reliably write back to git.) **Public demo generation is not tracked unless its folder is committed.** **Runtime split (decided):** the Agent SDK drives the Claude Code CLI under the hood (Node + subprocess), which Streamlit Community Cloud may not host. So in-app generation stays on the **v1 Messages-API path** (server-side web search, the proven `_extract` citation handling); the **Agent SDK is used only by the headless worker.** Step 1 of the build (§15) verifies the SDK on the headless runtime before any port. Same generation *logic and quality bar* on both; different transport.
- **Scheduled / headless runtime (GitHub Actions)** — owns persistent tracking. A baseline becomes “tracked” when its folder is committed (via CLI now; a `workflow_dispatch` trigger from the app is a later option). The cron monitor then maintains it, commits updates, and sends email.
- **Shared store** — files committed in the repo. The git history *is* the living battlecard’s heartbeat — a visible record of the document evolving (good for the portfolio story; see commit policy in §9).

**Why this split (interview answer):** monitoring must run with no user present, which Streamlit Cloud cannot do. Separating the interactive face (reads) from the headless worker (writes), with git as shared state, is zero-new-infra and keeps the write path single and clear.

-----

## 3. Models — verified, but kept as config

Defaults verified against Anthropic docs (June 2026), but **never hardcoded** — set in `.env`/config so the spec survives renames:

```
ORCHESTRATOR_MODEL=claude-opus-4-8     # judgment: planning, materiality, synthesis, consistency
SUBAGENT_MODEL=claude-sonnet-4-6       # legwork: research + verification, run in parallel
FAST_MODEL=claude-sonnet-4-6           # cheap triage gate (could be a haiku-class model)
```

|Role                                          |Default                  |Why                                                       |
|----------------------------------------------|-------------------------|----------------------------------------------------------|
|Orchestrator                                  |Opus 4.8 ($5/$25, 1M ctx)|The reasoning that decides what’s signal.                 |
|Subagents (researcher, verifier, change-scout)|Sonnet 4.6 ($3/$15)      |Fast/cheap enough to parallelize the legwork.             |
|Triage gate                                   |Sonnet / Haiku           |The cheap “is there anything here at all?” check (see §6).|

Confirm the exact SDK API for per-agent model assignment during build; read IDs from config.

-----

## 4. SDK and the agentic loop

Built on the **Claude Agent SDK** (`pip install claude-agent-sdk`, Python 3.10+) — the same harness that powers Claude Code, so this *is* the “built with Claude Code” story. The SDK runs the loop autonomously (vs. v1’s hand-written tool loop). We define agents, tools, and guards; the orchestrator decides what to call, in what order, and when it’s done.

Primitives: `query()` with an orchestrator prompt + `agents={...}` (`AgentDefinition`: description, prompt, tools, model). Built-in tools — **`WebSearch`** (reused capability), **`WebFetch`** (the new capability: read sources, not just search), **`Agent`** (invoke subagents). **Hooks** (`PreToolUse`, `Stop`) for guards.

> **Control-vs-autonomy line (the core principle, carried from v1):** the agent owns *judgment* — what to research, how deep, what’s material, what to write. Deterministic **side-effects stay in code**: git commits and email are NOT agent tools; the agent emits a structured result and plain code performs the commit and sends the mail. Output cleanup (`clean_output`/`format_report`) stays a deterministic post-step. A confused agent therefore cannot spam alerts, double-send, or corrupt the repo.

-----

## 5. The generation agent (brief -> tracked battlecard)

Input: competitor (required), your company (optional), focus (optional).

**Orchestrator (Opus 4.8)** plans the brief, delegates, synthesizes, runs the consistency sweep, emits the brief + cut log. Subagents (Sonnet 4.6):

- **researcher** — tools `WebSearch`, `WebFetch`. Gathers material per section; parallelizable.
- **verifier** — tools `WebSearch`, `WebFetch`. Re-checks each claim by *reading* the source (v1 verify discipline, now able to fetch and read). Applies the source hierarchy, judges *support*, cuts the unverifiable, emits structured claim objects (see `claim-object.md`) and cut-log entries. **Support is the model's call; provenance is code's** — after the verifier emits each claim, a deterministic **grounding check** (re-fetch `source_url`, confirm `evidence_excerpt` is a real substring of the page; fail → cut, recorded with reason) runs as a post-step. The two layers are never merged — see `claim-object.md` §4.

Reused wholesale from v1: `methodology.md`, source hierarchy, cut-log spec, formatting rules, “so what” requirement, consistency sweep, deterministic output cleanup.

Runs in the Streamlit app (on demand, download, **not tracked**) and via CLI/Action (to create a **tracked** baseline that gets committed). Same generation logic, different runtime.

-----

## 6. The monitoring agent — staged, so Opus doesn’t wake for nothing

Runs on the GitHub Actions schedule, once per tracked competitor. **Staged to control cost:** cheap retrieval and a cheap triage gate run first; the expensive judgment runs only on candidates.

1. **Load baseline** — `claims.json` + `meta.json` (baseline date, `last_checked`, alerted fingerprints).
1. **Retrieve (cheap)** — date-scoped `WebSearch` since `last_checked`, `WebFetch` to read.
1. **Triage gate (FAST_MODEL)** — “Is there anything here that *could* be material?” If no -> stop, log a no-change run (no Opus, no commit; see §9).
1. **Judge materiality (ORCHESTRATOR / Opus 4.8)** — only on candidates that pass triage. Apply the materiality threshold (§8) and verify with the same source-tiering discipline as generation. **The materiality judgment itself always runs on Opus — triage is allowed to be cheap, the decision to alert is not.**
1. **Update + record** — for each material change: update the affected claims in `claims.json` (and re-render `current.md`), append to `alerts.md` + `alerts.jsonl` with a dedup fingerprint, add the fingerprint to `meta.json`.
1. **Side-effects in code** — commit changed files; send ONE email digest of material deltas (each with its “so what”). Nothing material -> no email.

**Dedup:** `meta.json` holds fingerprints (hash of headline/URL/claim) of everything already alerted, so the same event isn’t re-alerted nightly.

-----

## 7. State / file layout (Markdown is presentation; JSON is state)

The monitor must not diff prose. Claims, sources, and fingerprints live in structured files; Markdown is rendered from them for display.

```
battlecards/<slug>/
  current.md       # rendered battlecard for display + an "as of <date>" heartbeat line
  claims.json      # the structured claim objects (state of record) — sources live inside each claim
  meta.json        # my_company, competitor, focus, baseline date, last_checked, alerted fingerprints
  alerts.md        # human-readable append-only alert log (date + so-what)
  alerts.jsonl     # machine-readable alert records, one JSON object per line
reports/           # v1 timestamped samples (showcase) — unchanged
```

Sources are folded **into** each claim, and the battlecard is **derived** from claims — so there’s no separate `sources.json`/`battlecard.json` to keep in sync. Three state files (`claims`, `meta`, `alerts.jsonl`) do what five would.

**Claim object (the verification contract — makes “verified” concrete and auditable):** the full schema, ID scheme, and grounding contract live in **[`claim-object.md`](claim-object.md)** (the first concrete v2 artifact). Shape, abbreviated:

```json
{
  "id": "c_a1b2c3d4e5f6",
  "subject_key": "aws | fy-revenue | 2025",
  "claim": "AWS full-year 2025 revenue was $128.7B.",
  "claim_type": "fact | interpretation | sentiment",
  "section": "executive_summary | snapshot | recent_moves | positioning | pricing | battlecard | sentiment | objection_handling",
  "zone": "where_we_win | contested | where_they_win | null",
  "order": 2,
  "source_url": "https://...",
  "source_tier": "primary | reputable_secondary | sentiment_only",
  "evidence_excerpt": "verbatim span copied from the fetched source",
  "as_of": "2026-02-06",
  "verified": true,
  "confidence": "high | medium | low",
  "grounding": { "checked": true, "match": true, "method": "substring", "fetched_at": "2026-06-03", "detail": null }
}
```

Rule, phrased precisely (not as an absolute): **every factual claim must be backed by fetched source evidence or it is cut.** `source_tier` maps to the v1 hierarchy — `primary` = Tier 1A (filings/transcripts/contracts), `reputable_secondary` = Tier 2/2E (news / analyst-estimate, labeled), `sentiment_only` = Tier 3S/4. A `fact` claim may not rest on `sentiment_only`. This makes the verifier’s job mechanical enough to audit. Three decisions baked into the schema:

- **Identity is the subject, not the text.** `id` is a deterministic hash of `subject_key` (`entity | attribute | qualifier`, value-independent). When a figure changes or a CEO is replaced, the new claim hashes to the **same `id`**, so the monitor updates in place instead of duplicating. The value lives only in `claim`.
- **Identity excludes presentation.** `section` / `zone` / `order` are mutable display attributes — moving a claim between sections must not change its `id`. They also let `current.md` be rendered by deterministic code (each claim knows its section, zone, and sort order) rather than an LLM pass.
- **Grounding is provenance, not support.** A deterministic check confirms the `evidence_excerpt` is really on the fetched page; whether it *supports* the claim is the verifier's separate, semantic job.

**Slug must encode perspective**, because a battlecard is not “Google Cloud” — it’s “AWS vs Google Cloud in cloud infrastructure”:

```
<my-company>__vs__<competitor>__<focus>     e.g. aws__vs__google-cloud__cloud-infra
```

If `my_company` is absent, use `scout__vs__<competitor>__<focus>`. A short hash may be appended for uniqueness. Full fields are also stored in `meta.json`.

-----

## 8. Materiality threshold (the agentic heart — where the effort goes)

This is the judgment that separates an agent from a cron job. Most of it already lives in `methodology.md` (signal vs. noise); v2 adds an explicit threshold for *change*.

**Material (alert + update):** funding, M&A, exec hires/departures, pricing/packaging changes, major launches, security incidents/breaches, legal actions, partnership shifts, public strategy statements that change a battlecard zone or an objection. (The Mercor breach in the v1 sample is the canonical example — a battlecard-altering event.)

**Noise (ignore):** routine blog posts, minor features, conference talks with no strategic shift, restatements of known facts, sentiment churn.

Every alert must carry a **so what** — the decision it should change — or it doesn’t ship. (Designing for signal, not notification volume: most alerting tools over-notify; this one is built to stay quiet.)

-----

## 9. Commit policy (preserve the living heartbeat without repo clutter)

- **Material change ->** commit the changed `claims.json` / `current.md` / `alerts.*` / `meta.json`. These are the signal commits.
- **No-change run ->** do **not** commit state; record the run in GitHub Action logs/artifacts. Keeps the log free of “checked, nothing” noise.
- **Weekly heartbeat ->** a deliberate, visible commit that updates the `as of <date> — no material change` line in `current.md`, so the document itself shows it’s alive and current even in quiet weeks. The heartbeat is part of the demo — don’t optimize it away to keep the log tidy; just keep it weekly, not daily.

-----

## 10. Guards & safety (an agent that can loop can burn money)

- **Max tool calls / iterations per run** — `PreToolUse` hook that counts and halts, and/or the SDK max-turns option (confirm name in docs).
- **Cost ceiling per run** — track token usage; abort if exceeded.
- **Staged models (§6)** — the cheapest guard of all: don’t run Opus on empty days.
- **GitHub Actions timeout** — hard wall-clock cap.
- **Anthropic account hard spend cap** — already set; ultimate backstop.
- **Side-effects gated in code** — agent can’t commit or email directly.
- **Auth** — Agent SDK runs on an API key (not claude.ai login), billed as API usage. From June 15, 2026, Agent SDK usage on subscription plans draws from a separate monthly Agent SDK credit — confirm the billing path before scheduling frequent runs.

-----

## 11. Reused vs. new

**Reused (~75–80%):** `methodology.md` (extended with the materiality threshold), source hierarchy, cut-log spec, formatting rules, “so what” discipline, consistency sweep, deterministic output cleanup, the Streamlit interface (extended to show living battlecards + alert log).

**New:** the Agent SDK orchestrator + subagent definitions (headless worker only — in-app generation stays on the v1 Messages-API path); structured claim objects (`claim-object.md`); `WebFetch` source reading + the deterministic **grounding check**; the monitoring agent + staged models + materiality layer; the GitHub Actions cron worker; the git-committed structured store + dedup; the email digest; guards/hooks. The rewrite is specifically the **orchestration core** — pipeline I control -> loop the model drives. Git history shows the evolution.

-----

## 12. Interviewer readout (what this is built to demonstrate)

- **PMM workflow understanding:** battlecards, objection handling, sales enablement, signal-over-volume.
- **Agentic design:** recurring monitoring, judgment, materiality, deliberate silence.
- **Technical judgment:** split runtimes, git-backed structured state, perspective-aware identity, staged model usage, guarded side-effects.
- **AI safety / practicality:** fetched-evidence verification, structured claim contract, cut logs, source hierarchy, cost controls.

-----

## 13. What “good” looks like

Generation (from v1): verdicts first, a “so what” on every exec point, every claim a structured object backed by fetched evidence or cut, a cut log, no internal contradictions.

Monitoring (new): only *material* deltas surface; every alert carries a “so what”; nothing re-alerts; the agent stays silent when nothing’s material; the baseline stays internally consistent across updates; the document visibly breathes (weekly heartbeat).

Demo bar: a deliberately stale baseline -> scheduled run -> agent finds real-world changes, judges them material on Opus, updates the affected claims/zones, and emails a digest with so-whats. Live, honest, no faking.

-----

## 14. Out of scope for v2

In-app tracking configuration (tracking = committed files); more than one alert channel (email only); hosted database (git files are the store); auth / multi-user; real-time event-push (scheduled polling is enough); anything that turns the materiality judgment into a side quest — that judgment IS the build; the plumbing stays minimal.

-----

## 15. Build order (for the Claude Code session)

1. Scaffold the Agent SDK **on the headless runtime**: one orchestrator + one subagent; confirm the loop runs and `WebSearch`/`WebFetch` work *there* (not just locally). Read model IDs from config. Decide here whether the SDK can also run on Streamlit Cloud; default assumption is no, so in-app generation stays on the v1 Messages-API path (§2).
1. Port generation: orchestrator + researcher + verifier producing the v1 quality bar, emitting **structured claim objects** (per `claim-object.md`) + cut log.
1. Wire the structured store (`battlecards/<slug>/...` with perspective slug) + the deterministic **grounding-check** post-step (`claim-object.md` §4) + output cleanup.
1. Monitoring loop against a stale baseline: retrieve -> **triage gate (cheap)** -> **materiality (Opus)** -> update claims + alerts + dedup.
1. Side-effects in code: git commit (per the §9 policy) + email digest.
1. Guards: hooks for tool-call cap + cost ceiling.
1. GitHub Actions cron workflow; secrets (API key, email creds). **FIRST action of this step (TRACKED RISK, do before trusting any monitoring run): re-run the reachability probe (`scout/_probe_reach.py`) from *inside* an actual GitHub Action to measure REAL production grounding reachability.** Build-step-5 probing showed ~67% reachability from Codespaces, but several sources block by **datacenter IP** (confirmed: SEC.gov 403s regardless of User-Agent, while the same content on a company IR host fetched fine). GitHub Actions is also a cloud IP, so production reachability may be materially worse — and monitoring is worthless if the worker can't ground. Measure it first; if it's bad, the fix is broader substitution / a fetch path with residential or proxied egress, decided from that data.
1. Extend Streamlit to show living battlecards + alert log (read-only).
1. Demo dry-run: stale baseline -> scheduled run -> real deltas + email.

-----

## 16. Decisions to resolve during build

- ~~Exact SDK API for per-agent model assignment and max-turns.~~ **Resolved (build step 1, `claude-agent-sdk` 0.2.88):** per-agent model via `AgentDefinition(model=...)`; orchestrator model via `ClaudeAgentOptions(model=...)`. Iteration cap via `ClaudeAgentOptions(max_turns=...)` (and per-agent `AgentDefinition(maxTurns=...)`). Cost ceiling is **native** — `ClaudeAgentOptions(max_budget_usd=...)` — so §10's per-run cost guard needs no hand-rolled token counter. Subagents registered via `agents={name: AgentDefinition}`; the orchestrator invokes them with the **`Agent`** tool (not `Task` in this version). Headless runs use `permission_mode="bypassPermissions"`. Verified live: orchestrator (Opus) delegated to a Sonnet researcher that ran `WebSearch` + `WebFetch` and returned a sourced answer.
- Email mechanism (transactional API vs. SMTP) — pick one pipe.
- Monitor cadence (start daily; tune).
- Triage-gate model (Sonnet vs. Haiku) — measure cost/accuracy before locking.