# Scout v2 — Launch Decision Log

Decisions made wrapping the v2 build for launch (June 2026). Newest context at top of each
section. This is the "why", not the "how" — code and commit history carry the implementation.

---

## 1. Deployment topology — async self-serve, NOT Cloud Run

**Decision:** Self-serve "create your own report" runs **async**: the deployed app captures a
request and the **same SDK generation pipeline runs out-of-band in a GitHub Action**, which
commits the result back. Chosen over a synchronous Google Cloud Run service.

**Why:** Async delivers the *same hero-fidelity* output (it's the identical pipeline, just
triggered out-of-band) with far less infra and risk — no Docker image bundling Node + the
`claude` CLI, no standing Cloud Run service, free Streamlit hosting — and a better UX ("we're
generating it, check back" beats a 5–8 minute in-browser spinner). Cloud Run was scoped at
~2 days with a real rabbit-hole risk (proving the CLI runs headless in-container); async reuses
the Action pattern already in the repo (`monitor.yml`). Cloud Run kept as a later upgrade if
instant in-browser generation ever justifies the standing infra.

## 2. Showcase battlecard set — 6 generated + the hero

**Decision:** Generated 6 cards under the validated prompts; the pre-existing Anthropic vs
OpenAI (enterprise coding) card is the hero. #1 (Cursor vs Cognition) gated on manual review
(0 hard failures) before batching the rest.

### Batch cost audit — 6 cards generated

| Card | Cost | Claims | Grounded | Audit |
|---|---|---|---|---|
| Google Cloud vs AWS | $9.12 | 38 | 38/38 (1 recovered on retry) | ✅ 0 fail |
| Cursor vs Cognition | $9.09 | 37 | 37/37 | ✅ 0 fail |
| Salesforce vs HubSpot | $8.95 | 25 | 25/25 | ✅ 0 fail |
| MS Teams vs Slack | $6.66 | 35 | 35/35 | ✅ 0 fail |
| Notion vs Atlassian | $6.63 | 39 | 39/39 (1 absent→recovered) | ✅ 0 fail |
| Batman vs Superman | $5.37 | 33 | 33/33 | ✅ 0 fail |
| **Total / mean** | **$45.81 / $7.64** | 207 | ~100% | **6/6 clean** |

**Variance: $5.37–$9.12, σ ≈ $1.5.** Every card passed the source audit with 0 hard failures,
~100% grounded.

**What drives the spread — it's search volume, not claim count.** The cost tracks how much the
researcher had to *search*, not how many claims it kept:
- Cursor: 134 web searches → $3.67 of the bill was search fees alone.
- Batman: 62 searches → $1.65. Fewer because the facts are finite (box office, release dates) vs
  a sprawling, fast-moving news landscape.
- Tell: Salesforce cost *more* ($8.95) with the *fewest* claims (25); Notion cost *less* ($6.63)
  with the *most* (39). Claim count is uncorrelated — **news density is the driver**.

**Batman (stress-test):** the pipeline refused to hallucinate a fictional matchup — it grounded
the *real* Batman/Superman film-franchise rivalry (box office, Man of Tomorrow 2026, RT
sentiment) on real entertainment-news sources. 0 hard failures. The sourcing discipline holds
even on an absurd input.

**Can they be cheaper? Decided NOT to.** The one lever is capping the researcher's web searches,
but that trades away research depth on the exact showcase cards whose quality is the credibility
pitch. Generation is a **one-time ~$46** for the whole set; the recurring cost was monitoring,
where we already won big. **Leave generation alone; bank the monitoring savings.** Revisit the
researcher cap only if regenerating frequently.

## 3. Monitoring cost — Haiku triage + capped searches + strict escalation gate

**Decision:** Cut per-check cost hard before launch:
- **A — Haiku triage:** the every-check gate runs on `claude-haiku-4-5-20251001` (was Sonnet).
- **B — capped searches:** triage gets its own tight caps — ≤5 searches (bounded by
  `TRIAGE_MAX_TURNS=8`) and a hard `$0.50` budget ceiling.
- **Strict gate:** triage tags each candidate `substantial: true/false`; the expensive Opus
  materiality judge runs **only** when ≥1 candidate is substantial. Quiet *or* minor-only windows
  exit triage-only.

**Measured result: $0.16 per quiet daily check** (Haiku, 0 escalation), down from $1–2.

**Tradeoff (accepted):** the strict gate trades a little recall/latency for the big saving — if
Haiku tags a genuinely material item "minor," it's caught on the next day's check, not same-day.
Fine for a daily-cadence battlecard; not for breaking-news same-day alerting.

## 4. Cadence & schedule — twice daily, window-anchored at 7am + 1pm US Eastern

**Decision (2026-06-05, supersedes the earlier once-daily plan):** For the launch window, check
**twice a day at fixed real-world times — 07:00 and 13:00 US Eastern (11:00 / 17:00 UTC, EDT)**,
emulating how a rep actually works: a morning brief when they wake up, a midday news re-check.
Pare back to once-daily after week one.

**Why this shape — the product promise is PREDICTABLE freshness, not just "twice a day."** A
battlecard that updates last-night-for-one, noon-for-another, whenever-for-a-third *looks broken*
even when the analysis is good. Two failures were producing exactly that drift, and both are now
fixed:

1. **The gate was relative, so it drifted.** `_is_due` checked `last_checked + cadence_hours`,
   which slides the next due-time later on *every* check — updates wander across the clock.
   Replaced with a **window-anchored gate** (`config.MONITOR_ANCHORS_UTC = "11:00,17:00"`): a card
   is due when it hasn't been checked since the most recent passed anchor. Freshness is pinned to
   the wall clock and cannot drift. (`cadence_hours` is now dormant — used only by the legacy
   relative fallback when anchors are disabled.)
2. **GitHub `schedule` fires late/unreliably** (observed: an 11:00 UTC fire landed 13:46, ~2h46
   late; can also be dropped). A single `0 11` / `0 17` would miss the slot. So the cron **bursts**
   every 30 min across each window (`*/30 11-13` and `*/30 17-19`). The first fire that lands after
   an anchor does the check; the anchor gate makes every later fire in the window a cheap no-op
   (a not-due card makes no API call). Net: one paid check per card per window, reliably on time.

**Cost:** back to ~2 paid checks/card/day for the window (~$12–23/day across the 6 cards at
~$1–1.9/check) — accepted as the launch-credibility spend; step down after week one by dropping an
anchor. Note this is higher than the §3/§6 "$0.16 quiet check" figure, which assumed once-daily.

**Not chasing 7:00:00 sharp:** GitHub cron can only promise "in the morning window," not the
minute. If literal on-the-minute timing ever matters, that's Cloud Scheduler — see the GCP
discussion (kept out of scope for launch).

**DST note:** when the US returns to EST (UTC-5) on **2026-11-01**, shift both crons +1h
(`*/30 12-14` / `*/30 18-20`) **and** set `MONITOR_ANCHORS_UTC` to `"12:00,18:00"`.

## 5. Monitored set & exclusions

- **6 cards monitored** (`monitored: true`, cadence 23): the hero + Cursor, Google Cloud,
  Salesforce, Notion, Teams.
- **Batman vs Superman: `monitored: false`** — it renders in the viewer as a stress-test
  showcase card but never burns a monitoring check.
- **Anthropic vs OpenAI (general): archived** → moved to `archive/` (out of `battlecards/`), so
  it's saved but excluded from both the public showcase *and* monitoring. Superseded by the
  enterprise-coding hero.

## 6. Spend caps — the full picture

| Cap | Scope | Value | Where |
|---|---|---|---|
| `TRIAGE_MAX_BUDGET_USD` | one triage check | $0.50 | `scout/config.py` |
| `MAX_BUDGET_USD` | one monitoring check (Opus escalation) | $3 | `scout/config.py` |
| `GEN_MAX_BUDGET_USD` | one generation | $10 | `scout/config.py` |
| self-serve ledger | **all self-serve combined** | **$100** | `selfserve/state.json` |

The self-serve ceiling is a **true hard cap**: the gate refuses to *start* a run that could cross
it (reserving one `GEN_MAX_BUDGET_USD` of headroom), so total self-serve spend can never exceed
$100.

**Global cap — must be set by the user.** There is **no code-level cap across monitoring +
self-serve combined**. The only true global ceiling is the **Anthropic Console monthly spend
limit** (Console → Settings → Limits/Billing). Set it to cover projected monitoring (~$15–35 over
the 10-day window with the cheap config) + the $100 self-serve ceiling + headroom. This is the one
place that guarantees no combined surprise.

## 7. Self-serve gating & privacy (Parts 2 + 3)

- **Launch window:** first **10** generations open (no password); after that the entry point locks
  to **"DM me for access."** Counter is **server-side / git-committed** (`selfserve/state.json`),
  shared across all visitors, checked before each run; the app shows "X free reports left."
- **Authoritative gate:** the GitHub Action (serialized via its `concurrency` group) re-checks the
  gate at the point of spend and is the sole writer of `state.json` — the app's gate is advisory
  display only, so a stale read can never overspend.
- **Privacy (Part 3) — REVISED 2026-06-05:** the original design kept user cards in
  `user_reports/<job_id>/` (not `battlecards/`) and called that "private by directory." The
  pre-launch security review caught the flaw: **this repo is PUBLIC**, so directory separation
  doesn't make anything private — every submission (the user's inputs *and* the generated card)
  would be world-readable on GitHub. **Fix: user data moved to a SEPARATE PRIVATE repo**
  (`SELFSERVE_REPO`, e.g. `uroshp/scout-user-data`); the code repo stays public for the portfolio.
  The app writes requests to / reads results from that private repo via the GitHub-API backend and
  triggers generation with a `workflow_dispatch` (a push to the data repo can't trigger a workflow
  in the code repo). The Action writes user data to the private repo via API and commits nothing to
  the public repo. Requests + cards stay reviewable by the owner, just not by the world. Token needs
  Contents:R/W on the data repo + Actions:R/W on the code repo.

## 8. Self-serve UX

Async with a **timed-estimate progress bar** (decoupled from the real job, per decision),
**"~6–8 minutes" stated up front**, and the **v1 rotating status messages** (ported verbatim).
The job id lives in the **URL (`?job=`)**, so a visitor can leave and come back — **no email/notify
system** (deliberately not built).

## 9. Deployment

**Streamlit Community Cloud**, subdomain **`agent-scout.streamlit.app`** (custom subdomains are
supported; a fully custom domain is not, on the free tier — acceptable for the showcase).

## 10. Launch prerequisites (user actions) & open risks

**Before go-live, the user must:**
1. Set GitHub repo secrets: `ANTHROPIC_API_KEY` (monitoring + self-serve generation),
   `RESEND_API_KEY` + `SCOUT_ALERT_TO` (email digests; optional — dry without them).
2. Set the app's host (Streamlit) secrets: `SELFSERVE_GH_TOKEN` (fine-grained PAT, this repo,
   contents:write), `SELFSERVE_REPO` (e.g. `uroshp/ci-agent`), `SELFSERVE_BRANCH` (`main`),
   `ANTHROPIC_API_KEY` not needed app-side (generation runs in the Action).
3. Set the **Anthropic Console monthly spend limit** (the global cap — see §6).
4. Deploy `app_v2.py` on Streamlit Community Cloud and claim `agent-scout.streamlit.app`.
5. **Merge `v2-agent` → `main`** — this is the launch flip: `schedule`/`push` workflows only fire
   from the default branch, so nothing spends until merged.

**Open risks (named, not blocking):**
- **The SDK-in-Actions path has never run live** (the cron has been inert on `v2-agent`). The
  first scheduled monitor run and first self-serve request are the real validation; watch them.
  The SDK uses its bundled CLI executable, so `pip install` should suffice (no Node step).
- **Materiality findings are high-variance** run-to-run — weigh monitoring alerts accordingly.
- **No per-user auth on self-serve:** the "10 free" is a global pool on a public URL. The $100
  hard spend ceiling is the real backstop against abuse.
