"""Central config for Scout v2.

Model IDs and guard limits are read from the environment with verified defaults,
so the code survives model renames without edits (v2-agent-spec.md §3, §10).
Never hardcode a model ID elsewhere — import from here.
"""
import os

from dotenv import load_dotenv

load_dotenv()

# --- Models (defaults verified against Anthropic docs, June 2026) -------------
# Orchestrator: judgment — planning, materiality, synthesis, consistency.
ORCHESTRATOR_MODEL = os.environ.get("ORCHESTRATOR_MODEL", "claude-opus-4-8")
# Subagents: legwork — research + verification, parallelized.
SUBAGENT_MODEL = os.environ.get("SUBAGENT_MODEL", "claude-sonnet-4-6")
# Triage gate: cheap "is there anything here at all?" check (§6).
FAST_MODEL = os.environ.get("FAST_MODEL", "claude-sonnet-4-6")

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

# --- Grounding (claim-object.md §4) -------------------------------------------
# Provisional fuzzy threshold (0..1) — to be TUNED from real fetched pages at the
# #7 measurement step, watching the 0.80-0.92 band for true claims being cut.
GROUNDING_FUZZY_THRESHOLD = float(os.environ.get("SCOUT_GROUNDING_FUZZY", "0.92"))
GROUNDING_TIMEOUT_S = float(os.environ.get("SCOUT_GROUNDING_TIMEOUT_S", "20"))
# Contact string for the grounding fetcher's descriptive User-Agent. SEC's
# fair-access policy 403s requests without a declared contact, so a real one is
# needed to ground filings. Defaults to the repo author's email; override in .env.
GROUNDING_CONTACT = os.environ.get("SCOUT_GROUNDING_CONTACT", "urospajic@gmail.com")


# --- Monitoring cadence (per-competitor; A1) ---------------------------------
# Default hours between checks for a battlecard with no explicit cadence_hours in
# its meta.json. The launch-window showcase cards override this to 6 (4x/day);
# dialed back after week one. run_all() honors per-card cadence via a due-gate;
# the Actions cron must fire at least this often for the gate to matter.
DEFAULT_CADENCE_HOURS = int(os.environ.get("SCOUT_DEFAULT_CADENCE_HOURS", "24"))


# --- Email alerts (transactional API; §5) ------------------------------------
# Deterministic side-effect in code, NOT an agent tool (control line). Sending is a
# no-op unless ALL of these are set, so testing/dev can never email anyone.
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
ALERT_EMAIL_TO = os.environ.get("SCOUT_ALERT_TO")
ALERT_EMAIL_FROM = os.environ.get("SCOUT_ALERT_FROM", "Scout <onboarding@resend.dev>")


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
