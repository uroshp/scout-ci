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

# --- Models (2026-10-03: moved to the newest version of each tier; Uroš: "no need to run anything
# with old models". Eval periods are keyed on the model per role, so the switch opens new periods
# by construction. The CHALLENGER pin below is the one deliberate exception: it is the measuring
# stick and must not move with the thing it measures.) --------------------------------------------
# Orchestrator: judgment — routing, materiality, the judge, the election. Opus 5.5 ($4/$20): 20% below Opus 4.8.
ORCHESTRATOR_MODEL = os.environ.get("ORCHESTRATOR_MODEL", "claude-opus-5-5")
# Subagents: legwork — research, authoring, quick answers. Sonnet 5.5: Sonnet 5's price, faster.
SUBAGENT_MODEL = os.environ.get("SUBAGENT_MODEL", "claude-sonnet-5-5")
# Triage gate: cheap "is there anything here at all?" check (§6). Runs on EVERY check, so
# it uses Haiku — the cheapest model — to keep the no-news floor low (lever A).
FAST_MODEL = os.environ.get("FAST_MODEL", "claude-haiku-4-5-20251001")
# LOCKSTEP (Uroš, 2026-10-03: "latest models and latest SDK, updated in lockstep"): the models run
# through the Claude Code binary the Agent SDK bundles, and a new model id can need a newer binary
# (Opus 5.5 needs 2.1.280+; the pinned SDK of 10/3 bundled 2.1.179 and every Opus call failed with
# a 400 until the first rehearsal caught it). When a model id above changes: bump `claude-agent-sdk`
# in requirements.txt AND requirements-engine.txt to the latest release, raise MODEL_MIN_CLI to the
# version the new model needs, and re-pip every environment that runs the SDK (the Actions runners
# rebuild from the pins; the mini's venv and the engine image must be rebuilt). tests/
# test_models_lockstep.py asserts the pins agree and the installed binary is new enough;
# monitor.preflight refuses a run on a binary that is too old, before any spend.
MODEL_MIN_CLI = "2.1.280"

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
# 2026-10-03 (25 captured materiality + own-company runs: median 9-10 turns, max 16): 20 truncates no
# successful run and stops a runaway before its dollar budget does (the Notion arm burned $1.50 for
# nothing on 10/3). Lowered from 40.
MAX_TURNS = int(os.environ.get("SCOUT_MAX_TURNS", "20"))
# Tool surface (2026-10-03): under the SDK's bypassPermissions mode `allowed_tools` is a name only;
# `disallowed_tools` is the block. Captured monitor calls showed the own-company step running Bash
# (including grep over the card store) and a rewrite reading CLAUDE.md on the Actions runner. Every
# monitor-side role disallows the shell, file and subagent tools; search, the fetch tool and the
# structured-source tools stay. No monitor role uses subagents (119 captured calls). The eval
# fingerprint hashes the system prompt only, so this moves no eval period.
MODEL_DISALLOWED_TOOLS = ["WebFetch", "Bash", "Read", "Write", "Edit", "NotebookEdit",
                         "Glob", "Grep", "Agent", "Task", "TodoWrite"]
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
# CATCH-UP (2026-10-03): a stale detection window (a promoted card, a skipped stretch, a held window)
# earns more searches, scaled to its length, up to this cap. The first check on the OpenAI vs
# Anthropic collaboration card squeezed 19 days with DevDay in them through the daily five searches
# and surfaced three candidates. Windows of up to 3 days keep the daily cap.
TRIAGE_CATCHUP_MAX_SEARCHES = int(os.environ.get("SCOUT_TRIAGE_CATCHUP_MAX_SEARCHES", "12"))
TRIAGE_CATCHUP_BUDGET_USD = float(os.environ.get("SCOUT_TRIAGE_CATCHUP_BUDGET_USD", "1.00"))
# FOCUS (2026-10-03, Uroš): a card with a focus has TWO scopes, both material: the focus area (first)
# and the companies' corporate developments. The daily check never received the focus before this
# date, so its searches drifted to corporate news on every focused card. A focused card gets these
# extra searches, reserved for the focus area, so a busy corporate week can never crowd it out.
TRIAGE_FOCUS_SEARCHES = int(os.environ.get("SCOUT_TRIAGE_FOCUS_SEARCHES", "3"))
# Surface router (propagation step 3a, scout/route.py): tools-off Opus deciding which brief surfaces
# an act-grade change reshapes, across all sections. One judgment call per change-set; it reasons over
# the facts + the card, never searches. Absorbs the old separate strategic-lead pass, so it is
# cost-neutral against it (Opus, similar budget). Kept a touch higher than propose (a full-card read).
ROUTE_MAX_TURNS = int(os.environ.get("SCOUT_ROUTE_MAX_TURNS", "6"))
ROUTE_MAX_BUDGET_USD = float(os.environ.get("SCOUT_ROUTE_MAX_BUDGET_USD", "0.90"))
# Propose/author pass (propagation step 3b): tools-off Sonnet drafting prose for the router's ops over
# already-grounded facts, so it is short and cheap. Tight caps keep it bounded; never searches/fetches.
PROPOSE_MAX_TURNS = int(os.environ.get("SCOUT_PROPOSE_MAX_TURNS", "6"))
PROPOSE_MAX_BUDGET_USD = float(os.environ.get("SCOUT_PROPOSE_MAX_BUDGET_USD", "0.60"))
# Judge pass (propagation step 4): tools-off Opus adversarially confirming/rejecting the proposer's
# ops against the same grounded facts. Also short — it reasons, never searches — but on the pricier
# model, so it keeps its own (slightly higher) ceiling than propose.
JUDGE_MAX_TURNS = int(os.environ.get("SCOUT_JUDGE_MAX_TURNS", "6"))
JUDGE_MAX_BUDGET_USD = float(os.environ.get("SCOUT_JUDGE_MAX_BUDGET_USD", "0.75"))
# Judge fallback (2026-07-01 Opus-4.8 outage): when the primary judge returns unparseable output
# twice, retry ONCE on this model — a DIFFERENT family/tier than ORCHESTRATOR_MODEL so one provider
# incident can't take out both. Deliberately NOT CHALLENGER_MODEL (that pin serves the shadow eval
# and must not move with ops concerns). Empty string disables the fallback. A fallback verdict
# gates the proposals EMAIL only — it never auto-applies to a card and never scores the Opus
# judge's promotion gate (adjudicate excludes it).
JUDGE_FALLBACK_MODEL = os.environ.get("SCOUT_JUDGE_FALLBACK_MODEL", "claude-sonnet-5-5")
# Rewrite loop (2026-07-01 Sonnet-5 silent drop; materiality-first cure 2026-07-31): a judge-rejected
# op the judge deems MATERIAL (its point would move a deal) gets up to N guided cure rounds — the
# judge's reason fed back — then a blind re-judge each round. A `cure:"prose"` reject fixes wording;
# a `cure:"root"` reject re-approaches a material point whose pivot/mechanism was wrong (the 7/31
# containment case). Exhausted (or `cure:"none"`) -> an URGENT separate email so a deal-moving
# point is NEVER silently dropped. 0 disables the loop (restores drop-on-reject). Reuses the
# PROPOSE/JUDGE caps per call.
# 2026-10-03 (owner's cost read): the rewrite is authored on the AUTHORING tier and capped at TWO
# attempts. The decision logs over 74 days: 231 ops passed first time, 66 needed one rewrite (63
# passed), 14 took three attempts (13 passed), 4 were exhausted; the 10/3 Teams card spent ~$3 on
# three Opus rewrites that all failed. The judge (Opus) still rules on every attempt; a third
# failure reaches the owner as AUTHORING FAILED instead of buying a third attempt.
PROPAGATE_MAX_REWRITES = int(os.environ.get("SCOUT_PROPAGATE_MAX_REWRITES", "2"))
PROPAGATE_REWRITE_MODEL = os.environ.get("SCOUT_PROPAGATE_REWRITE_MODEL", SUBAGENT_MODEL)
# Urgent-material alert (2026-07-31): when a material op can't be cured (exhausted the rewrites, or
# the judge ruled cure:"none"), send a SEPARATE urgent email (distinct from the proposals email's
# AUTHORING-FAILED section) so the owner sees a deal-moving point that went undrafted. review/live
# only (via the monitor gate). Empty/0/false disables.
PROPAGATE_URGENT_EMAIL = os.environ.get("SCOUT_PROPAGATE_URGENT_EMAIL", "1").strip().lower() not in ("0", "false", "off", "")
# Length-cure loop (2026-07-25, the 182-word hold): a judge-CONFIRMED op over the deterministic
# 170-word render cap gets its OWN bounded condense -> blind-re-judge budget, separate from
# PROPAGATE_MAX_REWRITES, so a content rewrite that consumed the rewrite budget (exactly the 7/25
# case) can't starve the cure. Uncured -> the op is RESTORED to its confirmed state and the render
# gate condenses-or-holds it (never an exhausted reject). 0 disables (hold-at-gate behavior).
PROPAGATE_MAX_LENGTH_CURES = int(os.environ.get("SCOUT_PROPAGATE_MAX_LENGTH_CURES", "1"))
# Supersede-retire sweep (2026-07-25, owner's no-stale-state philosophy): when a routed fact names
# superseded identifiers (a new flagship, a replaced product), code sweeps active claims still
# citing them into judge-decided retire candidates (deal-moving lens; lineage kept). Review/live
# only. The hunt window keeps triage actively searching for the REPLACEMENT value (e.g. the new
# model's benchmark) for N days after a supersede-retire, so the fresh claim arrives via the
# normal grounded path.
SUPERSEDE_SWEEP = os.environ.get("SCOUT_SUPERSEDE_SWEEP", "1").strip().lower() not in ("0", "false", "off", "")
SUPERSEDE_HUNT_DAYS = int(os.environ.get("SCOUT_SUPERSEDE_HUNT_DAYS", "14"))
# Lead election (2026-08-08, owner's "biggest deal impact, someone decides + enforces" bar): the
# viewer's "Today's angle" is the executive_summary claim at order 0, historically FROZEN at
# generation (new verdicts append to the bottom, revises keep their order). When a run produces or
# revises an exec-summary verdict, a model judgment decides whether that fresh verdict is MATERIALLY
# more deal-moving than the current lead and, if so, promotes it to order 0. Deal-impact is judgment
# (model); recency is the trigger (news-driven). AUTO-APPLIES with hysteresis — no approval gate —
# but every election is recorded + surfaced (feed + email FYI + morning tool) and reversible (order
# is data; a per-card pin freezes the angle). Review/live only; shadow never applies. Empty/0/false
# disables. The bar (this IS the enforcement, since there is no human gate at apply):
#   - margin decisive|clear promotes; marginal|none HOLDS the incumbent (stability default).
#   - within LEAD_COOLDOWN_DAYS of the last promotion, ONLY a 'decisive' challenger may displace.
#   - any parse error / no eligible challenger / pinned card -> HOLD (fail-safe = no change).
LEAD_ELECTION = os.environ.get("SCOUT_LEAD_ELECTION", "1").strip().lower() not in ("0", "false", "off", "")
LEAD_COOLDOWN_DAYS = int(os.environ.get("SCOUT_LEAD_COOLDOWN_DAYS", "14"))
LEAD_RECENCY_DAYS = int(os.environ.get("SCOUT_LEAD_RECENCY_DAYS", "14"))
# Sonnet, NOT Opus (2026-08-09 cost pass): the election fires on every run that confirms an
# exec-summary revise (frequent, not rare), so an Opus call each time is a real recurring bill. The
# election is a bounded COMPARATIVE judgment (which of a few verdicts opens the deal best) with a
# strict margin bar + hysteresis + a human pin/override backstop — well within Sonnet 5's range.
# Override to ORCHESTRATOR_MODEL if the deal-impact calls start looking wrong.
LEAD_ELECTION_MODEL = os.environ.get("SCOUT_LEAD_ELECTION_MODEL", SUBAGENT_MODEL)
# my_company fact-sourcing budget (2026-07-02 cost pass): the own-side arm runs on SUBAGENT_MODEL
# (sourcing work — search/fetch/excerpts; judgment stays with the Opus router/judge downstream and
# grounding verification is deterministic), with its own cap instead of riding the $3 materiality one.
MY_FACTS_MAX_BUDGET_USD = float(os.environ.get("SCOUT_MY_FACTS_MAX_BUDGET_USD", "1.50"))

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
# PINNED to Sonnet 4.6, NOT SUBAGENT_MODEL: the challenger is mid-evaluation (v3.5 longitudinal study),
# so its model must not move — changing it would break the running comparison against the accumulated
# data. Decoupled from SUBAGENT_MODEL on 2026-06-30 when that moved to Sonnet 5. Revisit once the
# current eval concludes.
CHALLENGER_MODEL = os.environ.get("CHALLENGER_MODEL", "claude-sonnet-4-6")
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

# Strategic-impact pass (strategy layer, scout/strategy.py): on an ACT-grade material change, re-pick
# the brief's single most strategic lead (impact x freshness, stress-tested) and email a proposal if
# it shifts. Reuses the Opus judge model; fires ONLY on ACT alerts when PROPAGATE_MODE is review/live,
# so quiet runs cost nothing. On by default; set SCOUT_STRATEGIC_PASS=0 to disable.
STRATEGIC_PASS = os.environ.get("SCOUT_STRATEGIC_PASS", "1").strip().lower() not in ("0", "false", "off", "")

# Consequentiality filter (the router's run_verdict; docs/consequential-filter-spec.md): on an
# ACT-grade change, the router also judges whether the change-set is CONSEQUENTIAL (changes the
# rep's play / thesis -> earns the expensive authoring stages) or routine (a fact patch that keeps
# the card current without changing the argument). SHADOW-FIRST:
#   "shadow" (default) -> run + LOG the verdict, change NOTHING (validate before trusting);
#   "gate"             -> BUILT 2026-07-02 (propagate.py, right after route()): an explicit
#                         routine verdict defers the author/judge/rewrite stages — ops recorded as
#                         "gated_routine" in the decision log + a digest audit line; fail-OPEN on a
#                         missing verdict. The production flip is ONE env line in monitor.yml
#                         (SCOUT_CONSEQUENTIAL_FILTER: "gate"), after the shadow spot-checks clean;
#   "off"              -> don't capture the verdict at all.
CONSEQUENTIAL_FILTER = os.environ.get("SCOUT_CONSEQUENTIAL_FILTER", "shadow").strip().lower()
# Conseq. track spot-check: how many shadow verdicts to accumulate before the monitor emails a
# one-time "ready to review" digest (then you spot-check consequential-vs-routine and decide gate).
CONSEQ_REVIEW_MIN = int(os.environ.get("SCOUT_CONSEQ_REVIEW_MIN", "8"))

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
# sec.gov's fair-access rule wants "Company Name email@domain" in the User-Agent and 403s anything
# else (2026-09-28: the URL-style contact UA above was refused by www.sec.gov, which is why the
# generation prompt warned against anchoring on SEC.gov). Sent to sec.gov hosts only.
SEC_CONTACT = os.environ.get("SCOUT_SEC_CONTACT", "Agent Scout admin scout@agent-scout.ai")


# --- Shadow-mode eval (v3.5 challenger qualification; docs/vnext-roadmap.md) --
# PURE OBSERVER, OFF by default. When "1", REAL generation/monitor runs record the CHAMPION
# decisions (the deterministic code grader + the verifier cut log) to shadow/<slug>/ in the
# PRIVATE data store, so an OFFLINE challenger model-judge can be scored against them later.
# It triggers no model call and never alters or gates a production run (scout/shadow.py).
SHADOW_EVAL_ENABLED = os.environ.get("SCOUT_SHADOW_EVAL", "") == "1"
# Call capture for the on-device model comparison (2026-09-28): records every paid call's exact
# inputs + output, one bundle per run, to the PRIVATE store (scout/calllog.py). Independent of
# SCOUT_SHADOW_EVAL; off = every hook is a no-op and the live path is byte-identical.
CALL_CAPTURE_ENABLED = os.environ.get("SCOUT_CALL_CAPTURE", "") == "1"


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
# times). The trigger is a Cloud Scheduler workflow_dispatch (no GitHub `schedule:`
# cron as of 2026-07-01); the anchor gate still permits only one check/window, so
# any extra manual dispatch in a served window is a cheap no-op.
#
# Daily run window = 7am US Eastern. June is EDT (UTC-4) → "11:00". (Was twice daily
# "11:00,17:00"; the 1pm pass was dropped 2026-06-28 — across weeks of history the 2nd run mostly
# repeated the 1st, and being retrieval-variance it sometimes REGRESSED it; since review shows only
# the latest run, a worse 2nd run could hide the 1st's catch.) DST: on 2026-11-01 the US falls back
# to EST (UTC-5) → change to "12:00". Set empty ("") to fall back to the legacy cadence_hours gate.
MONITOR_ANCHORS_UTC = [
    a.strip() for a in os.environ.get("SCOUT_MONITOR_ANCHORS_UTC", "11:00").split(",") if a.strip()
]

# Weekday skip (Mon=0 .. Sun=6): days the monitor does NOT run — no rep follows a battlecard on the
# weekend. Default Sunday (6). The next run picks up everything since last_checked, so a skipped day
# loses no COVERAGE, only timing (Monday catches Sat+Sun; Saturday catches Friday). The 11:00 UTC
# anchor is still the same weekday in ET/PT, so a UTC weekday check matches the rep's local day.
# Set "" to run every day. `force=True` (manual runs) bypasses this.
MONITOR_SKIP_WEEKDAYS = {
    int(d) for d in os.environ.get("SCOUT_MONITOR_SKIP_WEEKDAYS", "6").split(",") if d.strip()
}


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
# A FAILED generation still bills (subagents already searched before the crash) but produces no
# ResultMessage, so its exact cost is unknowable in-run. Count this ESTIMATE against the ceiling
# instead of $0 (the 7/18 incident: a ~$10 failure was invisible to the ledger). Default = the
# measured mean of a real July run ($13.55, 2026-07-19); explicitly an estimate, tune as measured.
SELFSERVE_FAILED_RUN_SPEND_EST = float(os.environ.get("SCOUT_FAILED_RUN_SPEND_EST", "14"))
# Where "DM me for access" should point once the window closes (shown by the app).
SELFSERVE_CONTACT = os.environ.get("SCOUT_SELFSERVE_CONTACT", "https://www.linkedin.com/in/urospajic")
# Optional "email me when it's ready" on the self-serve form. OFF by default so the form NEVER
# promises a notification the backend can't deliver. Turn on (SCOUT_SELFSERVE_EMAIL=1, app-side)
# ONLY after RESEND_API_KEY is configured in the self-serve ACTION's secrets — the Action is what
# actually sends, since the user may have closed the tab. App-side this flag only decides whether
# to SHOW the optional email field; the Action sends iff RESEND_API_KEY + a recipient are present.
SELFSERVE_EMAIL_ENABLED = os.environ.get("SCOUT_SELFSERVE_EMAIL", "") == "1"
# Public base URL of the deployed viewer, used to build the result link in the "ready" email.
SELFSERVE_APP_URL = os.environ.get("SCOUT_SELFSERVE_APP_URL", "https://agent-scout.ai")

# --- Analytics gate ----------------------------------------------------------
# Hostnames where the CLIENT GA tag may fire (comma-separated env SCOUT_ANALYTICS_HOSTNAMES).
# Its OWN config on purpose (2026-07-08 incident): the gtag guard used to derive its hostname
# from SELFSERVE_APP_URL — a LINK-base setting — so repointing links silently disarmed the tag
# on whichever surface relied on the default. Both prod surfaces are pinned here; dev/preview
# hosts (localhost, Codespaces) stay excluded exactly as before.
ANALYTICS_HOSTNAMES = tuple(
    h.strip() for h in os.environ.get(
        "SCOUT_ANALYTICS_HOSTNAMES",
        "agent-scout.ai,agent-scout.streamlit.app").split(",") if h.strip())
# Master switch for BOTH the client tag and the server-side visit event (SCOUT_ANALYTICS=0 on the
# RC service, 2026-09-28): the hostname allow-list already excludes an RC host from the client tag,
# but the server-side Measurement Protocol event fires from any host, so RC needs an explicit off.
ANALYTICS_ENABLED = os.environ.get("SCOUT_ANALYTICS", "1") != "0"

# --- Structured sources (WS1, 2026-09-28) ---------------------------------------
# SCOUT_SOURCES_TOOLS=1 adds the `scoutsources` MCP server (SEC EDGAR filings + XBRL facts, public
# job boards, Wayback page history; scout/sources/) to the tools-on model calls. OFF by default on
# main: the live prompts stay byte-identical until the flip, which is a new eval period for the
# tools-on roles (the on-device lane mirrors the same tools in scout/localagent.py).
SOURCES_TOOLS_ENABLED = os.environ.get("SCOUT_SOURCES_TOOLS", "") == "1"

# --- Ask Scout (WS2, 2026-09-28) -----------------------------------------------
# Per-call caps are the Agent SDK's native max_budget_usd; ASK_MAX_USD is the ceiling one question
# may reach in total (research + verify + one rewrite + a second verify). Measured target ~$1-1.75.
# 2026-09-28: a broad question ("how do Slack agents from Claude and ChatGPT compare?") blew the
# $1.00 research cap on search results alone (16 messages, 235k tokens), so the cap is $1.50 and the
# contract limits the tool budget (4 searches, 3 page reads); ASK_MAX_USD bounds one question end to end.
ASK_RESEARCH_BUDGET_USD = float(os.environ.get("SCOUT_ASK_RESEARCH_BUDGET_USD", "1.50"))
ASK_VERIFY_BUDGET_USD = float(os.environ.get("SCOUT_ASK_VERIFY_BUDGET_USD", "0.75"))
ASK_REWRITE_BUDGET_USD = float(os.environ.get("SCOUT_ASK_REWRITE_BUDGET_USD", "0.50"))
ASK_MAX_USD = float(os.environ.get("SCOUT_ASK_MAX_USD", "3.00"))
# The QUICK path (2026-09-28): no tools, answers from what Scout already verified, the same Opus
# verifier, rejects cut. One draft call + one judge call; the ledger reserves ASK_QUICK_MAX_USD.
# Measured 2026-09-28: the draft's prompt is ~35k tokens (119 facts + 33 takes for OpenAI +
# Anthropic) and the SDK bills it across two messages plus the cache write, so $0.40 was too low.
ASK_QUICK_DRAFT_BUDGET_USD = float(os.environ.get("SCOUT_ASK_QUICK_DRAFT_BUDGET_USD", "1.00"))
ASK_QUICK_MAX_USD = float(os.environ.get("SCOUT_ASK_QUICK_MAX_USD", "1.50"))
# Viewer-side soft limits (scout/ratelimit.py): per visitor id per day, per client IP per minute;
# the engine's ledger is the hard bound. RC sets the quota high for review; a comma list of visitor
# ids (the scout_cid cookie) is exempt, for the owner.
# --- Signals (WS3, 2026-09-29): event-driven monitoring. A new SEC filing on a watched company
# dispatches that card's check within the hour; a brand-new hiring department is queued for the
# next scheduled run; routine hiring deltas are CONTEXT for materiality and the router, never a
# trigger (Uroš: "hiring is a footnote"). Off by default on main.
SIGNALS_ENABLED = os.environ.get("SCOUT_SIGNALS", "0") == "1"
SIGNAL_FORMS = tuple(x.strip() for x in os.environ.get("SCOUT_SIGNAL_FORMS", "8-K,10-Q,10-K,D").split(",") if x.strip())
SIGNAL_NEW_DEPT_MIN_ROLES = int(os.environ.get("SCOUT_SIGNAL_NEW_DEPT_MIN_ROLES", "3"))
SIGNAL_NEW_DEPT_LOOKBACK_DAYS = int(os.environ.get("SCOUT_SIGNAL_NEW_DEPT_LOOKBACK_DAYS", "90"))
SIGNAL_MAX_DISPATCHES_PER_DAY = int(os.environ.get("SCOUT_SIGNAL_MAX_DISPATCHES_PER_DAY", "2"))
SIGNAL_MIN_HOURS_SINCE_CHECK = float(os.environ.get("SCOUT_SIGNAL_MIN_HOURS_SINCE_CHECK", "6"))
MONITOR_DISPATCH_WORKFLOW = os.environ.get("SCOUT_MONITOR_DISPATCH_WORKFLOW", "monitor.yml")
MONITOR_SIGNAL_GAP_H = float(os.environ.get("SCOUT_MONITOR_SIGNAL_GAP_H", "18"))   # a signal run inside this gap serves the next anchor
ASK_VISITOR_QUOTA = int(os.environ.get("SCOUT_ASK_VISITOR_QUOTA", "6"))
ASK_IP_PER_MIN = int(os.environ.get("SCOUT_ASK_IP_PER_MIN", "6"))
ASK_QUOTA_BYPASS_CIDS = [c.strip() for c in os.environ.get("SCOUT_ASK_QUOTA_BYPASS_CIDS", "").split(",") if c.strip()]
REQUEST_IP_PER_MIN = int(os.environ.get("SCOUT_REQUEST_IP_PER_MIN", "3"))
REQUEST_IP_PER_DAY = int(os.environ.get("SCOUT_REQUEST_IP_PER_DAY", "10"))
# The in-page Ask panel renders only when SCOUT_ASK=1 (production stays byte-identical until the
# flip). SCOUT_ASK_CANNED=<answer id> puts the panel in review mode: every question replays that
# stored answer with realistic stage timing, $0 (RC only). SCOUT_ASK_ENGINE_URL wires the engine.
ASK_ENABLED = os.environ.get("SCOUT_ASK", "") == "1"
ASK_CANNED_ID = os.environ.get("SCOUT_ASK_CANNED", "")
ASK_ENGINE_URL = os.environ.get("SCOUT_ASK_ENGINE_URL", "")
ASK_VIEWER_SECRET = os.environ.get("ASK_VIEWER_SECRET", "")     # shared with the engine; mints page tokens

# --- RC environment (2026-09-28) ---------------------------------------------
# A second deployment of the SAME code (branch `rc` -> service agent-scout-rc) where every new
# screen is reviewed before it reaches agent-scout.ai. Production is never the test surface.
# SCOUT_RC=1 turns on the visible ribbon and the robots Disallow; SCOUT_RC_PASSWORD gates every
# page behind a cookie (the v1 APP_PASSWORD idea, cookie-based so the iPad works); both are unset
# in production, so the code is inert there.
RC_MODE = os.environ.get("SCOUT_RC", "") == "1"
RC_PASSWORD = os.environ.get("SCOUT_RC_PASSWORD", "")

# --- Author / credit ---------------------------------------------------------
# Shown in the app footer and used as the self-serve "get in touch" link. When
# AUTHOR_LINKEDIN is set, the app renders "DM me on LinkedIn" → this URL; otherwise
# it falls back to the SELFSERVE_CONTACT email. Set via Streamlit secrets or .env.
AUTHOR_NAME = os.environ.get("SCOUT_AUTHOR_NAME", "Urosh Pajic")
AUTHOR_LINKEDIN = os.environ.get("SCOUT_AUTHOR_LINKEDIN", "https://www.linkedin.com/in/urospajic")
# Public code repo, linked from the footer credit (alongside LinkedIn) so visitors can see the build.
SOURCE_REPO_URL = os.environ.get("SCOUT_SOURCE_REPO_URL", "https://github.com/uroshp/scout-ci")
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
# RC isolation (2026-09-28): with a prefix set (rc/), every WRITE lands under it; with the read
# fallback on, a READ that finds nothing under the prefix falls through to the unprefixed
# production path, so RC pages see production's decision logs / ledgers while RC can never write
# to them. Writers never use the fallback (a fallback sha would target the wrong file).
SELFSERVE_DATA_READ_FALLBACK = os.environ.get("SCOUT_SELFSERVE_DATA_READ_FALLBACK", "") == "1"
# The public code repo whose selfserve workflow the app dispatches when a request is submitted.
SELFSERVE_DISPATCH_REPO = os.environ.get("SCOUT_SELFSERVE_DISPATCH_REPO", "uroshp/scout-ci")
SELFSERVE_DISPATCH_WORKFLOW = os.environ.get("SCOUT_SELFSERVE_DISPATCH_WORKFLOW", "selfserve.yml")

# Sensors (Release 2, 2026-10-04): code reads every registered source about each company every
# morning; a model (the screen) reads what is new. SCOUT_SENSORS is a repo variable like SCOUT_SIGNALS:
#   off     today's behaviour, byte for byte
#   shadow  the pass and the screen run next to the unchanged triage; a compare record per card per
#           run says whether a finding was behind every alert that landed; the FYI shows the streak
#   gate    the screen's candidates ARE the candidates; triage runs only on the card's weekly sweep
#           day, on a held window, on a filing-dispatched run, or when the entity's sensors were
#           unavailable. Cutover needs SENSOR_GATE_RUNS consecutive clean shadow runs (his call).
SENSORS_MODE = (os.environ.get("SCOUT_SENSORS", "off").strip().lower() or "off")
if SENSORS_MODE not in ("off", "shadow", "gate"):
    SENSORS_MODE = "off"
SENSOR_GATE_RUNS = int(os.environ.get("SCOUT_SENSOR_GATE_RUNS", "7"))
# Screen precision sampling in SHADOW (Uroš 2026-10-07: "surfacing more is good; find everything material
# and handle it, not limit it artificially"): a few screen-only candidates per run go through the
# materiality judge, nothing lands, the verdicts measure the screen's precision. Stops on its own at
# SCREEN_SAMPLE_MAX_TOTAL judged (a week at 5 a day); 0 turns it off.
# Wall-clock deadline for ONE model call (2026-10-08: the 4 AM run hung for six hours inside a single
# call, GitHub killed the job, the morning was lost and nothing was written). The longest honest call
# on the ledger is a catch-up triage at a few minutes; twenty is room, not a cap on work.
MODEL_CALL_TIMEOUT_S = int(os.environ.get("SCOUT_MODEL_CALL_TIMEOUT_S", "1200"))
# Inactivity watchdog on the stream: no message for this long means the call is stuck (a dropped
# connection, a tool that never answers), not thinking; the call is cancelled and retried ONCE from
# a fresh process, then fails the step. Honest calls stream something every few seconds.
MODEL_CALL_IDLE_S = int(os.environ.get("SCOUT_MODEL_CALL_IDLE_S", "300"))
MODEL_CALL_STALL_RETRIES = int(os.environ.get("SCOUT_MODEL_CALL_STALL_RETRIES", "1"))
SCREEN_SAMPLE_PER_RUN = int(os.environ.get("SCOUT_SCREEN_SAMPLE_PER_RUN", "5"))
SCREEN_SAMPLE_PER_CARD = int(os.environ.get("SCOUT_SCREEN_SAMPLE_PER_CARD", "2"))
SCREEN_SAMPLE_BUDGET_USD = float(os.environ.get("SCOUT_SCREEN_SAMPLE_BUDGET_USD", "2.00"))
SCREEN_SAMPLE_MAX_TOTAL = int(os.environ.get("SCOUT_SCREEN_SAMPLE_MAX_TOTAL", "35"))
SENSOR_SWEEP = os.environ.get("SCOUT_SENSOR_SWEEP", "1") == "1"       # the weekly model-triage audit in gate mode
SENSOR_SWEEP_DAYS = int(os.environ.get("SCOUT_SENSOR_SWEEP_DAYS", "7"))
# The rendered-fetch tier (headless Chromium via Playwright) for pages the plain fetcher cannot read
# (JavaScript shells, 401/403 to a plain client). A bot wall that defeats the browser too is recorded
# as `challenge` and shown as unreadable until a browser infrastructure (TinyFish Fetch) is wired.
SENSOR_RENDERED = os.environ.get("SCOUT_SENSOR_RENDERED", "1") == "1"
SENSOR_TINYFISH_KEY = os.environ.get("SCOUT_TINYFISH_KEY", "").strip()      # reserved: the third tier, when he has a key
# Evidence in hand (Part 3 #4, gate mode): when EVERY candidate a paid step receives carries the page
# text code already read, the step searches less, so its turn cap drops to this (from MAX_TURNS=40).
# Grounding still re-fetches. Only page findings carry evidence; a run with any evidence-less
# candidate keeps the full cap. The captured calls carry `evidence_attached` so the eval lanes compare
# within a cell.
EVIDENCE_MAX_TURNS = int(os.environ.get("SCOUT_EVIDENCE_MAX_TURNS", "12"))

# Rehearsal mode (2026-10-03): the monitor's REAL write path, run on a retired card that lives in a
# store root outside the checkout (SCOUT_STORE_ROOT) with every private-store write under the
# `rehearsal/` prefix and every email subject prefixed "[rehearsal]". A rehearsal on main proves
# the exact code and workflow expressions the 4 AM run will execute, without touching a card a
# reader sees. monitor.preflight refuses the flag without the prefix and the store root.
REHEARSAL = os.environ.get("SCOUT_REHEARSAL", "") == "1"
# Run-level spend ceiling (2026-10-03): once a monitor run's running total crosses this, the
# remaining due cards get triage only; their substantial candidates hold a detection window (the
# existing mechanism, so nothing is lost) and the needs-you email names them. Live average is
# ~$8.5 a day; the default is ~1.8x that. 0 disables.
RUN_MAX_USD = float(os.environ.get("SCOUT_RUN_MAX_USD", "15"))


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

# Audience leads (Level 2, 2026-10-02): when a brief's lead changes, write Today's angle for each
# buyer present on the card (>= AUDIENCE_MIN_PLAYS tagged plays), judged like every other edit and
# stored as a tagged executive_summary claim. At most AUDIENCE_MAX_PER_CARD_RUN buyers per card per
# run, so the first fill spreads over days. Needs the pack block audience._OP_BRIEF; without it the
# step skips itself and says so. Kill: SCOUT_AUDIENCE_LEADS=0.
AUDIENCE_LEADS = os.environ.get("SCOUT_AUDIENCE_LEADS", "1") == "1"
AUDIENCE_MIN_PLAYS = int(os.environ.get("SCOUT_AUDIENCE_MIN_PLAYS", "2"))
AUDIENCE_MAX_PER_CARD_RUN = int(os.environ.get("SCOUT_AUDIENCE_MAX_PER_CARD_RUN", "2"))

# Agent Scout in Slack (V1, 2026-10-02): the site shows the "Agent Scout in Slack" item and page
# only for the live paths that are configured. Nothing on the page stands in for the product.
SLACK_INVITE_URL = os.environ.get("SCOUT_SLACK_INVITE_URL", "").strip()      # the demo workspace's shared invite
SLACK_APP_ID = os.environ.get("SCOUT_SLACK_APP_ID", "").strip()
SLACK_TEAM_ID = os.environ.get("SCOUT_SLACK_TEAM_ID", "").strip()
SLACK_INSTALL_URL = os.environ.get("SCOUT_SLACK_INSTALL_URL", "").strip()    # V2: "Add to your Slack"
SLACK_PREVIEW = os.environ.get("SCOUT_SLACK_PREVIEW", "0") == "1"           # RC ONLY: show the button and page for layout review before the workspace exists
