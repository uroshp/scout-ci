# Scout v2 — Agent Scout (living battlecards)

> Part of the [Scout repo](../README.md). This is **v2**: the model-driven evolution of the v1
> [pipeline](../v1/). It replaces the fixed `generate → verify` flow with a tool-use loop, adds a
> fetch-and-read-the-source grounding tool, and turns each brief into a **living battlecard** that
> a daily agent keeps current. This is the deployed app.

🔗 **[Live demo](https://agent-scout.ai)**

## What's different from v1

- **Model-driven orchestration.** The agent decides its own steps (research → draft → verify),
  parallelizes subagent work and runs under iteration/cost guards. Specialized agents are kept separate so the verification step stays independent of the drafting step.
- **Grounding.** Claims are re-fetched and checked against the actual source text by model-free code (httpx + fuzzy match) before they survive, so a model can’t fake a citation, because the code that verifies provenance shares nothing with the code that writes the claim. 
- **Living battlecards.** Each card is stored as JSON state + rendered markdown under
  `battlecards/<slug>/`, it's re-checked twice a day, before the work day and at lunch, surfacing only what's *new and
  material* (with a per-card cadence and a cheap triage gate to keep cost down).
- **The viewer** (`server.py`, Flask on Cloud Run at agent-scout.ai; `app_v2.py` is the retired Streamlit
  stub) is **read-only and holds no model key**: it renders committed cards, the freshness/updates
  indicators, every citation's source kind (`/c/<slug>/sources`),
  and a per-persona view (`?persona=`). It never generates or monitors; self-serve requests are handed to
  an Action. Every change is reviewed on a release-candidate service before it reaches the main domain
  (`docs/cloud-run-setup.md`, "The RC environment").
- **Ask Scout** (the chat panel on every card; `scout/ask.py`, `scout/askui.py`, `engine/`) answers a
  rep's question in about a minute from everything Scout has already verified about the companies
  named, through the same verifier the cards go through, and offers "Research deeper" (web research,
  a few minutes) as the next turn. Every sentence shown was fact-checked or it is not shown; what
  was cut is listed. It runs in a separate Cloud Run service with a hard daily spend ceiling
  (`docs/cloud-run-setup.md`, "The Ask Scout engine").
- **Signals** (`scout/signals.py`, the mini's hourly poller): a new SEC filing on a watched company
  starts that card's check within the hour and shows as "Triggered by: new filing" on the alert; a
  brand-new hiring department is queued for the next run; other hiring deltas are context for the
  judgment, never a trigger. A triggered run never emails on its own; its findings ride the next
  morning's FYI (`docs/signals-and-mcp-build-plan.md`).
- **MCP** (`scout/mcp_server.py`): the cards and Ask as tools for other agents, over stdio or the
  engine's `/mcp` (`docs/mcp.md`).
- **Self-serve** ("create your own") runs **out-of-band**: the app commits a request, a GitHub
  Action runs the same pipeline headless and commits a private result to a private repo visible only to the author.

## Run it

```bash
cd scout-ci/v2
pip install -r requirements.txt
cp ../.env.example .env        # add ANTHROPIC_API_KEY (+ SELFSERVE_*/RESEND_* for the full backend)
streamlit run app_v2.py
```

(From the repo root: `streamlit run v2/app_v2.py`. All data paths are anchored to this folder via
`scout.config.APP_ROOT`, so the app works regardless of the working directory — which is why
Streamlit Cloud, running from the repo root with the entrypoint set to `v2/app_v2.py`, finds
`v2/battlecards`, etc.)

## Structure

```
v2/
├── app_v2.py            # READ-ONLY Streamlit viewer for committed battlecards
├── scout/               # the engine package
│   ├── config.py        # central config + APP_ROOT / REPO_SUBDIR path anchors
│   ├── generate.py      # the agentic generate→verify loop (Claude Agent SDK)
│   ├── monitor.py       # daily re-check: triage gate → material-change detection → alerts
│   ├── grounding.py     # fetch-and-read-the-source verification
│   ├── store.py         # battlecards/<slug>/ JSON state + rendered markdown
│   ├── selfserve.py     # async "create your own" gate + GitHub-API backend
│   ├── display.py       # the viewer's data (freshness, change feed, claim timestamps)
│   ├── judgment.py      # the only door to the private judgment pack (prompts, rules, methodology)
│   └── prompts.py       # names the shared blocks (SOURCE_HIERARCHY / WRITING_STYLE / methodology), text loaded from the pack
├── battlecards/         # committed living battlecards (the showcase set)
├── selfserve/           # request queue + gate state (state.json)
├── scripts/             # run_selfserve.py (Action entrypoint), render_static.py, source_audit.py
├── assets/              # logo / icon
├── docs/                # v2 spec, claim-object schema, launch decision log
└── requirements.txt
```

## Automation (GitHub Actions, defined at the repo root, run inside `v2/`)

- **`monitor.yml`** — daily run (Cloud Scheduler fires a `workflow_dispatch` at 7am ET Mon–Sat; no
  GitHub `schedule:` cron); runs `python -m scout.monitor`, commits `last_checked` bumps every
  run and content changes only on a material change. Inert until merged to the default branch.
- **`selfserve.yml`** — triggered by a `workflow_dispatch` the app POSTs on submit; runs
  `scripts/run_selfserve.py`, which reads the request from / writes the private card to the
  SEPARATE PRIVATE data repo (`SELFSERVE_REPO`) via the GitHub API — never this public repo.

Both set `working-directory: v2`. Self-serve user data (requests, cards, the gate ledger) is kept
out of this public repo entirely: it lives in `SELFSERVE_REPO` (a private repo) and is reached
through `scout/selfserve.py`'s GitHub-API backend, so submissions are never world-readable.

See [`docs/architecture.md`](docs/architecture.md) for diagrams (generation flow, monitoring loop,
control-vs-autonomy) and [`docs/v2-agent-spec.md`](docs/v2-agent-spec.md) for the full design.
[`ROADMAP.md`](ROADMAP.md) records what I chose to defer and the next step for each.
