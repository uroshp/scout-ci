1# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Scout — a competitive intelligence tool. Given a competitor (and optionally the user's own company and a focus area), it produces a verified strategic brief. The defining feature is a **two-pass generate-then-verify pipeline**: the second pass independently re-checks every claim from the first and cuts anything it can't confirm, logging each removal in a user-facing **Cut Log**. The verification step is the product, not a polish step.

## Repo layout — two self-contained versions

The repo holds two generations, each in its own folder with its own `requirements.txt`,
and README (the methodology is in the private judgment pack):

- **`v1/`** — the shipped **pipeline** (this CLAUDE.md mostly documents v1; details below).
- **`v2/`** — **Agent Scout**, the model-driven evolution (`app_v2.py` + the `scout/` package,
  living battlecards under `v2/battlecards/`). See [`v2/README.md`](v2/README.md) and
  [`v2/docs/v2-agent-spec.md`](v2/docs/v2-agent-spec.md). Key v2 conventions: data paths are
  anchored to `scout.config.APP_ROOT` (works from any CWD; Streamlit Cloud runs from the repo
  root with entrypoint `v2/app_v2.py`); the two GitHub Actions live in `.github/` (at the repo
  root, required) but run with `working-directory: v2`; self-serve USER DATA (requests, generated
  cards, the spend ledger) lives in a SEPARATE PRIVATE repo (`config.SELFSERVE_REPO`, NOT this
  public repo) which `scout/selfserve.py` reads/writes via the GitHub API; the app triggers the
  generation Action by POSTing a `workflow_dispatch`. The viewer is read-only — it never
  generates or monitors.

`.github/`, `.streamlit/`, `.env.example`, `LICENSE`, and this `CLAUDE.md` stay at the repo root.

## Commands

```bash
# v1 (the pipeline) — from v1/
streamlit run v1/app.py         # web UI (password-gated; APP_PASSWORD env, default "worksmarter")
python v1/research.py           # run the pipeline headless (hardcoded: Slack vs Microsoft Teams in __main__)
python v1/test.py               # smoke-test the Anthropic API connection

# v2 (Agent Scout) — read-only viewer; engine runs headless via the Actions
streamlit run v2/app_v2.py      # the living-battlecard viewer
python -m scout.monitor         # daily re-check (run from v2/; SCOUT_MONITOR_LIVE=1 to write)
python -m unittest discover -s tests   # v2 unit tests (run from v2/; security + scheduling invariants)
```

No build step, no linter. v2 has a focused stdlib-`unittest` suite in `v2/tests/` (security + scheduling invariants — no extra deps); run it from `v2/` with `python -m unittest discover -s tests`. `v1/test.py` and `v1/sdk_test.py` are ad-hoc scratch scripts, not part of it. Environment: copy the root `.env.example` to a `.env` and set `ANTHROPIC_API_KEY` (and `APP_PASSWORD`). Dependencies are per-folder: `pip install -r v1/requirements.txt` or `pip install -r v2/requirements.txt`.

## Architecture (v1)

The pipeline is a **fixed control flow written in code** (`research_competitor`): `generate_brief` → `verify_brief` → `save_report`. This is deliberate (v1 is a pipeline, not an agent — see Roadmap in README). Both passes are single `client.messages.create` calls with the Anthropic **web search tool** enabled.

- **`v1/research.py`** — the retired engine's control flow (`research_competitor`: generate → verify → save) and its deterministic helpers. Its two prompts and shared rule blocks are no longer in this file; the complete v1 engine is archived in the private judgment pack.
- **The methodology and every prompt** (v1 and v2) live in the PRIVATE judgment pack (`judgment/pack.json` in the private data repo), loaded through `v2/scout/judgment.py`. They are not in this repo. v1's `generate_brief` / `verify_brief` are retired stubs.
- **`v1/app.py`** — Streamlit UI: password gate, two tabs (sample reports / run-your-own), daily run limit, fake progress messages. Calls the same engine functions.
- **`v1/reports/`** — committed sample briefs. The in-app "Sample Reports" dropdown reads directly from this directory (`list_samples` parses the `Label_vs_Label_DATE_TIME.md` filename convention). New live runs also save here via `save_report`.

### The control-vs-model boundary (the key design principle)

Anything **deterministically fixable is fixed in code; anything requiring judgment is left to the model.** When editing, respect this line:

- `clean_output` (v1/app.py) — strips preamble before the title, escapes `$` (Streamlit renders `$…$` as LaTeX and garbles every dollar figure), de-indents lines (indented lines render as gray code blocks).
- `format_report` (v1/app.py) — drops stray cover-block lines before the title, stitches bullets whose text leaked onto the next line, collapses blank lines between consecutive bullets.
- `_from_title` / `_extract` (v1/research.py) — `_extract` rebuilds prose from response content blocks and appends **real** source links pulled from the web_search tool's citation objects (URLs the model cannot fabricate); `_from_title` guarantees the saved brief starts at the report title even if the model adds preamble.

Don't ask the model to do work these functions already handle reliably, and don't move judgment work (analysis, sourcing, what to cut) into code.

## The judgment pack (2026-10-02)

Scout's instruction text is private. Modules name their blocks (`_JUDGE_SYSTEM = judgment.get("propagate._JUDGE_SYSTEM")`);
the text is in `judgment/pack.json` (private repo; `rc/judgment/pack.json` for rc). Rules when editing:

- NEVER paste prompt or methodology text into this repo (code, tests, docs, commit messages).
- Edit a block in the private repo's checkout (`~/code/scout-user-data/judgment/pack.json`) via `python scripts/judgment_pack.py` (run from `v2/`); a changed pack version opens a new eval period.
- `python scripts/judgment_pack.py verify` must report every block byte-identical to its frozen hash after any refactor that touches a block's call site.
- Every model-call choke point calls `judgment.require()` and `judgment.assert_clean(...)`; keep that when adding one.

## Shipping a monitor change (2026-10-03)

The morning run has no dry-run equivalent: a dry run skips the write-only path where two changes
broke production this week. Every change to `scout/monitor.py`, `scout/propagate.py`,
`scout/audience.py`, `scout/notify.py` or `.github/workflows/monitor.yml` ships like this:

1. Build on `rc`; `python -m unittest discover -s tests` from `v2/` (the live-wiring test in
   `tests/test_monitor_live_wiring.py` runs `check(write=True)` in production's mode through the
   real store with fakes only at the model boundary; extend it when a step is added).
2. **Rehearse on rc**: dispatch `monitor.yml` on `rc` with `rehearsal=<archive slug[,slug]>`
   (`cursor__vs__cognition__general` is the everyday card). It runs the REAL write path on the
   retired card in a store root outside the checkout, every private-store write under
   `rehearsal/`, every email subject prefixed `[rehearsal]`, nothing committed; the job summary
   holds the step table and the diff. Avoid 03:00–05:30 PT.
3. Read the rehearsal's **lifecycle audit** (the `[lifecycle]` line in the run log, the FYI footer,
   `lifecycle/<stamp>.json` under the rehearsal prefix; `python scripts/lifecycle_report.py <stamp>`
   renders the page). It follows every finding from the searches to the card and into the eval
   lanes against the promise's rules (focus coverage, boundary accounting, continuity, eval
   continuity). A release is not green until the audit is GREEN and Uroš has seen the page: the
   two 10/3-10/4 bugs that invalidated the tool were invisible to every other signal.
4. Uroš approves; merge `rc` into `main`; **rehearse on main** the same way (it proves the exact
   code and workflow expressions the 4 AM run will use).
5. The first morning is watched: the FYI footer's run-health and lifecycle lines, the needs-you
   email (every failed step and every red lifecycle rule is an item), the step table in
   `costs/<stamp>.json`, and the 09:00 PT canary's `check_steps` and `check_lifecycle`.

Rehearsals cost real model calls ($0.20 for a quiet card, up to $3 with news) and run ONLY when
Uroš asks for one (2026-10-05: "enough with rehearsals"). The standing gate for a change is the unit
suite plus the lifecycle audit that runs inside every real morning run and emails when a rule
breaks; steps 2 and 4 above are reserved for changes he judges risky, on his say-so. A release may
bundle several changes (the step rows attribute a failure to a step). Rollback levers that need no
deploy: repo variables (`SCOUT_SIGNALS`, `SCOUT_SENSORS`), and the env in `monitor.yml` for
everything else.

## Conventions specific to this repo

- **Models and the Agent SDK move in lockstep** (v2, 2026-10-03). The models run through the Claude
  Code binary that `claude-agent-sdk` bundles, and a new model id can need a newer binary. When a
  model id in `v2/scout/config.py` changes: bump `claude-agent-sdk` to the latest release in BOTH
  `v2/requirements.txt` and `v2/requirements-engine.txt`, raise `MODEL_MIN_CLI`, re-pip the mini's
  venv, redeploy the engine, and rehearse. `tests/test_models_lockstep.py` and `monitor.preflight`
  enforce it; never revert a model to dodge the bump.
- **Model is pinned** to `MODEL = "claude-sonnet-4-6"` in `v1/research.py` — a pinned ID, not an evergreen alias, for reproducibility. Don't swap it for an alias. (Note: `v1/test.py` independently hardcodes an older model for its smoke test.)
- Every saved brief begins with the exact line `# Competitive Intelligence Brief` — multiple functions key off this string to trim preamble. Don't change that title phrasing without updating `_from_title`, `clean_output`, and `format_report`.
- The Cut Log is a **user-facing feature**, not an internal note. Its `## Cut Log` header and the `**CUT — …:**` / `**REVISED — …:**` entry format are load-bearing (CUT = removed from body, REVISED = corrected but still present). Preserve that distinction in prompt edits.
- `v1/sdk_test.py` imports `claude_agent_sdk` (not in `v1/requirements.txt`) — it's an exploratory spike toward the v2 model-driven agent, not part of the shipping app.
