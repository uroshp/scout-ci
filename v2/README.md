# Scout v2 — Agent Scout (living battlecards)

> Part of the [Scout repo](../README.md). This is **v2**: the model-driven evolution of the v1
> [pipeline](../v1/). It replaces the fixed `generate → verify` flow with a tool-use loop, adds a
> fetch-and-read-the-source grounding tool, and turns each brief into a **living battlecard** that
> a daily agent keeps current. This is the deployed app.

🔗 **[Live demo](https://agent-scout.streamlit.app)**

## What's different from v1

- **Model-driven orchestration.** The agent decides its own steps (research → draft → verify),
  parallelizes subagent work, and runs under iteration/cost guards — instead of two hardcoded calls.
- **Grounding.** Claims are re-fetched and checked against the actual source text before they survive.
- **Living battlecards.** Each card is stored as JSON state + rendered markdown under
  `battlecards/<slug>/`, and a daily GitHub Action re-checks it, surfacing only what's *new and
  material* (with a per-card cadence and a cheap triage gate to keep cost down).
- **The viewer** (`app_v2.py`) is **read-only** — it renders committed cards plus the four
  "show-the-agentic-work" elements (freshness strip, change feed, just-updated, per-claim timestamps).
  It never generates or monitors.
- **Self-serve** ("create your own") runs **out-of-band**: the app commits a request, a GitHub
  Action runs the same pipeline headless and commits a private result to `user_reports/`.

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
`v2/battlecards`, `v2/methodology.md`, etc.)

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
│   └── prompts.py       # SOURCE_HIERARCHY / FORMATTING_RULES (+ loads methodology.md)
├── battlecards/         # committed living battlecards (the showcase set)
├── selfserve/           # request queue + gate state (state.json)
├── user_reports/        # private self-serve outputs (never shown publicly, never monitored)
├── scripts/             # run_selfserve.py (Action entrypoint), render_static.py, source_audit.py
├── assets/              # logo / icon
├── docs/                # v2 spec, claim-object schema, launch decision log
├── methodology.md       # the CI discipline (v2's own copy; v1 keeps its own)
└── requirements.txt
```

## Automation (GitHub Actions, defined at the repo root, run inside `v2/`)

- **`monitor.yml`** — daily cron; runs `python -m scout.monitor`, commits `last_checked` bumps every
  run and content changes only on a material change. Inert until merged to the default branch.
- **`selfserve.yml`** — triggered by a `workflow_dispatch` the app POSTs on submit; runs
  `scripts/run_selfserve.py`, which reads the request from / writes the private card to the
  SEPARATE PRIVATE data repo (`SELFSERVE_REPO`) via the GitHub API — never this public repo.

Both set `working-directory: v2`. Self-serve user data (requests, cards, the gate ledger) is kept
out of this public repo entirely: it lives in `SELFSERVE_REPO` (a private repo) and is reached
through `scout/selfserve.py`'s GitHub-API backend, so submissions are never world-readable.

See [`docs/architecture.md`](docs/architecture.md) for diagrams (generation flow, monitoring loop,
control-vs-autonomy) and [`docs/v2-agent-spec.md`](docs/v2-agent-spec.md) for the full design.
