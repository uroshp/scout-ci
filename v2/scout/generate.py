"""Generation (v2 build step 6): brief -> tracked battlecard, via the Agent SDK.

Architecture (v2-agent-spec.md §5, and the approved decomposition):
  orchestrator (Opus) plans, delegates, synthesizes, runs the consistency sweep,
  and emits structured claim objects + a cut log. Two subagents (Sonnet) do legwork:
    - researcher: WebSearch + WebFetch, gathers candidate findings per section.
    - verifier:   WebSearch + WebFetch, re-checks each claim by reading the source,
                  applies the source hierarchy, and emits claim objects + cut entries.

The SDK runs that loop; THEN deterministic code takes over (the control line):
  derive ids -> validate -> GROUND each claim (independent fetch) -> render -> store.
Grounding, ids, rendering, and the store are code, never agent tools.

This module is the headless-worker path (SDK). v1's in-app generation stays on the
Messages-API path in research.py — the two are intentionally separate.

CLI:  python -m scout.generate "<competitor>" ["<your company>"] ["<focus>"]
"""
import asyncio
import json
import os
import re
import sys
from datetime import date

from claude_agent_sdk import AgentDefinition, ClaudeAgentOptions, query

from scout import config, shadow
from scout.prompts import SOURCE_HIERARCHY, WRITING_STYLE, load_methodology
from scout.schema import (
    SECTIONS, ZONES, claim_id, pregrounding_errors, validation_errors,
)
from scout.grounding import ground_claims, _fetch_response, _extract_text
from scout.fetch_tool import (
    FETCH_SERVER, FETCH_TOOL_NAME, FETCH_LOG, reset_log, BLUNT_CAP,
)
from scout.render import claims_to_markdown, render_cut_log, clean_output, format_report
from scout.store import make_slug, new_meta, write_baseline

# How much of a failed page's REAL (httpx) text to hand the retry agent to re-extract
# a verbatim span from. Bounds tokens; a supporting span beyond this -> the claim drops.
RETRY_PAGE_CHARS = 18000

# --- Subagents ----------------------------------------------------------------
RESEARCHER = AgentDefinition(
    description="Researches a company by searching and READING sources.",
    prompt=(
        "You are a competitive-intelligence researcher. Use WebSearch to find recent, "
        "reputable NEWS and the fetch_page tool to READ it (pass query = the specific thing "
        "you're looking for). fetch_page returns the REAL page text, so copy a short VERBATIM "
        "span from it that supports each finding, and report the exact URL you read. Do NOT use "
        "any other web-fetch tool.\n"
        "RECENCY SWEEP (do this FIRST and explicitly): the user prompt gives today's date. Search "
        "for what has happened in roughly the LAST 2-3 WEEKS — IPO/funding/filings, launches, "
        "partnership changes, pricing/limit changes, exec moves. Use date-scoped queries (e.g. "
        "'<company> news <current month year>'). If the freshest thing you find is weeks old, "
        "search harder — you are missing the story.\n"
        "HUNT ADVERSE SIGNALS on BOTH companies, not just wins: deliberately search for the bad "
        "news that moves the competitive picture — contract cancellations, customers "
        "churning/defecting, budget caps or usage limits being hit, outages, layoffs, lawsuits, "
        "lost deals, downgrades. Scan adverse developments for the COMPETITOR *and* for OUR OWN "
        "side (the company we're selling for) — e.g. a major customer dropping OUR product is a "
        "buyer objection the rep must be ready for, so surface it explicitly with its source. A "
        "researcher who returns only positive announcements, or only the competitor's bad news, "
        "has failed.\n"
        "SOURCES: anchor on reputable news outlets (Reuters, Bloomberg, The Information, CNBC, "
        "TechCrunch, major outlets) or primary documents. NEVER use Wikipedia, wikis, "
        "encyclopedias (Britannica, Fandom), or promo/SEO listicles and aggregators — they are "
        "excluded. Prefer sources a plain HTTP client can fetch over hard-paywalled ones. Return "
        "concise, sourced findings — not prose."
    ),
    tools=["WebSearch", FETCH_TOOL_NAME],
    model=config.SUBAGENT_MODEL,
)

VERIFIER = AgentDefinition(
    description="Independently fact-checks each claim like a news editor.",
    prompt=(
        "You are the verification layer — fact-check each candidate claim the way a news editor "
        "would, not by confirming a string sits on some page. For each claim, independently "
        "re-search and use fetch_page to READ the source (pass query = the claim's key fact). "
        "fetch_page returns the REAL page text. Do NOT use any other web-fetch tool.\n"
        "CHECK CURRENCY, not just support: actively search for the LATEST status and for "
        "DISCONFIRMING reports — e.g. for any 'current / flagship / exists / ongoing' claim, "
        "search '<thing> discontinued OR cancelled OR sunset OR shut down'. If recent reporting "
        "supersedes or contradicts the claim, REVISE it to the current truth or CUT it. A claim "
        "that was true months ago but is now stale must not survive.\n"
        "SOURCE DISCIPLINE: every Recent-Strategic-Moves item and every status/current-state claim "
        "must anchor on a reputable NEWS outlet (Reuters, Bloomberg, The Information, CNBC, "
        "TechCrunch, major outlet) or a primary filing. REJECT and re-source anything anchored on "
        "Wikipedia, a wiki, an encyclopedia (Britannica/Fandom), or a promo/SEO listicle or "
        "aggregator — those are excluded; find the originating reputable source or cut.\n"
        "Keep only what is verifiable, current, specific, and decision-relevant. Copy the exact "
        "VERBATIM span (from fetch_page's output) backing each kept claim. Record every cut or "
        "revision. Your support judgment is separate from the later mechanical grounding check — "
        "do your job even though grounding will re-check the excerpt."
    ),
    tools=["WebSearch", FETCH_TOOL_NAME],
    model=config.SUBAGENT_MODEL,
)

# --- The claim contract the orchestrator must emit ----------------------------
SUBJECT_KEY_GUIDE = f"""SUBJECT_KEY (stable identity — read carefully, monitoring depends on it):
Every claim has a `subject_key`: a canonical, value-INDEPENDENT description of WHAT THE
CLAIM IS ABOUT, in the form `entity | attribute | qualifier`. It must be reproducible:
the same fact must get the same subject_key on every run, so a later run can update it.

- Use this controlled attribute vocabulary where it fits (extend only when needed, in the
  same lowercase-hyphen style): fy-revenue, q-revenue, revenue-run-rate, net-income,
  operating-income, valuation, funding-total, latest-funding-round, market-share, headcount,
  ceo, cto, key-hire, key-departure, flagship-model, flagship-product, list-price,
  pricing-model, launch, partnership, acquisition, security-incident, legal-action,
  positioning, differentiator, integration, certification.
- Qualifier decides update-vs-new: use `current`/`latest` for the present holder of a role,
  price, or flagship (so a change UPDATES in place — e.g. `aws | ceo | current`). Use a fixed
  period only when a new period is genuinely a new fact (e.g. `aws | fy-revenue | 2025`).
- NEVER put the value in the subject_key: `aws | fy-revenue | 2025`, never `...| 128.7b`.
- subject_key must be UNIQUE within this brief — it is the dedup key. Two different claims
  must never share one. If a subject belongs in two sections, it is ONE claim, rendered once.
"""

CLAIM_CONTRACT = f"""Emit each claim as a JSON object with EXACTLY these fields (no others):
- "subject_key": see the SUBJECT_KEY guide.
- "claim": the claim as it should read in the brief, including its current value/number.
- "claim_type": one of "fact" | "interpretation" | "sentiment". A "fact" may NOT rest on a
  sentiment-only source.
- "section": one of {SECTIONS}.
- "zone": for section "battlecard" ONLY, one of {ZONES}; for every other section, null.
- "order": integer >= 0, the sort order within its section (and zone), most important first.
- "source_url": the SINGLE load-bearing source for this claim (one source per claim).
- "source_tier": "primary" (filings/transcripts/contracts) | "reputable_secondary"
  (reputable news / analyst-estimate, labeled) | "sentiment_only" (reviews/forums).
- "evidence_excerpt": a VERBATIM span (>= 40 characters) copied CHARACTER-FOR-CHARACTER from
  the page at source_url — the span that backs the claim. This is non-negotiable: a
  deterministic check will RE-FETCH source_url and require this excerpt to literally appear on
  the page. If you paraphrase, tighten, or stitch it, the claim WILL BE CUT. Copy, do not write.
- "as_of": the date the fact is true as-of / the source's date, "YYYY-MM-DD" (required for facts).
- "persona" (battlecard + objection_handling ONLY — REQUIRED on EVERY claim in those two
  sections; omit for every other section): the primary buyer persona this play is aimed at, or
  that tends to raise this objection — one of "eng_led" | "technical_evaluator" |
  "economic_buyer" | "security_regulated" | "exec_top_down". Every battlecard play and every
  objection MUST carry one; pick the single best fit from the "for which buyer" reasoning you
  already do — never leave it blank for these sections, and do not invent new values.
- "confidence": "high" | "medium" | "low".
- "corroboration" (optional): a list of secondary sources confirming the SAME value, each
  {{"source_url","source_tier","note","grounded":false}}. Never grounded; never the anchor.
- "anchor_substitution" (optional): include ONLY if the best (higher-tier) source is unfetchable
  by a plain HTTP client (hard paywall, Cloudflare, SEC.gov direct) AND you read both it and a
  fetchable agreeing source. Then make the FETCHABLE source the anchor (source_url/excerpt),
  put the unfetchable one in corroboration, and set
  {{"preferred_url","preferred_tier","agreement_verified":true,"note"}}.
  GUARD: substitution is for FETCH WEAKNESS ONLY. If the two sources CONFLICT, do NOT
  substitute — revise per the hierarchy/recency rules or cut, and log it in the cut log.

Do NOT include "id", "verified", or "grounding" — those are filled deterministically downstream.

GROUNDABILITY: prefer source_url values a plain HTTP client can read. Avoid anchoring on
SEC.gov directly (it blocks datacenter IPs), hard paywalls, or Cloudflare-walled pages; use a
fetchable reputable source as the anchor and keep the stronger one as corroboration.

SOURCING DISCIPLINE (enforced): every "recent_moves" claim and every status/current-state claim
(current/flagship/latest, a launch, a cancellation, a price/limit change) MUST anchor source_url
on a reputable NEWS outlet (Tier 2) or a primary filing/announcement (Tier 1). NEVER anchor any
claim on Wikipedia, a wiki, an encyclopedia (Britannica/Fandom), or a promo/SEO listicle or
aggregator — a deterministic check CUTS any claim anchored on a wiki/encyclopedia domain, so it
will not survive. An ADVERSE fact about the competitor (a cancellation, a loss, churn) should
trace to independent reporting, not only the affected company's own PR.

Three sections — EXECUTIVE SUMMARY, COMPETITIVE BATTLECARD (every zone), and OBJECTION HANDLING —
are authored as short PROSE BLOCKS inside the single "claim" string, NOT as bullets. Natural,
human writing a person would actually say, not a terse spec-sheet line. Keep the analysis sharp and
the "so what" intact; just warm the language (see the Voice and tone methodology section).

EXACT BLOCK SHAPE — every such "claim" string is THREE visually separate parts, each separated by a
genuine BLANK LINE (\\n\\n), never blended into one paragraph. Do NOT start the block with "- ":

  **<bolded one-line title>**

  <a 1-2 sentence paragraph in plain, human language>

  **<label>:** <closing line>

The parts by section:
- EXECUTIVE SUMMARY: title = the verdict; paragraph = the supporting detail; closing = "**So what:**"
  + the concrete decision it changes. Every exec point needs its So what.
- BATTLECARD (section "battlecard"): title = the edge in one line; paragraph = why it holds and for
  which buyer; closing = "**Soundbite:**" + an italicized line a rep could say out loud, e.g.
  *"..."*. Evidence-backed, never combative trash-talk.
- OBJECTION HANDLING (section "objection_handling"): title = the objection a prospect raises — citing
  EITHER the competitor's strength OR an adverse development on OUR OWN side (a customer dropping our
  product, our usage limits, a public setback); paragraph = an honest, evidence-based response that
  pivots to a genuine strength; closing = "**So what:**" + the implication. Ground every objection in
  a REAL surfaced fact, never invented — and never omit a real adverse fact a buyer would raise.

TONE (all three): direct, confident, and human. NO combative or zero-sum phrasing — never "you will
lose", "crush", "dominate", "they're finished". Confidence is a clear verdict with evidence behind
it, not trash talk. A reader should find it sharp AND pleasant to read.
"""


# Battlecard routing — generic (no names, so the system prompt stays cache-stable; the
# dynamic "us vs them" identity arrives in the per-run user framing).
ROUTING_RULES = """BATTLECARD ROUTING — this brief is a SALES WEAPON for OUR side, not neutral coverage
of two companies. The inclusion test for EVERY event is: "does this change how OUR side WINS, LOSES,
or HANDLES AN OBJECTION?" Scan what is happening to BOTH companies (two-sided input), but route every
item asymmetrically as a "so what for us" — nothing appears as neutral trivia.

- THE COMPETITOR's moves -> "recent_moves" (newest-first) and the battlecard zones, filtered to those
  with a real competitive implication for us. A competitor event with no consequence for our position
  does not belong.
- OUR OWN side's POSITIVE / NEUTRAL events (our funding, our IPO filing, our launch) are NOT standalone
  "recent_moves" items — we already know our own moves; listing them is noise. Include one ONLY where
  it carries a competitive implication, framed as a so-what (e.g. our IPO filing -> objection-handling
  ammunition for "is this vendor financially stable enough to bet on?"). If an own-side event has no
  competitive so-what, OMIT it.
- OUR OWN side's ADVERSE events a buyer would raise (a major customer dropping our product, our product
  hitting usage limits, a public setback) -> "objection_handling": state the objection honestly and
  give the rep an evidence-based answer and a real so-what. NEVER omit or bury these — the rep WILL be
  asked (e.g. "I heard Microsoft pulled Claude Code from its dev teams" needs a ready answer).
- "recent_moves" is for THE COMPETITOR, not for us — do not file our own news there.
"""


def _framing(target, perspective, focus):
    foc = f" Focus specifically on: {focus}." if focus else ""
    if perspective:
        title = f"# Competitive Intelligence Brief: {perspective} vs {target}"
        return (
            f"You are arming {perspective}'s sales team against {target}.{foc} This brief is a "
            f"SALES WEAPON FOR {perspective} — NOT neutral coverage of two companies. The inclusion "
            f"test for ANY event is not 'is this recent news about either company?' but: 'does this "
            f"change how {perspective} WINS, LOSES, or HANDLES AN OBJECTION against {target}?' Scan "
            f"everything happening to BOTH companies (two-sided input), but frame every item as a "
            f"'so what for {perspective}' (asymmetric output), and route it per the BATTLECARD "
            f"ROUTING rules: {target}'s moves drive Recent Strategic Moves and the battlecard zones; "
            f"{perspective}'s OWN adverse news a buyer would raise (e.g. a key customer dropping "
            f"{perspective}'s product) goes in Objection Handling with an honest answer; "
            f"{perspective}'s own positive news appears ONLY if it carries a competitive so-what. Be "
            f"honest in both directions — do not assume {perspective} is superior; a weakness the rep "
            f"must defend is as valuable as a strength.",
            title,
        )
    title = f"# Competitive Intelligence Brief: {target}"
    return (
        f"You are a competitive-intelligence analyst researching {target} to produce a "
        f"specific, evidence-grounded competitive intelligence brief.{foc}",
        title,
    )


def _orch_system():
    """STATIC orchestrator instructions -> the system prompt, so they're prompt-CACHED
    across the run's turns and across runs (free lever N), not re-billed every turn.
    Only the dynamic framing/title lives in the per-run user prompt."""
    return f"""You are a competitive-intelligence analyst. You research a competitor and emit an
evidence-grounded brief as structured claim objects plus a cut log.

Follow this methodology exactly:

<methodology>
{load_methodology()}
</methodology>

{SOURCE_HIERARCHY}

{WRITING_STYLE}

{SUBJECT_KEY_GUIDE}

{CLAIM_CONTRACT}

{ROUTING_RULES}

PROCESS: Plan the brief. Delegate research to your 'researcher' subagent and verification to your
'verifier' subagent. DISPATCH THE SECTION RESEARCHERS AS A SINGLE PARALLEL BATCH — issue multiple
Agent calls in ONE step (one per section: {SECTIONS}) rather than one at a time — then verify and
synthesize. Run a final consistency sweep: the same entity/product/version named identically
everywhere; one value per metric; every surviving claim carries a real source link; nothing in
the cut log is also asserted as fact.

OUTPUT: Your FINAL message must be a single fenced ```json code block and NOTHING else:
{{
  "title": "<use EXACTLY the title given in the user message>",
  "claims": [ {{ ...claim objects per the contract... }} ],
  "cut_log": [ {{ "action": "CUT" | "REVISED", "claim": "<short statement>", "reason": "<why>" }} ]
}}
Fewer, solid, sharp, ranked claims beat many weak ones. Every claim must be groundable."""


def _build_user_prompt(target, perspective, focus):
    framing, title = _framing(target, perspective, focus)
    today = date.today().isoformat()
    return f"""{framing}

TODAY'S DATE IS {today}. This brief must reflect the world as of today.
- Run a RECENCY SWEEP: dispatch researchers to find what happened in the last ~2-3 weeks
  (IPO/funding/filings, launches, partnership changes, pricing/limit changes, exec moves).
  "Recent Strategic Moves" must LEAD with the newest items and cover that window — if your
  newest item is weeks old, you have missed the story.
- Surface ADVERSE / competitive-threat signals on BOTH companies, not just wins: cancellations,
  customer churn, budget caps / usage limits hit, outages, layoffs, lawsuits, lost deals. Per the
  ROUTING rules: the COMPETITOR's moves go in Recent Strategic Moves; OUR OWN side's adverse news a
  buyer would raise (a customer dropping our product, our usage limits) goes in Objection Handling
  with an honest answer — never omit it. Our own positive news appears only with a competitive so-what.
- SOURCING DISCIPLINE: every Recent-Strategic-Moves item and every status/current-state claim
  must cite a reputable NEWS outlet (Tier 2) or a primary filing (Tier 1). NEVER Wikipedia, a
  wiki, an encyclopedia, or a promo/SEO listicle/aggregator — those are excluded and will be cut.

Produce the competitive intelligence brief now, per your system instructions.
Use EXACTLY this title in the output JSON:
{title}"""


RETRY_CONTRACT = """You are REPAIRING claims that failed an independent grounding check (a
deterministic re-fetch of source_url that requires evidence_excerpt to appear verbatim on the
page). For each item below, do ONE of: repair it, or drop it. Two failure modes:

- status "absent": your previous excerpt was NOT found verbatim on the page. A `page_text`
  field gives the ACTUAL text of that page as an INDEPENDENT fetcher sees it. Copy a NEW
  evidence_excerpt VERBATIM from `page_text`, character-for-character, that genuinely supports
  the claim. Do NOT copy from memory — only from `page_text`. If `page_text` contains no span
  that supports the claim, DROP the claim.

- status "unreachable": an independent HTTP client could not fetch source_url (it is bot-walled
  or IP-blocked to the grounding fetcher). Use WebSearch + fetch_page to find a DIFFERENT
  reputable source that (a) a plain HTTP client can fetch and (b) you VERIFY agrees with the
  claim. Make it the new source_url with a verbatim excerpt; put the original source in
  `corroboration`; set `anchor_substitution` with `agreement_verified: true`.
  If you cannot find a fetchable AGREEING source, or the sources conflict, DROP the claim — do
  not substitute a conflicting source.

- status "excluded": source_url is a banned wiki/encyclopedia (Wikipedia, Fandom, Britannica,
  etc.) — NEVER permitted as a source here. Use WebSearch + fetch_page to find a REPUTABLE NEWS
  source (Reuters, Bloomberg, The Information, CNBC, TechCrunch, a major outlet) or a primary
  filing that you VERIFY supports the claim, and make THAT the new source_url with a verbatim
  excerpt. Do NOT keep the wiki source anywhere, not even in corroboration. If no reputable news
  source supports it, DROP the claim — a fact that only a wiki asserts is not good enough here.

Keep each repaired claim's subject_key, section, zone, order, claim_type unchanged. Emit full
claim objects per the original contract (NO "id", "verified", or "grounding" fields).

OUTPUT: your final message must be a single fenced ```json block and nothing else:
{
  "revised": [ { ...full claim object... } ],
  "dropped": [ { "action": "CUT", "claim": "<short statement>", "reason": "<why it could not be repaired>" } ]
}"""


def _build_retry_payload(failed):
    """For each failed claim, attach the REAL page text (for 'absent') so the agent
    re-extracts from the exact bytes grounding will re-check. Deterministic; no model."""
    items = []
    for f in failed:
        c = f["claim"]
        entry = {
            "subject_key": c.get("subject_key"), "claim": c.get("claim"),
            "claim_type": c.get("claim_type"), "section": c.get("section"),
            "zone": c.get("zone"), "order": c.get("order"),
            "source_url": c.get("source_url"), "status": f["status"],
        }
        if f["status"] == "absent":
            try:
                resp = _fetch_response(c["source_url"])
                text, _ = _extract_text(resp)
                # collapse whitespace but PRESERVE case, so the agent copies a
                # natural-cased span (grounding normalizes both sides at check time).
                entry["page_text"] = re.sub(r"\s+", " ", text).strip()[:RETRY_PAGE_CHARS]
            except Exception:
                entry["page_text"] = None  # unfetchable now -> agent should drop
        items.append(entry)
    return items


async def _run_retry(payload):
    options = ClaudeAgentOptions(
        model=config.SUBAGENT_MODEL,            # mechanical repair — Sonnet
        # Free lever N: the static repair contract goes in the (cached) system prompt.
        system_prompt={"type": "preset", "preset": "claude_code",
                       "append": RETRY_CONTRACT + "\n\n" + WRITING_STYLE},
        mcp_servers={"scoutfetch": FETCH_SERVER},
        allowed_tools=["WebSearch", FETCH_TOOL_NAME],  # re-source 'unreachable' claims via real fetch
        disallowed_tools=["WebFetch"],
        permission_mode="bypassPermissions",
        max_turns=config.MAX_TURNS,
        max_budget_usd=config.GEN_MAX_BUDGET_USD,
    )
    user = "ITEMS TO REPAIR:\n" + json.dumps(payload, ensure_ascii=False)
    return await _drive(user, options, "retry-agent")


def _extract_json(text: str) -> dict:
    """Pull the final JSON object out of the orchestrator's last message. Robust to
    fenced (```json) output, nested braces, and prose around it."""
    # Capture each fenced block's CONTENTS (non-greedy on the FENCE, not the braces),
    # last-first, and return the first that parses as a JSON object.
    for block in reversed(re.findall(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)):
        block = block.strip()
        if block.startswith("{"):
            try:
                return json.loads(block)
            except json.JSONDecodeError:
                pass
    # Fallback: the widest brace span in the text.
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start:end + 1])
    raise ValueError("no JSON object found in orchestrator output")


def _accept_grounded(slug, grounded_kept, schema_problems):
    """Filter grounded claims to those we publish, but NEVER silently drop a render-format failure
    (decision-log §11). A claim that grounded TRUE and fails ONLY the render-structure gate is a
    confirmed-material claim with a formatting problem -> repair-or-hold it (publish the repair, or
    HOLD + flag), never cut. Claims with OTHER schema errors are surfaced to schema_problems rather
    than vanishing. This replaces the old `[c for c in kept if not validation_errors(c)]`, which
    silently dropped both."""
    from scout import reformat
    out = []
    for c in grounded_kept:
        errs = validation_errors(c)
        if not errs:
            out.append(c)
        elif all(("So what" in e or "Soundbite" in e) for e in errs):   # render-format only
            status, c2 = reformat.repair_or_hold(slug, c)
            if status != "held":
                out.append(c2)                                          # held -> surfaced, never dropped
        else:
            schema_problems.append((c.get("subject_key"), errs))        # other schema errors: surface
    return out


def _u(usage, key):
    if usage is None:
        return 0
    if isinstance(usage, dict):
        return usage.get(key, 0) or 0
    return getattr(usage, key, 0) or 0


async def _drive(prompt: str, options, top_role: str) -> dict:
    """Run a query loop and capture per-agent token usage (orchestrator vs each
    subagent), cache hits, cost, and wall/api time — the Phase-1 instrumentation."""
    by_role, agent_names = {}, {}
    final_text, last_text, result = None, "", None

    def bump(role, usage):
        d = by_role.setdefault(role, {"input": 0, "output": 0, "cache_read": 0,
                                      "cache_creation": 0, "messages": 0})
        d["input"] += _u(usage, "input_tokens")
        d["output"] += _u(usage, "output_tokens")
        d["cache_read"] += _u(usage, "cache_read_input_tokens")
        d["cache_creation"] += _u(usage, "cache_creation_input_tokens")
        d["messages"] += 1

    try:
        async for message in query(prompt=prompt, options=options):
            kind = type(message).__name__
            if kind == "AssistantMessage":
                parent = getattr(message, "parent_tool_use_id", None)
                role = top_role if parent is None else agent_names.get(parent, "subagent")
                bump(role, getattr(message, "usage", None))
                for b in getattr(message, "content", []) or []:
                    bk = type(b).__name__
                    if bk == "ToolUseBlock" and getattr(b, "name", "") == "Agent":
                        inp = getattr(b, "input", {}) or {}
                        agent_names[getattr(b, "id", "")] = inp.get("subagent_type") or "subagent"
                    elif bk == "TextBlock":
                        last_text = getattr(b, "text", "") or last_text
            elif kind == "ResultMessage":
                result = message
                final_text = getattr(message, "result", None)
    except BaseException as e:
        # A crash mid-stream still costs money: the subagents already ran their web searches
        # and model calls server-side. Make that LOUD so a failed run is never mistaken for free.
        tok = sum(d["input"] + d["output"] + d["cache_creation"] for d in by_role.values())
        msgs = sum(d["messages"] for d in by_role.values())
        print(f"[generate] {top_role} run FAILED mid-stream — billable work already ran: "
              f"{msgs} msgs, ~{tok} non-cache tokens across {list(by_role) or '[]'} BEFORE the "
              f"error. A crashed run is NOT free; verify actual usage before any retry. "
              f"Error: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        raise

    return {
        "text": final_text or last_text,
        "cost_usd": getattr(result, "total_cost_usd", None),
        "duration_ms": getattr(result, "duration_ms", None),
        "duration_api_ms": getattr(result, "duration_api_ms", None),
        "num_turns": getattr(result, "num_turns", None),
        "by_role": by_role,
        "model_usage": getattr(result, "model_usage", None),
    }


async def _preflight():
    """A trivial one-turn query with a near-zero budget, run BEFORE the expensive
    orchestration. Its only job is to prove the SDK and the Claude CLI can still talk to
    each other. If they can't (CLI auto-updated past the pinned SDK, missing CLI, broken
    auth) this fails for pennies, and generate() aborts before spending real money. This is
    the exact failure class that silently cost ~$28 once: a full run that bills, then
    crashes at the final result-parse. Cheap-and-loud beats expensive-and-silent."""
    options = ClaudeAgentOptions(
        model=config.SUBAGENT_MODEL,
        permission_mode="bypassPermissions",
        max_turns=1,
        max_budget_usd=0.10,
        allowed_tools=[],
        disallowed_tools=["WebSearch", "WebFetch", "Agent"],
    )
    saw_result = False
    async for message in query(prompt="Reply with exactly: OK", options=options):
        if type(message).__name__ == "ResultMessage":
            saw_result = True
    if not saw_result:
        raise RuntimeError("preflight returned no ResultMessage")


async def _run_orchestrator(target, perspective, focus) -> dict:
    options = ClaudeAgentOptions(
        model=config.ORCHESTRATOR_MODEL,                  # Opus orchestrator
        # Free lever N: static instructions in the (cached) system prompt, appended
        # to the default preset; only the dynamic framing goes in the user prompt.
        system_prompt={"type": "preset", "preset": "claude_code", "append": _orch_system()},
        agents={"researcher": RESEARCHER, "verifier": VERIFIER},
        mcp_servers={"scoutfetch": FETCH_SERVER},        # our httpx fetch tool, replaces WebFetch
        allowed_tools=["Agent", "WebSearch", FETCH_TOOL_NAME],
        disallowed_tools=["WebFetch"],                    # no model-mediated fetch anywhere
        permission_mode="bypassPermissions",
        max_turns=config.MAX_TURNS,
        max_budget_usd=config.GEN_MAX_BUDGET_USD,
    )
    return await _drive(_build_user_prompt(target, perspective, focus), options, "orchestrator")


def _coverage_report(results, fetch_log):
    """The coverage read: did keyword-windowing surface facts a blunt first-N-chars
    cap would have missed, and how often did windowing fall back to the page head?"""
    grounded_sub = [r for r in results
                    if r.get("status") == "grounded" and r.get("excerpt_offset") is not None]
    beyond = [r for r in grounded_sub if (r["excerpt_offset"] or 0) > BLUNT_CAP]
    fetches = [f for f in fetch_log if "error" not in f]
    return {
        "blunt_cap_chars": BLUNT_CAP,
        "grounded_substring_claims": len(grounded_sub),
        # supporting fact sat DEEPER than a blunt cap -> windowing captured it, blunt would miss
        "facts_beyond_blunt_cap": len(beyond),
        "deepest_grounded_offset": max((r["excerpt_offset"] or 0 for r in grounded_sub), default=0),
        "fetch_calls": len(fetch_log),
        "fetch_errors": sum(1 for f in fetch_log if "error" in f),
        "windowed_fetches": sum(1 for f in fetches if f.get("windowed")),
        "fallback_fetches": sum(1 for f in fetches if not f.get("windowed")),  # query terms not found
        "fetches_reaching_beyond_blunt": sum(1 for f in fetches if (f.get("max_end") or 0) > BLUNT_CAP),
        "avg_returned_len": round(sum(f.get("returned_len", 0) for f in fetches) / max(1, len(fetches))),
    }


def generate(target, perspective=None, focus=None, write=True, retry=True):
    """Run the full generation -> ground -> [retry] -> render -> store pipeline.
    Returns a result dict with claims, cut log, grounding instrumentation, and markdown."""
    slug = make_slug(target, perspective, focus)
    reset_log()  # clear the fetch-tool coverage log for this run
    # PREFLIGHT: prove the SDK<->CLI handshake works for pennies before committing to the
    # ~$10 run. If the toolchain is broken (e.g. the local Claude CLI auto-updated past the
    # pinned SDK), abort here rather than bill a full run that then crashes at the end.
    try:
        asyncio.run(_preflight())
    except BaseException as e:
        raise RuntimeError(
            f"PREFLIGHT FAILED ({type(e).__name__}: {e}). The SDK<->CLI handshake is broken, "
            f"most likely a Claude CLI version that no longer matches claude-agent-sdk. Refusing "
            f"to start the ~$10 generation. Re-sync the CLI and SDK, then retry."
        ) from e
    run = asyncio.run(_run_orchestrator(target, perspective, focus))
    data = _extract_json(run["text"])

    # Derive deterministic id + set verified; validate the pre-grounding shape so we
    # don't spend a fetch on a malformed claim. Malformed ones are reported, not stored.
    candidates, schema_problems = [], []
    claims_in = data.get("claims")
    if not isinstance(claims_in, list):
        claims_in = []
    for c in claims_in:
        if not isinstance(c, dict) or "subject_key" not in c:
            schema_problems.append((None, ["claim is not a valid object / missing subject_key"]))
            continue
        c["id"] = claim_id(slug, str(c["subject_key"]))
        c["verified"] = True
        errs = pregrounding_errors(c)
        if errs:
            schema_problems.append((c.get("subject_key"), errs))
        else:
            candidates.append(c)

    # GROUND (independent fetch; model-free) — drops absent/unreachable claims.
    grounded = ground_claims(candidates)
    kept = _accept_grounded(slug, grounded["kept"], schema_problems)  # no-drop: repair-or-hold, never silently cut
    failed = grounded.get("failed", [])

    # FEEDBACK RETRY (one bounded round): send each failed claim back to repair —
    #   'absent'      -> re-extract a verbatim span from the REAL page text we fetched;
    #   'unreachable' -> substitute a fetchable AGREEING source (guarded) or drop.
    # Then re-ground the repairs. Recovers true claims the first pass cut on excerpt
    # drift, and closes the substitution gap (verifier never saw our block first time).
    retry_info = {"attempted": False, "revised_grounded": 0, "dropped": [], "still_failed": 0, "run": None}
    if retry and failed:
        retry_info["attempted"] = True
        rr = asyncio.run(_run_retry(_build_retry_payload(failed)))
        retry_info["run"] = rr
        try:
            rdata = _extract_json(rr["text"])
        except Exception:
            rdata = {"revised": [], "dropped": []}
        revised = []
        for c in rdata.get("revised", []):
            if "subject_key" not in c:
                continue
            c["id"] = claim_id(slug, c["subject_key"])
            c["verified"] = True
            if not pregrounding_errors(c):
                revised.append(c)
        reground = ground_claims(revised) if revised else {"kept": [], "failed": []}
        newly = _accept_grounded(slug, reground["kept"], schema_problems)  # no-drop on the retry path too
        kept += newly
        # Every ORIGINAL failure must end up either recovered (re-grounded into kept)
        # or in the Cut Log — never silently dropped. Match recoveries by stable id.
        recovered_ids = {c["id"] for c in newly}
        final_cut = [
            {"action": "CUT", "claim": f["claim"].get("claim", "?"), "reason": f["reason"]}
            for f in failed if f["claim"].get("id") not in recovered_ids
        ]
        retry_info.update(
            revised_grounded=len(newly),
            dropped=rdata.get("dropped", []),
            still_failed=len(failed) - len(newly),
        )
    else:
        final_cut = list(grounded["cut"])

    model_cut = data.get("cut_log")
    if not isinstance(model_cut, list):
        model_cut = []
    cut_log = model_cut + final_cut
    title = data.get("title")
    if not isinstance(title, str) or "Competitive Intelligence Brief" not in title:
        title = _framing(target, perspective, focus)[1]
    body = claims_to_markdown(kept, title, my_company=perspective, competitor=target)
    markdown = format_report(clean_output(body + "\n\n" + render_cut_log(cut_log)))

    paths = None
    if write:
        meta = new_meta(target, perspective, focus, slug)
        paths = write_baseline(slug, kept, meta, markdown)
        # Shadow-eval observer (v3.5): record champion decisions for offline challenger scoring.
        # No-op unless SCOUT_SHADOW_EVAL=1; guaranteed not to raise (scout/shadow.py).
        shadow.capture(slug, "generate", kept=kept, cut=cut_log, grounding=grounded,
                       competitor=target, my_company=perspective, focus=focus)

    coverage = _coverage_report(grounded.get("results", []), FETCH_LOG)

    result = {
        "slug": slug,
        "kept": kept,
        "cut_log": cut_log,
        "grounding": grounded,            # FIRST-pass counts, per-claim results, substituted
        "retry": retry_info,              # repair lift: revised_grounded, dropped, still_failed
        "coverage": coverage,             # windowing vs blunt-cap coverage read
        "fetch_log": FETCH_LOG,           # per-fetch instrumentation
        "schema_problems": schema_problems,
        "markdown": markdown,
        "paths": paths,
        "run": run,                       # cost, num_turns
    }
    # Persist a debug artifact (incl. every excerpt + grounding ratio) so a
    # measurement run is inspectable offline WITHOUT re-running — even a dry run.
    # Set SCOUT_DEBUG_DIR to enable. (Lesson from the first #7 run: don't lose the data.)
    debug_dir = os.environ.get("SCOUT_DEBUG_DIR")
    if debug_dir:
        os.makedirs(debug_dir, exist_ok=True)
        with open(os.path.join(debug_dir, f"{slug}.json"), "w") as f:
            json.dump(result, f, indent=2, default=str, ensure_ascii=False)
    return result


def _fmt_roles(by_role):
    lines = []
    for role, d in sorted(by_role.items(), key=lambda kv: -kv[1]["input"]):
        lines.append(
            f"      {role:14} msgs={d['messages']:3} in={d['input']:>8} out={d['output']:>7} "
            f"cache_read={d['cache_read']:>8} cache_write={d['cache_creation']:>7}"
        )
    return "\n".join(lines)


def _print_report(res):
    g, rt, run = res["grounding"], res["retry"], res["run"]
    print(f"\n=== {res['slug']} ===")
    dur = run.get("duration_ms")
    print(f"GENERATION  cost=${run.get('cost_usd')}  wall={dur/1000 if dur else '?'}s  "
          f"api={ (run.get('duration_api_ms') or 0)/1000 }s  turns={run.get('num_turns')}")
    print(f"  per-agent (orchestrator vs subagents):\n{_fmt_roles(run.get('by_role', {}))}")
    print(f"  model_usage: {run.get('model_usage')}")
    print(f"first-pass grounding: {g['counts']}  substituted={g['substituted']}")
    if rt["attempted"]:
        rr = rt["run"] or {}
        print(f"RETRY  cost=${rr.get('cost_usd')}  recovered={rt['revised_grounded']}  "
              f"dropped={len(rt['dropped'])}  still_failed={rt['still_failed']}")
        print(f"  per-agent:\n{_fmt_roles(rr.get('by_role', {}))}")
    gen_cost = run.get("cost_usd") or 0
    retry_cost = ((rt.get("run") or {}).get("cost_usd")) or 0
    print(f"TOTAL cost=${gen_cost + retry_cost:.4f}  (gen ${gen_cost} + retry ${retry_cost})")
    print(f"kept (valid+grounded): {len(res['kept'])}   schema_problems: {len(res['schema_problems'])}")
    cov = res["coverage"]
    print("COVERAGE (windowed fetch vs blunt cap):")
    print(f"  fetch_calls={cov['fetch_calls']} windowed={cov['windowed_fetches']} "
          f"fallback={cov['fallback_fetches']} errors={cov['fetch_errors']} "
          f"avg_returned={cov['avg_returned_len']}ch")
    print(f"  facts BEYOND blunt {cov['blunt_cap_chars']}ch cap: {cov['facts_beyond_blunt_cap']}"
          f"/{cov['grounded_substring_claims']} grounded  (deepest fact @ {cov['deepest_grounded_offset']}ch)")
    print(f"  fetches reaching beyond blunt cap: {cov['fetches_reaching_beyond_blunt']}")
    if res["paths"]:
        print(f"written: {res['paths']['dir']}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print('usage: python -m scout.generate "<competitor>" ["<your company>"] ["<focus>"]')
        sys.exit(1)
    target = args[0]
    perspective = args[1] if len(args) > 1 else None
    focus = args[2] if len(args) > 2 else None
    _print_report(generate(target, perspective, focus))
