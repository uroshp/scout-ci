# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Scout — a competitive intelligence tool. Given a competitor (and optionally the user's own company and a focus area), it produces a verified strategic brief. The defining feature is a **two-pass generate-then-verify pipeline**: the second pass independently re-checks every claim from the first and cuts anything it can't confirm, logging each removal in a user-facing **Cut Log**. The verification step is the product, not a polish step.

## Commands

```bash
streamlit run app.py            # run the web UI (password-gated; APP_PASSWORD env, default "worksmarter")
python research.py              # run the pipeline headless (hardcoded: Slack vs Microsoft Teams in __main__)
python test.py                  # smoke-test the Anthropic API connection
```

No build step, no linter, no test suite. `test.py` and `sdk_test.py` are ad-hoc scratch scripts, not a test framework. Environment: copy `.env.example` to `.env` and set `ANTHROPIC_API_KEY` (and `APP_PASSWORD`). Dependencies: `pip install -r requirements.txt`.

## Architecture

The pipeline is a **fixed control flow written in code** (`research_competitor`): `generate_brief` → `verify_brief` → `save_report`. This is deliberate (v1 is a pipeline, not an agent — see Roadmap in README). Both passes are single `client.messages.create` calls with the Anthropic **web search tool** enabled.

- **`research.py`** — the engine. Two large prompts (`generate_brief`, `verify_brief`) that share three constants injected into both: `SOURCE_HIERARCHY` (a typed trust ladder — Tier 1A audited fact vs 1B self-positioning vs 2E analyst estimate vs 4 raw sentiment; the prompts forbid blurring fact / company-claim / estimate / sentiment), `FORMATTING_RULES`, and the contents of `methodology.md`. `verify_brief` is adversarial by design — it re-searches the draft's claims and returns only survivors plus the Cut Log.
- **`methodology.md`** — the CI discipline the model is held to, kept as a plain-English spec **out of code on purpose** so it can be edited without touching the engine. Loaded at runtime by `load_methodology()` and embedded in the generate prompt.
- **`app.py`** — Streamlit UI: password gate, two tabs (sample reports / run-your-own), daily run limit, fake progress messages. Calls the same engine functions.
- **`reports/`** — committed sample briefs. The in-app "Sample Reports" dropdown reads directly from this directory (`list_samples` parses the `Label_vs_Label_DATE_TIME.md` filename convention). New live runs also save here via `save_report`.

### The control-vs-model boundary (the key design principle)

Anything **deterministically fixable is fixed in code; anything requiring judgment is left to the model.** When editing, respect this line:

- `clean_output` (app.py) — strips preamble before the title, escapes `$` (Streamlit renders `$…$` as LaTeX and garbles every dollar figure), de-indents lines (indented lines render as gray code blocks).
- `format_report` (app.py) — drops stray cover-block lines before the title, stitches bullets whose text leaked onto the next line, collapses blank lines between consecutive bullets.
- `_from_title` / `_extract` (research.py) — `_extract` rebuilds prose from response content blocks and appends **real** source links pulled from the web_search tool's citation objects (URLs the model cannot fabricate); `_from_title` guarantees the saved brief starts at the report title even if the model adds preamble.

Don't ask the model to do work these functions already handle reliably, and don't move judgment work (analysis, sourcing, what to cut) into code.

## Conventions specific to this repo

- **Model is pinned** to `MODEL = "claude-sonnet-4-6"` in `research.py` — a pinned ID, not an evergreen alias, for reproducibility. Don't swap it for an alias. (Note: `test.py` independently hardcodes an older model for its smoke test.)
- Every saved brief begins with the exact line `# Competitive Intelligence Brief` — multiple functions key off this string to trim preamble. Don't change that title phrasing without updating `_from_title`, `clean_output`, and `format_report`.
- The Cut Log is a **user-facing feature**, not an internal note. Its `## Cut Log` header and the `**CUT — …:**` / `**REVISED — …:**` entry format are load-bearing (CUT = removed from body, REVISED = corrected but still present). Preserve that distinction in prompt edits.
- `sdk_test.py` imports `claude_agent_sdk` (not in `requirements.txt`) — it's an exploratory spike toward the v2 model-driven agent, not part of the shipping app.
