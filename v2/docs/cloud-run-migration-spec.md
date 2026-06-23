# Cloud Run migration spec — drop Streamlit, deploy under agent-scout.ai

Status: **proposed** (spec only, no code). Parallel build, zero risk to the live app.

## Decision

Replace Streamlit with a thin **FastAPI** app on **Cloud Run**, served at **agent-scout.ai**.
Streamlit is reduced to an optional **one-line redirect** at the old `*.streamlit.app` URL so the
links already on the resume / LinkedIn / applications keep resolving — that is a tombstone
forwarder, not "keeping Streamlit."

**Why not lift-and-shift Streamlit onto Cloud Run.** It only buys migration speed, and the price
is staying inside the wrapper that caused the whole bug class (iframe `height=0` prod crash,
sticky-rail scroll, `$`→LaTeX, hot-reload races). Moving in order to stay locked in defeats the
move. Rejected.

## The leverage: what carries over vs. what is rebuilt

The value lives in `page.py` (the card HTML) and the **headless engine** (Actions + GCP). Streamlit
is a ~400-line shell on top. The migration replaces that shell.

| Module | LOC | In the migration |
|---|---|---|
| `scout/page.py` | 994 | **Reused as-is** — emits the full card HTML, incl. the rail + the new feed-collapse fix. |
| `scout/display.py` | — | **Reused as-is** — loads card data / alerts / status from the battlecards store. |
| `scout/selfserve.py` | 387 | **Reused as-is** — GitHub-API backend for the request form (`submit`, `dispatch_generation`, `get_request`, `get_result`, `gate`, validators). Framework-agnostic. |
| `scout/config.py` | — | **Reused as-is** — paths, brand constants, repo config. |
| `scout/analytics.py` | 297 | **Trimmed** — keep the client GA tag (now in `<head>`, no iframe) + optional server-side GA4 event; drop the first-party JSONL writes. |
| `app_v2.py` | 909 | **Rebuilt** (~400 lines of it) → FastAPI routes + 2 small templates + the form page's JS. |

## Target architecture

- **FastAPI + uvicorn**, one container, on Cloud Run.
- Routes:
  - `GET /` → index / card list (small template).
  - `GET /c/{slug}` → `page.render_page(slug)` wrapped in a full HTML document.
  - `GET /print/{slug}` → the print / call-sheet view (native, no iframe).
  - `POST /api/request` → self-serve submit → `selfserve.submit` + `dispatch_generation`.
  - `GET /api/status/{job_id}` → `selfserve.get_request` / `get_result`.
  - `GET /static/*` → icons, the detective-dog logo (when created), favicon.
- **Document wrapper.** `page.render_page` returns a fragment (`<style>` + `#scout-page` div) —
  Streamlit used to supply the outer page. The wrapper adds `<!doctype html><html><head>` with the
  GA tag, `<title>`, favicon, and `og:`/`twitter:` meta, then the fragment in `<body>`.
- **The iframe hacks all vanish.** GA → straight in `<head>`. Print, live countdown, scroll-to-top →
  native HTML/JS. (8 `st.iframe` call sites → 0.)

## The one hard part: self-serve form + job poller

The only genuinely stateful piece (14 `session_state` + 5 `rerun` + a polling loop today).

- Static form page → JS `fetch('/api/request', {POST})` → returns `job_id`.
- JS polls `GET /api/status/{job_id}` every few seconds → updates a progress UI → on `done`, links
  to the finished card. Optional email-notify field passes straight through to `selfserve`.
- The submit rate-limit (today's `_submit_times` history) moves **server-side** into the endpoint.
- `selfserve.py` is wrapped verbatim — no logic change.

## Analytics — GA-only, and reliability goes *up*

- **Client GA tag in `<head>`**, no iframe `window.parent` injection. That hack (which the code notes
  fails on iOS Safari) is the likely reason GA missed visits like Paris. Direct-in-head is reliable
  by construction.
- **Optional:** keep the server-side GA4 Measurement Protocol event (`analytics._ga4_server_event`),
  fired from the request handler — reliable, server-side, immune to ad-blockers.
- **Free reliable counter:** Cloud Run access logs record every HTTP hit in Cloud Logging — a 100%
  reliable raw count with zero code. This **more than replaces** the first-party JSONL "catcher"
  that the Streamlit count was providing.
- **Dropped:** first-party JSONL (`_append_visit`) + data-repo visit writes + the keep-warm cron
  (no sleep on Cloud Run, so nothing to keep warm).

## Hosting & deploy

- **Dockerfile**: `python:3.x-slim` + `uvicorn` + `requirements`.
- **Cloud Run** service in the existing GCP project (rename to drop "monitor" if desired).
- **Domain mapping** `agent-scout.ai` → service; TLS auto-provisioned. DNS records at the registrar.
- **Secrets** → env / Secret Manager: GitHub token (for `selfserve`), GA measurement id. No
  `ANTHROPIC_API_KEY` in the viewer — generation runs in the GitHub Action, the app only POSTs a
  `workflow_dispatch`.

## Parallel track & cutover (the discipline that contains the risk)

| Phase | Action | Live app |
|---|---|---|
| 0 | Scaffold FastAPI app + Dockerfile locally; reuse `page`/`display`/`selfserve`. | untouched |
| 1 | Deploy to the default `*.run.app` URL. | untouched |
| 2 | **Verify** (gate below). | untouched |
| 3 | Map `agent-scout.ai` DNS → service; test on the domain. | untouched |
| 4 | **Cut over** — use `agent-scout.ai` in new contexts. Streamlit = instant rollback. | rollback |
| 5 | Reduce `*.streamlit.app` to a redirect → `agent-scout.ai` (preserve old links). | redirect |

Rollback at any point: `agent-scout.streamlit.app` stays live and untouched through phase 4.

## Verification gate (the "no unforeseen bugs" answer)

- **All 9 cards:** HTML diff of the `page.py` body (must match the live render minus the wrapper) +
  visual spot check — **sticky rail**, the **feed collapse**, `$` figures, deep-link anchors.
- **Narrow / mobile** breakpoint (the rail goes static).
- **Print view.**
- **Self-serve E2E:** one real request → Action runs → status polls → card appears.
- **Analytics:** GA event in realtime + a matching Cloud Run access-log entry.
- **CI boot/smoke test** of the FastAPI app.

## What we drop / residual risk

- Drop first-party analytics (mitigated, see above) and the keep-warm cron.
- Main new-bug surface is the **form rebuild** → covered by the E2E test.
- DNS/TLS is standard → done in phase 3, before cutover.
- The Streamlit-specific bug classes **cannot recur** (no Streamlit, no iframe).

## Effort

Per piece: static cards + index + GA + routing = **low** (page.py does the work); form + poller
rebuild = **medium** (only stateful piece; `selfserve`/`analytics` reused); Docker + Cloud Run +
DNS = **low-medium**. Overall **medium** — a focused couple of days of coding plus parallel
testing, no live risk, ~$0 API (only a test generation costs a few cents).

## Resolved decisions

1. **Always-warm.** Cloud Run **min-instances = 1** — instant for every visitor, no cold start.
2. **Single cutover (both viewer + form at once).** Not phased. Phasing would split the form onto
   `streamlit.app` while the viewer lived at `agent-scout.ai` (cross-domain "view your card" links) —
   more complexity, and no live-risk benefit since Streamlit stays the fallback until everything is
   verified. One DNS flip, one redirect.
3. **Handlers are synchronous — async is explicitly NOT a goal.** Async was never something we
   wanted: the rerun-based job poll was a Streamlit artifact (dies with Streamlit), and FastAPI's
   async runtime is opt-in (sync `def` handlers run fine). The submit→poll *UX* stays — generation
   takes minutes (a GitHub Action), intrinsic to the work, not Streamlit — but becomes ~15 lines of
   client-side `fetch`-and-poll instead of the rerun hack. **Framework: Flask** (sync-native,
   Jinja-native, right-sized for serve-HTML-plus-two-endpoints; no inherited complexity). FastAPI
   with sync handlers was the alternative — kept only for Pydantic validation + auto `/docs`, both
   minor — and was set aside in favor of simplicity.
4. **All three analytics layers:** client GA tag in `<head>` + server-side GA4 Measurement Protocol
   event (from the request handler) + Cloud Run access logs (the free, reliable raw counter).
