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

from scout import config, shadow, calllog, sources_tool
from scout.prompts import SOURCE_HIERARCHY, WRITING_STYLE, load_methodology
from scout.schema import (
    SECTIONS, ZONES, claim_id, normalize_claim, pregrounding_errors, validation_errors,
)
from scout.grounding import ground_claims, _fetch_response, _extract_text
from scout.fetch_tool import (
    FETCH_SERVER, FETCH_TOOL_NAME, FETCH_LOG, reset_log, BLUNT_CAP,
)
from scout.render import claims_to_markdown, render_cut_log, clean_output, format_report
from scout.store import make_slug, new_meta, write_baseline
from scout import judgment

# How much of a failed page's REAL (httpx) text to hand the retry agent to re-extract
# a verbatim span from. Bounds tokens; a supporting span beyond this -> the claim drops.
RETRY_PAGE_CHARS = 18000

# --- Subagents ----------------------------------------------------------------
RESEARCHER = AgentDefinition(
    description="Researches a company by searching and READING sources.",
    prompt=(
        judgment.get("generate.RESEARCHER.prompt")
    ),
    tools=["WebSearch", FETCH_TOOL_NAME, *sources_tool.names()],
    model=config.SUBAGENT_MODEL,
)

VERIFIER = AgentDefinition(
    description="Independently fact-checks each claim like a news editor.",
    prompt=(
        judgment.get("generate.VERIFIER.prompt")
    ),
    tools=["WebSearch", FETCH_TOOL_NAME, *sources_tool.names()],
    model=config.SUBAGENT_MODEL,
)

# --- The claim contract the orchestrator must emit ----------------------------
SUBJECT_KEY_GUIDE = judgment.get("generate.SUBJECT_KEY_GUIDE")

CLAIM_CONTRACT = judgment.get("generate.CLAIM_CONTRACT", {'SECTIONS': SECTIONS, 'ZONES': ZONES})


# Battlecard routing — generic (no names, so the system prompt stays cache-stable; the
# dynamic "us vs them" identity arrives in the per-run user framing).
ROUTING_RULES = judgment.get("generate.ROUTING_RULES")


def _framing(target, perspective, focus):
    foc = f" Focus specifically on: {focus}." if focus else ""
    if perspective:
        title = f"# Competitive Intelligence Brief: {perspective} vs {target}"
        return (
            judgment.text("generate._FRAMING_VS", {'perspective': perspective, 'target': target, 'foc': foc}),
            title,
        )
    title = f"# Competitive Intelligence Brief: {target}"
    return (
        judgment.text("generate._FRAMING_SOLO", {'target': target, 'foc': foc}),
        title,
    )


def _orch_system():
    """STATIC orchestrator instructions -> the system prompt, so they're prompt-CACHED
    across the run's turns and across runs (free lever N), not re-billed every turn.
    Only the dynamic framing/title lives in the per-run user prompt."""
    return judgment.text("generate._ORCH_SYSTEM", {'load_methodology()': load_methodology(), 'SOURCE_HIERARCHY': SOURCE_HIERARCHY, 'WRITING_STYLE': WRITING_STYLE, 'SUBJECT_KEY_GUIDE': SUBJECT_KEY_GUIDE, 'CLAIM_CONTRACT': CLAIM_CONTRACT, 'ROUTING_RULES': ROUTING_RULES, 'SECTIONS': SECTIONS})


def _build_user_prompt(target, perspective, focus):
    framing, title = _framing(target, perspective, focus)
    today = date.today().isoformat()
    return judgment.text("generate._USER_PROMPT", {'framing': framing, 'today': today, 'title': title})


RETRY_CONTRACT = judgment.get("generate.RETRY_CONTRACT")


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
                       "append": RETRY_CONTRACT + sources_tool.note() + "\n\n" + WRITING_STYLE},
        mcp_servers={"scoutfetch": FETCH_SERVER, **sources_tool.servers()},
        allowed_tools=["WebSearch", FETCH_TOOL_NAME, *sources_tool.names()],  # re-source 'unreachable' claims via real fetch
        disallowed_tools=config.MODEL_DISALLOWED_TOOLS,
        permission_mode="bypassPermissions",
        max_turns=config.MAX_TURNS,
        max_budget_usd=config.GEN_MAX_BUDGET_USD,
    )
    user = "ITEMS TO REPAIR:\n" + json.dumps(payload, ensure_ascii=False)
    return await _drive(user, options, "retry-agent")


def _extract_json(text: str) -> dict:
    """Pull the final JSON object out of the orchestrator's last message. Robust to
    fenced (```json) output, nested braces, prose around it, and (2026-09-28) an object the
    model closed one bracket short: Opus judge verdicts ended `...}]` + fence with the outer `}`
    missing in ~1 in 5 propagation runs (Sep 2026), which sent two Opus calls to the bin and the
    verdict to the Sonnet fallback (~$0.60 a run, verdicts not adjudicable). `_close_unbalanced`
    appends ONLY the missing closers, never content; anything else still fails."""
    # Capture each fenced block's CONTENTS (non-greedy on the FENCE, not the braces),
    # last-first, and return the first that parses as a JSON object.
    for block in reversed(re.findall(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)):
        block = block.strip()
        if block.startswith("{"):
            try:
                return json.loads(block)
            except json.JSONDecodeError:
                repaired = _close_unbalanced(block)
                if repaired is not None:
                    return repaired
    # Fallback: the widest brace span in the text.
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            repaired = _close_unbalanced(text[start:])
            if repaired is not None:
                return repaired
            raise
    if start != -1:
        repaired = _close_unbalanced(text[start:])
        if repaired is not None:
            return repaired
    raise ValueError("no JSON object found in orchestrator output")


def _close_unbalanced(block: str):
    """If `block` is a JSON object missing only its final closers (`}` / `]`), append them in the
    right order and return the parsed object; else None. Strings are skipped when counting, so a
    brace inside a "reason" cannot fool it. Deterministic, content-free."""
    stack, in_str, esc = [], False, False
    for ch in block:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if not stack or stack[-1] != ch:
                return None
            stack.pop()
    if in_str or not stack or len(stack) > 3:
        return None
    try:
        return json.loads(block.rstrip().rstrip(",") + "".join(reversed(stack)))
    except json.JSONDecodeError:
        return None


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


# Run-level token totals by role, accumulated across every _drive call since the last reset —
# snapshotted into the monitor's per-run cost ledger so cache behavior and per-phase input weight
# are measurable over time (2026-07-02 cost pass). Additive observation only.
ROLE_TOTALS: dict = {}

# Optional live-progress hook (2026-07-19): generate(on_stage=...) sets this so the self-serve
# runner can surface REAL pipeline stages to the visitor's progress bar. Fail-soft by contract —
# a stage-callback failure can NEVER break a paid generation — and None (the default) keeps every
# other caller (monitor, scripts) byte-identical.
_ON_STAGE = None
# Same contract for tool calls (2026-09-28): Ask Scout streams "Searching: …" / "Reading: …" lines
# to the reader while the research pass runs, so a two-minute step is not one frozen line.
_ON_TOOL = None


def _emit_stage(stage: str) -> None:
    cb = _ON_STAGE
    if cb is None:
        return
    try:
        cb(stage)
    except Exception as e:
        print(f"[generate] stage hook skipped ({type(e).__name__}: {e})", file=sys.stderr)


def _emit_tool(name: str, inp: dict) -> None:
    cb = _ON_TOOL
    if cb is None:
        return
    try:
        cb(name, inp)
    except Exception as e:
        print(f"[generate] tool hook skipped ({type(e).__name__}: {e})", file=sys.stderr)


def reset_role_totals() -> None:
    ROLE_TOTALS.clear()


def _merge_role_totals(by_role: dict) -> None:
    for role, d in by_role.items():
        t = ROLE_TOTALS.setdefault(role, {"input": 0, "output": 0, "cache_read": 0,
                                          "cache_creation": 0, "messages": 0})
        for k in t:
            t[k] += d.get(k, 0)


async def _drive(prompt: str, options, top_role: str) -> dict:
    """Run a query loop and capture per-agent token usage (orchestrator vs each
    subagent), cache hits, cost, and wall/api time — the Phase-1 instrumentation."""
    judgment.require()                          # no model call without the judgment pack
    judgment.assert_clean(prompt, getattr(options, "system_prompt", None))
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

    cap = calllog.start(top_role, prompt, options)      # call capture: None unless enabled + run open
    try:
        async for message in query(prompt=prompt, options=options):
            kind = type(message).__name__
            parent = getattr(message, "parent_tool_use_id", None)
            role = top_role if parent is None else agent_names.get(parent, "subagent")
            if cap is not None:
                cap.event(message, role)
            if kind == "AssistantMessage":
                # First sighting of a research/verify subagent = a REAL pipeline stage boundary
                # for the live progress UI. Fires once per role per drive; fail-soft.
                if role in ("researcher", "verifier") and role not in by_role:
                    _emit_stage("researching" if role == "researcher" else "verifying")
                bump(role, getattr(message, "usage", None))
                for b in getattr(message, "content", []) or []:
                    bk = type(b).__name__
                    if bk == "ToolUseBlock":
                        inp = getattr(b, "input", {}) or {}
                        if getattr(b, "name", "") == "Agent":
                            agent_names[getattr(b, "id", "")] = inp.get("subagent_type") or "subagent"
                        _emit_tool(getattr(b, "name", "") or "", inp if isinstance(inp, dict) else {})
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
        if cap is not None:
            cap.fail(e, by_role)
        # The SDK yields the error ResultMessage (with the cost so far) BEFORE it raises on the
        # CLI's non-zero exit, so a budget-exhausted run still knows what it spent (2026-09-28):
        # callers with a ledger settle the real number instead of guessing. A process that died
        # before its first message did no billable work (the Cloud Run root-user ProcessError), so
        # that is a KNOWN zero; None stays "unknown" and the ledger fails closed on it.
        try:
            e.scout_cost_usd = 0.0 if (result is None and msgs == 0) else getattr(result, "total_cost_usd", None)
        except Exception:
            pass
        raise

    _merge_role_totals(by_role)
    out = {
        "text": final_text or last_text,
        "cost_usd": getattr(result, "total_cost_usd", None),
        "duration_ms": getattr(result, "duration_ms", None),
        "duration_api_ms": getattr(result, "duration_api_ms", None),
        "num_turns": getattr(result, "num_turns", None),
        "by_role": by_role,
        "model_usage": getattr(result, "model_usage", None),
    }
    if cap is not None:
        cap.finish(out)
    return out


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
        # Calibration note (2026-07-18): the trivial turn's REAL cost is ~$0.17 now (CLI system-
        # prompt overhead grew since June, when it was <$0.10) — the old $0.10 cap tripped on
        # HEALTHY runs and blocked the first visitor generation. $0.50 keeps the guard cheap-and-
        # loud (a broken toolchain still fails for cents) with headroom against overhead creep.
        max_budget_usd=0.50,
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
        system_prompt={"type": "preset", "preset": "claude_code", "append": _orch_system() + sources_tool.note()},
        agents={"researcher": RESEARCHER, "verifier": VERIFIER},
        mcp_servers={"scoutfetch": FETCH_SERVER, **sources_tool.servers()},   # our httpx fetch tool, replaces WebFetch
        allowed_tools=["Agent", "WebSearch", FETCH_TOOL_NAME, *sources_tool.names()],
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


def generate(target, perspective=None, focus=None, write=True, retry=True, on_stage=None):
    """Run the full generation -> ground -> [retry] -> render -> store pipeline.
    Returns a result dict with claims, cut log, grounding instrumentation, and markdown.
    `on_stage` (optional, fail-soft) receives real pipeline stage names for live progress UIs:
    preflight_ok -> researching -> verifying -> grounding -> rendering."""
    global _ON_STAGE
    _ON_STAGE = on_stage
    try:
        return _generate_inner(target, perspective, focus, write, retry)
    finally:
        _ON_STAGE = None


def _generate_inner(target, perspective, focus, write, retry):
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
    _emit_stage("preflight_ok")
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
        normalize_claim(c)
        errs = pregrounding_errors(c)
        if errs:
            schema_problems.append((c.get("subject_key"), errs))
        else:
            candidates.append(c)

    # GROUND (independent fetch; model-free) — drops absent/unreachable claims.
    _emit_stage("grounding")
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
            normalize_claim(c)
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

    _emit_stage("rendering")
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
    calllog.begin_run("baseline")                       # roster baseline = real spend; no-op unless enabled
    calllog.set_context(slug=make_slug(target, perspective, focus), phase="generate")
    try:
        _print_report(generate(target, perspective, focus))
    finally:
        calllog.flush_run(True)
