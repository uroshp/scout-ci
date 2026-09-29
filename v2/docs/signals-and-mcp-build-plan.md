# WS3 signals + WS4 MCP: build plan (2026-09-29)

Prepared after the Ask Scout promotion landed (main `98365b2`). Both workstreams are engine- and
monitor-side; neither touches the Ask panel. Source of the design: `~/.claude/plans/…biscuit.md`
(the plan Uroš approved 2026-09-28), narrowed here to the files and functions in the code as it is.

## What already exists (do not rebuild)

- **Structured-source clients**: `scout/sources/edgar.py` (`filings(cik, forms, since)`,
  `company_name`, XBRL facts), `scout/sources/jobs.py` (`postings(host, token)`, `diff(prev, cur)`,
  `summarize`), Wayback, the registry of job boards (`scout/sources/registry.py`). All pure, all
  tested (`tests/test_sources.py`), SEC UA (`config.SEC_CONTACT`) works from GitHub runners.
- **Monitor run controls**: `SCOUT_MONITOR_SLUGS`, `SCOUT_MONITOR_FORCE`, `SCOUT_MONITOR_TRACE`;
  `monitor.yml` inputs `dry_run / slugs / force / sources_tools`; `_run_all_impl(slugs=, force=)`.
  A signal dispatch is a `workflow_dispatch` with `slugs=<one card>` and `force=true`.
- **Dispatch pattern**: `selfserve.dispatch_generation()` POSTs a `workflow_dispatch` to the code
  repo with the store token; reuse its shape for the monitor.
- **Triage hook**: `monitor.check()` builds the triage prompt with `hunt_block` (the superseded-
  subject replacement hunt, `monitor.py:263`) appended to `_tracked_digest(claims)`; a second block,
  `SIGNALS TO INVESTIGATE FIRST`, goes in the same place.
- **Fingerprint dedupe**: `meta.alerted_fingerprints` + `_apply_updates` (`monitor.py:432`).
- **Due gate**: `_is_due(meta)` (`monitor.py:985`) keys on `last_checked` vs the anchors; a
  signal-triggered check writes `last_checked`, so the 4 AM anchor sees the card as checked
  (no second paid run).
- **Engine**: FastAPI + uvicorn (`engine/app.py`), `mcp==1.27.2` in the image with `FastMCP`
  importable; `ask.quick_ask` / `ask.ask` callable in-process; `ledger.Ledger` for spend.
- **Mini**: launchd jobs (`com.urosh.scout-replay`, `com.urosh.scout-sync`), `~/scout-replay/env`
  with the store token, the always-on pattern for $0 pollers.

## WS3: event-driven monitoring (signals)

Goal: a new SEC filing triggers that card's check within the hour, with the filing put in front
of triage, and "Triggered by: new 8-K" on the alert. Pages are NOT triggers (nonces and dates flip
page digests). **Hiring is not a trigger** (Uroš, 2026-09-29: "hiring is a footnote; ten roles is
nothing; I don't want to over-index on it; it is one pillar that feeds strategy and should be
interpreted through strategy"): job-board deltas are CONTEXT handed to materiality and the router
(a one-line "hiring: +N net, departments: …" block), never a dispatch. The one hiring event that
is a signal is a **brand-new department** (a department name not seen on that board in the prior
90 days, with at least 3 open roles): it is written to the card's open signals and put in front of
the NEXT scheduled run's triage, still not a dispatch. Caps: 2 triggered runs a day across all
cards (filings only), fingerprint dedupe, a triggered run counts as the day's check, and a
triggered run never emails on its own: what it finds lands in the next morning's FYI.

### Files

- `scout/signals.py` (new, pure + store I/O; no model):
  - `watch_for(slug, meta) -> {edgar_cik?, job_boards: [{host, token}]}` from `meta.watch`.
  - `poll_edgar(cik, state) -> [Signal]`: `edgar.filings(cik, forms=("8-K","10-Q","10-K","D"),
    since=state.last_accession)`; a Signal per new accession: `{kind: "filing", form, accession,
    filed, source_url, source_class: "filing", summary, fingerprint: sha(cik|accession)}`.
  - `poll_jobs(host, token, state) -> (context: dict, signals: [Signal])`: `jobs.postings` then
    `jobs.diff(state.rows, cur)`; `context` = `{net_new, removed, by_department}` for the prompts
    (always); a Signal ONLY for a brand-new department (`kind: "new_department", department, roles,
    source_url: board_url, source_class: "job_posting", fingerprint: sha(host|token|department)`),
    which is queued for the next scheduled run, never dispatched.
  - `state_path(slug) = f"signals/{slug}/state.json"`, `events_path = f"signals/{slug}/events.jsonl"`;
    `load_state`, `save_state` (via `selfserve.update_data`, optimistic), `append_events`.
  - `open_signals(slug) -> [Signal]` (events with `consumed_by` empty), `consume(slug, run_ts)`.
  - `dispatch(slug, reason) -> bool` (filings only): POST `workflow_dispatch` to `monitor.yml` with
    inputs `{slugs: slug, force: "true", reason}`; refuses when `dispatches_today >=
    SIGNAL_MAX_DISPATCHES_PER_DAY` (state in `signals/_dispatch.json`) or when the card was checked
    in the last 6 h. A dispatched run sends no email of its own (the live-mode FYI is per scheduled
    run; a triggered run's results ride the next FYI).
- `scripts/poll_signals.py` (new; runs on the mini hourly, $0): for every card with `meta.watch`,
  poll, append events, dispatch at most twice a day; prints a one-line summary per card; never emails.
- `scripts/set_watch.py` (new): `--slug --edgar-cik --job-board host:token` writes `meta.watch`
  (commit + push by the caller); seeds: OpenAI (Ashby `openai`), Anthropic (Greenhouse `anthropic`),
  Notion / Perplexity / Cursor / Cognition on Ashby (from the roster probe), Salesforce (archived card,
  EDGAR CIK 1108524) for the filing path.
- `scout/monitor.py`:
  - `check(slug, …)`: read `signals.open_signals(slug)`; render `SIGNALS TO INVESTIGATE FIRST`
    (kind, form/delta, filed date, source URL, class) after `hunt_block`; pass the filing/hiring
    delta into materiality as tier-tagged evidence (same block, materiality prompt); after a
    successful write, `signals.consume(slug, run_ts)` and stamp `signal_fingerprint` + `triggered_by`
    on the alert rows the run produced from those signals.
  - `__main__`: reads `SCOUT_MONITOR_REASON` (hint only; the store is truth).
- `.github/workflows/monitor.yml`: input `reason` (string, optional) → env `SCOUT_MONITOR_REASON`.
- `scout/page.py` / `server.py`: the alert row shows a "Triggered by: new 8-K / hiring +14" chip
  when `triggered_by` is set (rail alerts + the card's updates panel).
- `scout/config.py`: `SIGNALS_ENABLED` (flag, default off on main), `SIGNAL_NEW_DEPT_MIN_ROLES=3`,
  `SIGNAL_NEW_DEPT_LOOKBACK_DAYS=90`, `SIGNAL_MAX_DISPATCHES_PER_DAY=2`, `SIGNAL_MIN_HOURS_SINCE_CHECK=6`.
- Mini: `~/Library/LaunchAgents/com.urosh.scout-signals.plist` (hourly, `~/scout-replay/env`),
  logs under `~/scout-signals/`. GitHub fallback: the 4 AM run also calls the poller in
  read-only mode (so a home outage only delays, never loses, a signal).

### Tests (`tests/test_signals.py`, `tests/test_monitor_gates.py`)

Recorded EDGAR submissions + Ashby fixtures → signals; thresholds and department detection; fingerprint
dedupe across polls; the dispatch cap and the 6-hour guard (in-memory store from
`test_replay_calls.py`); state round-trip; triage prompt carries the signals block; a triggered
check marks `last_checked` so `_is_due` is false at the next anchor; `triggered_by` renders.

### Verification on RC, then production

Dry `workflow_dispatch` on `rc` with `slugs=openai…`, `force=true`, `reason=test`, `dry_run=1`,
after seeding one synthetic hiring signal into `rc/signals/…`; the run log shows the signals block
in triage. Production: seed `watch` on the OpenAI and Anthropic cards + the archived Salesforce card
(EDGAR); the first real 8-K or hiring delta is the proof, with the chip on the alert.

Effort: 10 to 14 hours. Spend: polling $0; at most 2 triggered checks a day (each the price of a
normal card check, $0.20 to $4).

## WS4: Scout over MCP

Goal: Scout's cards and Ask as tools other agents can call. stdio first (the "add Scout to Claude
Code" demo with no hosting), then the same server mounted on the engine at `/mcp`.

### Files

- `scout/mcp_server.py` (new; `from mcp.server.fastmcp import FastMCP`):
  - `list_battlecards() -> [{slug, competitor, my_company, focus, last_checked, claims}]`
  - `get_battlecard(slug, section?: str, persona?: str) -> markdown` (from `page.content_html` →
    text, or `store.load_claims` + `render.claims_to_markdown` for a clean text form)
  - `recent_changes(slug, days=7) -> [alerts]` (from `alerts.jsonl`)
  - `sources(slug) -> [{url, class, tier, as_of, claims}]` (the `/sources` page's data)
  - `ask_scout(question, mode="quick"|"deep", competitor?) -> {paragraphs, sources, cut_log,
    unanswered, cost_usd, id}` → `ask.quick_ask` / `ask.ask`, through `ledger.Ledger` with the
    same reservations; requires a bearer key from `ASK_API_KEYS` when served over HTTP.
  - Reads are free and unauthenticated; `ask_scout` is the only metered tool.
  - `python -m scout.mcp_server` = stdio transport (`mcp.run()`); `claude mcp add scout -- python
    -m scout.mcp_server` for the demo.
- `engine/app.py`: `app.mount("/mcp", mcp_server.streamable_http_app())` behind a small ASGI
  middleware that checks the bearer for `tools/call` of `ask_scout` (reads pass); CORS as today.
- `docs/mcp.md`: the stdio demo, the remote URL, the auth note (Claude Code and the API take a
  static bearer; Claude.ai's connector UI needs OAuth 2.1, the upgrade path, not built).
- README: one paragraph; the viewer's footer link "Use Scout from your agent (MCP)".

### Tests (`tests/test_mcp.py`)

FastMCP's in-memory client: every tool's schema and output shape; `get_battlecard` for a real
roster slug; `ask_scout` with `ask.quick_ask` faked (no spend) and the ledger reservation
observed; the HTTP mount rejects `ask_scout` without a bearer and serves `list_battlecards`
without one.

### Verification

Claude Code on the mini with `claude mcp add scout …` calling `get_battlecard` and `recent_changes`;
then the same over `https://scout-engine-rc-…/mcp` with the owner key; one `ask_scout` quick call
on RC (~$0.60).

Effort: 6 to 8 hours. Spend: reads $0; `ask_scout` metered by the existing ledger.

## Sequencing and decisions

1. WS3 first (10 to 14 h): it changes what the cards know; WS4 exposes what they know.
2. Both behind flags on `main` (`SCOUT_SIGNALS`, and the `/mcp` mount only when `SCOUT_MCP=1`),
   built on `rc`, reviewed on the RC services, promoted in one merge as before.
3. Decided by Uroš 2026-09-29: hiring is context, not a trigger (a brand-new department is the one
   hiring signal, queued for the next run); a triggered run never emails on its own; `ask_scout`
   over `/mcp` is owner-key only.
4. Still open (his call): which cards get `watch` first. Proposed: EDGAR on every public company on
   the roster or its my_company side (Salesforce archived card; Microsoft/Slack via Salesforce;
   Google, Amazon, Atlassian), job boards as context on OpenAI, Anthropic, Notion, Perplexity.
