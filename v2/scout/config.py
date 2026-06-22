"""Central config for Scout v2.

Model IDs and guard limits are read from the environment with verified defaults,
so the code survives model renames without edits (v2-agent-spec.md §3, §10).
Never hardcode a model ID elsewhere — import from here.
"""
import os

from dotenv import load_dotenv

load_dotenv()

# --- Paths -------------------------------------------------------------------
# The v2 app root (the directory holding this `scout` package) resolved from this file, so
# data paths work no matter the working directory — Streamlit Cloud runs from the git repo
# root, not from v2/. REPO_SUBDIR is v2's location WITHIN the git repo, used only for the
# GitHub-API commit paths in selfserve.py (which are repo-root-relative, not local-FS paths).
APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_SUBDIR = "v2"

# --- Models (defaults verified against Anthropic docs, June 2026) -------------
# Orchestrator: judgment — planning, materiality, synthesis, consistency.
ORCHESTRATOR_MODEL = os.environ.get("ORCHESTRATOR_MODEL", "claude-opus-4-8")
# Subagents: legwork — research + verification, parallelized.
SUBAGENT_MODEL = os.environ.get("SUBAGENT_MODEL", "claude-sonnet-4-6")
# Triage gate: cheap "is there anything here at all?" check (§6). Runs on EVERY check, so
# it uses Haiku — the cheapest model — to keep the no-news floor low (lever A).
FAST_MODEL = os.environ.get("FAST_MODEL", "claude-haiku-4-5-20251001")

# --- Analytics ----------------------------------------------------------------
# GA4 Measurement ID for the viewer. This is a CLIENT-side id (it ships in every
# visitor's page source), so it is not a secret. Empty string disables tracking.
GA_MEASUREMENT_ID = os.environ.get("GA_MEASUREMENT_ID", "G-MR1Z8NB7BP")
# GA4 Measurement Protocol API secret (GA4 Admin -> Data Streams -> Measurement Protocol API
# secrets). Set in Streamlit secrets to enable the unblockable SERVER-SIDE visit feed; the
# client-side gtag above keeps working regardless. Empty -> server feed disabled, no-op.
GA_API_SECRET = os.environ.get("GA_MP_API_SECRET", "")

# --- Guards (§10) -------------------------------------------------------------
# Hard caps so an agent that can loop can't burn money. The SDK enforces both
# natively (ClaudeAgentOptions.max_turns / max_budget_usd).
MAX_TURNS = int(os.environ.get("SCOUT_MAX_TURNS", "40"))
# Per-query ceiling. Fine for a monitoring check (~$1-1.9). NOT enough for a full
# generation: the orchestrator runs the researcher + verifier subagents INLINE in
# one query, so this cap must cover the whole two-pass brief (measured ~$5.77) —
# hence GEN_MAX_BUDGET_USD below. This default governs monitoring.
MAX_BUDGET_USD = float(os.environ.get("SCOUT_MAX_BUDGET_USD", "3.0"))
# Generation's own ceiling (orchestrator + both subagents, one query). Expected
# spend ~$6; this is only a runaway backstop, not a target.
GEN_MAX_BUDGET_USD = float(os.environ.get("SCOUT_GEN_MAX_BUDGET_USD", "10.0"))

# Triage runs on EVERY monitoring check and most windows are quiet, so it gets its OWN
# tight caps (lever B) — far below the monitoring MAX_BUDGET_USD that governs the rare
# Opus materiality escalation. Few searches (structurally capped by max_turns) + a
# sub-dollar budget + Haiku keep the routine no-news check at pennies.
TRIAGE_MAX_TURNS = int(os.environ.get("SCOUT_TRIAGE_MAX_TURNS", "8"))
TRIAGE_MAX_BUDGET_USD = float(os.environ.get("SCOUT_TRIAGE_MAX_BUDGET_USD", "0.50"))
TRIAGE_MAX_SEARCHES = int(os.environ.get("SCOUT_TRIAGE_MAX_SEARCHES", "5"))
# Propose pass (propagation step 3): tools-off Sonnet drafting over already-grounded facts, so it
# is short and cheap. Tight caps keep it bounded; it never searches or fetches.
PROPOSE_MAX_TURNS = int(os.environ.get("SCOUT_PROPOSE_MAX_TURNS", "6"))
PROPOSE_MAX_BUDGET_USD = float(os.environ.get("SCOUT_PROPOSE_MAX_BUDGET_USD", "0.50"))
# Judge pass (propagation step 4): tools-off Opus adversarially confirming/rejecting the proposer's
# ops against the same grounded facts. Also short — it reasons, never searches — but on the pricier
# model, so it keeps its own (slightly higher) ceiling than propose.
JUDGE_MAX_TURNS = int(os.environ.get("SCOUT_JUDGE_MAX_TURNS", "6"))
JUDGE_MAX_BUDGET_USD = float(os.environ.get("SCOUT_JUDGE_MAX_BUDGET_USD", "0.75"))

# --- Shadow-eval challenger (v3.5; docs/vnext-roadmap.md §v3.5, decision-log §11) -------------
# The verification-judge CHALLENGER: a tools-off model that re-judges captured champion decisions
# (kept/cut claims) over their CAPTURED EVIDENCE — no re-research — to mine disagreements with the
# code grader for offline human adjudication. Pinned to SONNET, NOT Haiku (decision-log §11): the
# task is discriminating SUBTLE ungroundedness, where capability buys real accuracy and a cheap
# judge's leniency/verbosity bias skews toward keeping slop; the cost saved by Haiku here is cents
# across the whole trial (offline, batched ~1 call/card, no search) so it isn't worth the accuracy.
# Opus is the production AUTHORSHIP judge (ORCHESTRATOR_MODEL), so keeping the challenger BELOW it
# (Sonnet) means a proven win is a real cost saving and the challenger isn't grading its own family's
# production pipeline. Reserve Opus as a targeted tie-breaker on the hardest band, run by hand.
CHALLENGER_MODEL = os.environ.get("CHALLENGER_MODEL", SUBAGENT_MODEL)   # default Sonnet
CHALLENGER_MAX_TURNS = int(os.environ.get("SCOUT_CHALLENGER_MAX_TURNS", "4"))
CHALLENGER_MAX_BUDGET_USD = float(os.environ.get("SCOUT_CHALLENGER_MAX_BUDGET_USD", "0.50"))
# Propagation mode (step 5) — the shadow-first ladder for the AUTHORSHIP judge (spec §17):
#   "off"    — propagation never runs (no propose/judge spend). DEFAULT.
#   "shadow" — on each act-grade survivor, run propose->judge and LOG the decisions (the authorship
#              training corpus), but NEVER mutate the card and NEVER notify. Earn autonomy here.
#   "review" — like shadow, PLUS email the human each judge-confirmed proposal (where/what/how/
#              verdict). The card is still untouched; a human approves a proposal out-of-band and it
#              is applied in-session (scout/review.py). The human-approval gate.
#   "live"   — also APPLY judge-confirmed ops (add/revise/retire) to the card automatically.
# The card is rewritten automatically only under "live"; "shadow"/"review" never auto-mutate it.
# Promotion off->shadow->review->live is gated on adjudicated authorship deltas, never calendar.
PROPAGATE_MODE = os.environ.get("SCOUT_PROPAGATE_MODE", "off").strip().lower()

# --- Grounding (claim-object.md §4) -------------------------------------------
# Provisional fuzzy threshold (0..1) — to be TUNED from real fetched pages at the
# #7 measurement step, watching the 0.80-0.92 band for true claims being cut.
GROUNDING_FUZZY_THRESHOLD = float(os.environ.get("SCOUT_GROUNDING_FUZZY", "0.92"))
GROUNDING_TIMEOUT_S = float(os.environ.get("SCOUT_GROUNDING_TIMEOUT_S", "20"))
# Contact string for the grounding fetcher's descriptive User-Agent. SEC's
# fair-access policy 403s requests without a declared contact, so a real one is
# needed to ground filings. Defaults to the public project URL so no personal email
# ships in a public default; override with a real contact in .env if a service demands one.
GROUNDING_CONTACT = os.environ.get("SCOUT_GROUNDING_CONTACT", "https://github.com/uroshp/scout-ci")


# --- Shadow-mode eval (v3.5 challenger qualification; docs/vnext-roadmap.md) --
# PURE OBSERVER, OFF by default. When "1", REAL generation/monitor runs record the CHAMPION
# decisions (the deterministic code grader + the verifier cut log) to shadow/<slug>/ in the
# PRIVATE data store, so an OFFLINE challenger model-judge can be scored against them later.
# It triggers no model call and never alters or gates a production run (scout/shadow.py).
SHADOW_EVAL_ENABLED = os.environ.get("SCOUT_SHADOW_EVAL", "") == "1"


# --- Monitoring cadence (per-competitor; A1) ---------------------------------
# Default hours between checks for a battlecard with no explicit cadence_hours in
# its meta.json. Used ONLY by the legacy relative due-gate (when anchors are
# disabled — see MONITOR_ANCHORS_UTC). run_all() honors per-card cadence via the
# gate; the Actions cron must fire at least this often for the gate to matter.
DEFAULT_CADENCE_HOURS = int(os.environ.get("SCOUT_DEFAULT_CADENCE_HOURS", "24"))

# When triage flags a SUBSTANTIAL development but nothing survives grounding+retry, the
# detection window is HELD OPEN (not advanced past it) so the next check re-attempts it,
# rather than losing it forever. Bounded: after this many consecutive failed attempts on the
# same window, give up (advance, surface the abandonment) so we don't re-escalate indefinitely.
MONITOR_MAX_UNRESOLVED_RETRIES = int(os.environ.get("SCOUT_MONITOR_MAX_UNRESOLVED_RETRIES", "3"))

# --- Monitoring anchors (window-anchored due-gate; the launch promise) -------
# Daily wall-clock times (UTC) at which every monitored card becomes due. The
# product promise for the launch window is PREDICTABLE freshness — a morning
# refresh and a midday refresh, the same way every day — so the gate is anchored
# to the clock, NOT to elapsed hours. A card is due when it hasn't been checked
# since the most recent anchor that has already passed; that makes checks land in
# the morning + midday windows and NEVER drift (a relative last_checked+cadence
# gate slides later on every check — the bug that scattered updates across random
# times). The Actions cron BURSTS across each window so a late or dropped GitHub
# fire can't blow the slot; the anchor gate still permits only one check/window.
#
# Launch window = 7am + 1pm US Eastern. June is EDT (UTC-4) → "11:00,17:00".
# DST: on 2026-11-01 the US falls back to EST (UTC-5) → change to "12:00,18:00".
# To pare down later, drop an anchor (e.g. just "11:00" for once-daily).
# Set empty ("") to fall back to the legacy per-card cadence_hours relative gate.
MONITOR_ANCHORS_UTC = [
    a.strip() for a in os.environ.get("SCOUT_MONITOR_ANCHORS_UTC", "11:00,17:00").split(",") if a.strip()
]


# --- Email alerts (§5) -------------------------------------------------------
# Deterministic side-effect in code, NOT an agent tool (control line). Sending is a no-op unless
# email is configured, so testing/dev can never email anyone. Two backends, tried in order by
# notify._dispatch: (1) GMAIL SMTP — the owner's own Google account via an app password, no third-
# party service; (2) RESEND — a transactional API. Owner alerts (digests + propagation proposals)
# go to ALERT_EMAIL_TO; Gmail is preferred when its creds are present.
GMAIL_USER = os.environ.get("SCOUT_GMAIL_USER")              # the sending Gmail address (also the login)
GMAIL_APP_PASSWORD = os.environ.get("SCOUT_GMAIL_APP_PASSWORD")  # a Google App Password (not the account pw)
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
ALERT_EMAIL_TO = os.environ.get("SCOUT_ALERT_TO")
ALERT_EMAIL_FROM = os.environ.get("SCOUT_ALERT_FROM", "Scout <onboarding@resend.dev>")


# --- Self-serve generation (launch window; Parts 2/3) ------------------------
# Async, gated, git-as-store. The DEPLOYED app captures a request and the SDK pipeline
# runs out-of-band in a GitHub Action (scout/selfserve.py explains the topology).
#
# Two INDEPENDENT gates: a launch window (first N free) and a hard dollar ceiling, so a
# cost spike can't blow past the budget even if the counter still shows room.
SELFSERVE_FREE_LIMIT = int(os.environ.get("SCOUT_SELFSERVE_FREE_LIMIT", "10"))
SELFSERVE_SPEND_CEILING_USD = float(os.environ.get("SCOUT_SELFSERVE_SPEND_CEILING_USD", "100"))
# Where "DM me for access" should point once the window closes (shown by the app).
SELFSERVE_CONTACT = os.environ.get("SCOUT_SELFSERVE_CONTACT", "https://www.linkedin.com/in/urospajic")
# Optional "email me when it's ready" on the self-serve form. OFF by default so the form NEVER
# promises a notification the backend can't deliver. Turn on (SCOUT_SELFSERVE_EMAIL=1, app-side)
# ONLY after RESEND_API_KEY is configured in the self-serve ACTION's secrets — the Action is what
# actually sends, since the user may have closed the tab. App-side this flag only decides whether
# to SHOW the optional email field; the Action sends iff RESEND_API_KEY + a recipient are present.
SELFSERVE_EMAIL_ENABLED = os.environ.get("SCOUT_SELFSERVE_EMAIL", "") == "1"
# Public base URL of the deployed viewer, used to build the result link in the "ready" email.
SELFSERVE_APP_URL = os.environ.get("SCOUT_SELFSERVE_APP_URL", "https://agent-scout.streamlit.app")

# --- Author / credit ---------------------------------------------------------
# Shown in the app footer and used as the self-serve "get in touch" link. When
# AUTHOR_LINKEDIN is set, the app renders "DM me on LinkedIn" → this URL; otherwise
# it falls back to the SELFSERVE_CONTACT email. Set via Streamlit secrets or .env.
AUTHOR_NAME = os.environ.get("SCOUT_AUTHOR_NAME", "Urosh Pajic")
AUTHOR_LINKEDIN = os.environ.get("SCOUT_AUTHOR_LINKEDIN", "https://www.linkedin.com/in/urospajic")
# Storage backend: when a token + repo are set (deployed app), the app reads/writes via the
# GitHub API on this branch; otherwise it falls back to the local filesystem (dev/test).
#
# PRIVACY: user requests + generated cards + the spend ledger are USER DATA, so they live in a
# SEPARATE PRIVATE repo — NOT the public code repo (which would make every submission world-
# readable). SELFSERVE_REPO is that private data repo; SELFSERVE_DATA_PREFIX is where the data
# sits inside it (root by default — the data repo has no v2/ nesting). The generation Action
# lives in the public CODE repo and is triggered by an explicit workflow_dispatch the app POSTs
# (a push to the private data repo can't trigger a workflow in the code repo), so the token needs
# Contents:R/W on the DATA repo AND Actions:R/W on the DISPATCH (code) repo.
SELFSERVE_GH_TOKEN = os.environ.get("SELFSERVE_GH_TOKEN")
SELFSERVE_REPO = os.environ.get("SELFSERVE_REPO")              # PRIVATE data repo, e.g. "uroshp/scout-user-data"
SELFSERVE_BRANCH = os.environ.get("SELFSERVE_BRANCH", "main")
SELFSERVE_DATA_PREFIX = os.environ.get("SCOUT_SELFSERVE_DATA_PREFIX", "")  # path prefix in the data repo (root)
# The public code repo whose selfserve workflow the app dispatches when a request is submitted.
SELFSERVE_DISPATCH_REPO = os.environ.get("SCOUT_SELFSERVE_DISPATCH_REPO", "uroshp/scout-ci")
SELFSERVE_DISPATCH_WORKFLOW = os.environ.get("SCOUT_SELFSERVE_DISPATCH_WORKFLOW", "selfserve.yml")


def require_api_key() -> str:
    """Return the Anthropic API key or raise with a clear message.

    The Agent SDK runs on an API key (not a claude.ai login) and bills as API
    usage — confirm the billing path before scheduling frequent runs (§10).
    """
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add your key."
        )
    return key
