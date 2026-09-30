"""Local agent loop for the on-device model comparison, question 2 (plan 2026-09-28, section C).

Question 1 (exact replay, scout/replaybackends.py) asks whether an on-device model DECIDES the same
way on identical inputs. This module asks whether it could run a tools-on step ITSELF: given the
captured user prompt + system append of a triage / materiality / my_facts call, the local model
searches and fetches with its own tools and produces the step's output, which scout/modelcompare.py
scores against the live result exactly as it scores the exact replays.

ONE loop, over the OpenAI chat message shape, with two tools that mirror what the live run had:
search(query) via a self-hosted SearXNG on loopback, and fetch_page(url, query) via Scout's own
fetcher (scout.grounding + scout.fetch_tool._window with a smaller budget). Every backend gets the
same LOOP_BUDGET, so retrieval capacity is held constant and only the model varies.

Two tool protocols, recorded on every result and sliced in the scorecard:
  native       Ollama (Magistral / Mistral Small are function-calling tagged): `tools` in the request,
               `message.tool_calls` back, `role: tool` messages in.
  action_json  Apple `fm serve` cannot return tool_calls, so each turn is schema-constrained to ONE
               action object {action: search|fetch_page|answer, ...}; the same two tool schemas,
               wrapped.

This is control code meant to outlive the eval (the roadmap's own-loop direction): nothing here
touches the live MCP path, and the loop never spends a cent (both backends are local; the
`anthropic` backend is refused here by construction).
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from urllib.parse import urlparse

import httpx

from scout import config, replaybackends as rb, rolespecs

SEARXNG_URL = os.environ.get("SCOUT_SEARXNG_URL", "http://127.0.0.1:8080")

# The same budget on every backend (R9): capacity is a constant, the model is the variable.
LOOP_BUDGET = {"search_hits": 5, "hit_chars": 200, "fetch_chars": 4000, "max_tool_calls": 12}
SEARCH_MIN_SPACING_S = 3.0      # a small model looping on one query rate-limited Brave in 30 s (smoke, 2026-09-28)
_last_search_at = 0.0

TOOL_PROTOCOL = {**{name: "native" for name in rb.OLLAMA_MODELS}, "apple_ondevice": "action_json"}

SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "search",
        "description": "Web search. Returns up to 5 results as 'title | url | snippet' lines.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}},
                       "required": ["query"]},
    },
}
FETCH_TOOL = {
    "type": "function",
    "function": {
        "name": "fetch_page",
        # Mirrors scout/fetch_tool.py's description so both arms read the same tool contract.
        "description": ("Fetch a URL and return the REAL page text (no AI summary). Pass `query` = the "
                        "specific thing you are looking for; the tool returns the passages around it so "
                        "you can copy a verbatim span."),
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}, "query": {"type": "string"}},
                       "required": ["url", "query"]},
    },
}
# The structured-source tools (WS1) are declared once in scout/sources/toolspec.py; this list is
# the OpenAI-shaped mirror so the local models get exactly the tools the paid model had.
from scout.sources import toolspec as _toolspec  # noqa: E402

TOOLS = [SEARCH_TOOL, FETCH_TOOL] + _toolspec.openai_tools()

ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["search", "fetch_page", "answer"]},
        "query": {"type": "string", "description": "search: the query; fetch_page: what to look for on the page"},
        "url": {"type": "string", "description": "fetch_page only"},
        "answer": {"type": "string", "description": "answer only: the FINAL output, exactly as the task asks for it"},
    },
    "required": ["action"],
}

_LOOP_PREAMBLE = (
    "You are running as a small autonomous agent with two tools: search(query) and "
    "fetch_page(url, query). Use them to do the task below, then give the final answer in exactly "
    "the format the task asks for. Keep tool use focused: you have a limited number of turns."
)
_ACTION_PREAMBLE = (
    "You are running as a small autonomous agent. Each turn, reply with ONE JSON action object: "
    '{"action":"search","query":"..."} to search the web, {"action":"fetch_page","url":"...","query":"..."} '
    'to read a page, or {"action":"answer","answer":"..."} where `answer` is the FINAL output exactly in '
    "the format the task asks for (as a string). Keep tool use focused: you have a limited number of turns."
)


class UnsupportedBackend(Exception):
    pass


# --- tools -------------------------------------------------------------------------------------------
def tool_search(query: str) -> tuple[str, dict]:
    """SearXNG JSON search. Meta records what the scorecard's `tool_failure` class needs."""
    global _last_search_at
    meta = {"name": "search", "query": query, "status": "ok", "http_status": None, "result_count": 0,
            "unresponsive_engines": [], "hosts": []}
    wait = SEARCH_MIN_SPACING_S - (time.monotonic() - _last_search_at)
    if wait > 0:
        time.sleep(wait)
    _last_search_at = time.monotonic()
    try:
        r = httpx.get(f"{SEARXNG_URL}/search", params={"q": query, "format": "json"}, timeout=20)
        meta["http_status"] = r.status_code
        if r.status_code != 200:
            meta["status"] = "http_error"
            return f"SEARCH_ERROR HTTP {r.status_code}", meta
        data = r.json()
    except Exception as e:
        meta["status"] = f"transport:{type(e).__name__}"
        return f"SEARCH_ERROR {type(e).__name__}", meta
    meta["unresponsive_engines"] = [list(x) if isinstance(x, (list, tuple)) else x
                                    for x in (data.get("unresponsive_engines") or [])]
    hits = (data.get("results") or [])[:LOOP_BUDGET["search_hits"]]
    meta["result_count"] = len(data.get("results") or [])
    lines = []
    for h in hits:
        url = str(h.get("url") or "")
        meta["hosts"].append(_host(url))
        snippet = re.sub(r"\s+", " ", str(h.get("content") or ""))[:LOOP_BUDGET["hit_chars"]]
        lines.append(f"{str(h.get('title') or '')[:120]} | {url} | {snippet}")
    if not lines:
        meta["status"] = "empty"
        return "NO_RESULTS", meta
    return "\n".join(lines), meta


def tool_fetch(url: str, query: str) -> tuple[str, dict]:
    """Scout's own fetcher with the loop's smaller window budget (fetch_tool._window budget_chars)."""
    from scout.fetch_tool import _collapse, _window
    from scout.grounding import _extract_text, _fetch_response
    meta = {"name": "fetch_page", "url": url, "query": query, "status": "ok", "http_status": None,
            "chars": 0, "hosts": [_host(url)]}
    try:
        resp = _fetch_response(url)
        meta["http_status"] = getattr(resp, "status_code", None)
        if resp is None or resp.status_code >= 400:
            meta["status"] = "http_error"
            return f"FETCH_ERROR HTTP {getattr(resp, 'status_code', '?')}", meta
        text, _kind = _extract_text(resp)
        page = _collapse(text)
        out, _windowed, n, _end = _window(page, query, budget_chars=LOOP_BUDGET["fetch_chars"])
        meta["chars"] = n
        return out or "EMPTY_PAGE", meta
    except Exception as e:
        meta["status"] = f"transport:{type(e).__name__}"
        return f"FETCH_ERROR {type(e).__name__}", meta


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().removeprefix("www.")
    except Exception:
        return ""


def _run_tool(name: str, args: dict) -> tuple[str, dict, bool]:
    """(text, meta, arg_schema_ok)."""
    if name == "search":
        q = args.get("query")
        if not isinstance(q, str) or not q.strip():
            return "TOOL_ARG_ERROR search needs a non-empty `query`", {"name": name, "status": "bad_args"}, False
        text, meta = tool_search(q.strip())
        return text, meta, True
    if name == "fetch_page":
        u, q = args.get("url"), args.get("query")
        if not isinstance(u, str) or not u.startswith("http"):
            return "TOOL_ARG_ERROR fetch_page needs an http(s) `url`", {"name": name, "status": "bad_args"}, False
        text, meta = tool_fetch(u.strip(), q if isinstance(q, str) else "")
        return text, meta, True
    if name in _toolspec.BY_NAME:
        spec = _toolspec.BY_NAME[name]
        missing = [k for k in spec.required if not args.get(k)]
        if missing:
            return f"TOOL_ARG_ERROR {name} needs {missing}", {"name": name, "status": "bad_args"}, False
        header, body = _toolspec.run(name, args)
        meta = {"name": name, "args": {k: args.get(k) for k in spec.params}, "status": "ok" if header else "empty",
                "hosts": [_host(header.split("url=", 1)[1].split()[0])] if "url=" in header else [], "chars": len(body)}
        return _toolspec.joined(header, body), meta, True
    return f"TOOL_ERROR unknown tool {name!r}", {"name": name, "status": "unknown_tool"}, False


# --- prompts ------------------------------------------------------------------------------------------
def loop_prompts(record: dict, protocol: str) -> tuple[str, str]:
    """System = the captured `system.append` (or text) behind the loop preamble; user = captured."""
    sysd = record.get("system") or {}
    base = sysd.get("append") if sysd.get("kind") == "preset" else sysd.get("text")
    pre = _ACTION_PREAMBLE if protocol == "action_json" else _LOOP_PREAMBLE
    return f"{pre}\n\n{base or ''}", record.get("user") or ""


def turn_cap(record: dict) -> int:
    """The live call's own max_turns (captured), else the role's config cap."""
    opts = record.get("options") or {}
    if opts.get("max_turns"):
        return int(opts["max_turns"])
    return config.TRIAGE_MAX_TURNS if record.get("role") == "triage" else config.JUDGE_MAX_TURNS


# --- backend chat turns -------------------------------------------------------------------------------
def _ollama_turn(messages: list[dict], *, tools: bool, schema: dict | None, timeout: float = 900.0,
                 backend: str = "ollama") -> dict:
    cfg = rb.ollama_cfg(backend)
    body = {"model": cfg["tag"], "stream": False, "think": cfg["think"], "keep_alive": "20m", "messages": messages,
            "options": {"num_ctx": cfg["num_ctx"], "temperature": 0, "seed": rb.OLLAMA_SEED,
                        "num_predict": 1024 + rb.OLLAMA_THINK_RESERVE}}   # a turn cannot run away
    if tools:
        body["tools"] = TOOLS
    if schema:
        body["format"] = schema
    t0 = time.monotonic()
    try:
        r = httpx.post(f"{rb.OLLAMA_URL}/api/chat", json=body, timeout=timeout)
    except Exception as e:
        return {"status": "error", "reason": "transport", "text": f"{type(e).__name__}: {e}", "duration_ms": 0}
    ms = int((time.monotonic() - t0) * 1000)
    if r.status_code != 200:
        return {"status": "error", "reason": "error", "text": r.text[:500], "duration_ms": ms}
    data = r.json()
    msg = data.get("message") or {}
    return {"status": "ok", "message": msg, "text": msg.get("content") or "", "thinking": msg.get("thinking"),
            "tool_calls": msg.get("tool_calls") or [], "duration_ms": ms,
            "tokens": {"input": data.get("prompt_eval_count"), "output": data.get("eval_count")}}


def _apple_turn(messages: list[dict], *, schema: dict | None, timeout: float = 180.0) -> dict:
    body = {"model": "system", "stream": False, "temperature": 0, "messages": messages}
    if schema:
        body["response_format"] = {"type": "json_schema", "json_schema": {"name": "action", "schema": schema}}
    t0 = time.monotonic()
    try:
        r = httpx.post(f"{rb.FM_URL}/v1/chat/completions", json=body, timeout=timeout)
    except Exception as e:
        return {"status": "error", "reason": "transport", "text": f"{type(e).__name__}: {e}", "duration_ms": 0}
    ms = int((time.monotonic() - t0) * 1000)
    if r.status_code != 200:
        try:
            msg = str((r.json().get("error") or {}).get("message") or "")
        except Exception:
            msg = r.text[:300]
        low = msg.lower()
        reason = ("context_exceeded" if "context" in low and ("exceed" in low or "size" in low)
                  else "guardrail" if "guardrail" in low else "refusal" if "refus" in low else "error")
        return {"status": "skipped" if reason == "context_exceeded" else "error", "reason": reason,
                "text": msg, "duration_ms": ms}
    data = r.json()
    m = ((data.get("choices") or [{}])[0]).get("message") or {}
    usage = data.get("usage") or {}
    if m.get("refusal"):
        return {"status": "error", "reason": "refusal", "text": str(m.get("refusal")), "duration_ms": ms}
    return {"status": "ok", "message": m, "text": m.get("content") or "", "duration_ms": ms,
            "tokens": {"input": usage.get("prompt_tokens"), "output": usage.get("completion_tokens")}}


def _apple_fits(messages: list[dict], reserve: int) -> tuple[bool, int | None]:
    """Whole-conversation token fit against Apple's 8k, via `fm count-tokens` (system + the rest)."""
    system = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
    rest = "\n\n".join(f"[{m['role']}] {m.get('content') or ''}" for m in messages[1:])
    n = rb.fm_count_tokens(system, rest)
    if n is None:
        return False, None
    return n + reserve <= rb.APPLE_CONTEXT, n


def _parse_action(text: str) -> dict | None:
    from scout.generate import _extract_json
    try:
        d = _extract_json(text or "")
    except Exception:
        return None
    return d if isinstance(d, dict) and d.get("action") in ("search", "fetch_page", "answer") else None


# --- the loop ------------------------------------------------------------------------------------------
def run_loop(record: dict, backend: str, *, rep: int = 0) -> dict:
    """Run one tools-on call locally. Returns the drive_replay result shape plus the loop fields."""
    if backend not in TOOL_PROTOCOL:
        raise UnsupportedBackend(f"loop mode runs on {sorted(TOOL_PROTOCOL)} only, not {backend!r}")
    protocol = TOOL_PROTOCOL[backend]
    role = record.get("role")
    system, user = loop_prompts(record, protocol)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    cap = turn_cap(record)
    reserve = rolespecs.output_reserve(role)
    lag_h = _lag_hours(record.get("run_ts"))
    tool_log: list[dict] = []
    loop = {"tool_protocol": protocol, "turns": 0, "turn_at_exceed": None, "tool_calls": tool_log,
            "tool_call_parse_ok": True, "tool_arg_schema_ok": True, "tool_failure": False,
            "hosts": [], "replay_lag_hours": lag_h, "format_retry": False, "rep": rep, "budget": dict(LOOP_BUDGET),
            "repeated_tool_calls": 0}
    seen_calls: dict[str, str] = {}

    def run_tool(name, args):
        """Identical (name, args) calls are answered from the first result with a nudge, never
        re-executed: a looping small model must not burn the budget or rate-limit the engines."""
        key = json.dumps([name, {k: args.get(k) for k in ("query", "url")}], sort_keys=True)
        if key in seen_calls:
            loop["repeated_tool_calls"] += 1
            return (f"REPEATED CALL (identical to an earlier one; same result). Use a DIFFERENT query or "
                    f"URL, or give the final answer now.\n{seen_calls[key]}",
                    {"name": name, "status": "repeated", "repeat_of": key}, True)
        text, meta, ok = _run_tool(name, args)
        if ok:
            seen_calls[key] = text
        return text, meta, ok
    t0 = time.monotonic()
    tokens_in = tokens_out = 0
    thinking_parts: list[str] = []
    final_text: str | None = None
    status, reason = "ok", None
    n_tools = 0

    def _finish(**kw) -> dict:
        r = rb._result(status=status, reason=reason, text=final_text, duration_ms=int((time.monotonic() - t0) * 1000),
                       tokens={"input": tokens_in, "output": tokens_out, "thinking": None},
                       thinking="\n---\n".join(thinking_parts) or None,
                       backend_model="SystemLanguageModel" if backend == "apple_ondevice" else rb.ollama_cfg(backend)["tag"],
                       backend_version=(rb.apple_version() if backend == "apple_ondevice" else rb.ollama_version(backend)),
                       schema_enforced=protocol == "action_json",
                       reasoning="unsupported" if backend == "apple_ondevice" else "thinking")
        r.update(kw)
        loop["hosts"] = sorted({h for t in tool_log for h in (t.get("hosts") or []) if h})
        loop["tool_failure"] = _tool_failure(tool_log)
        r["loop"] = loop
        r["mode"] = "loop"
        return r

    for turn in range(1, cap + 1):
        loop["turns"] = turn
        if backend == "apple_ondevice":
            ok, n = _apple_fits(messages, reserve)
            if not ok:
                status, reason = "skipped", ("context_exceeded" if n is not None else "token_count_failed")
                loop["turn_at_exceed"] = turn
                return _finish(observed_token_count=n)
            step = _apple_turn(messages, schema=ACTION_SCHEMA)
        else:
            step = _ollama_turn(messages, tools=True, schema=None, backend=backend)
        if step.get("status") != "ok":
            status, reason = step.get("status", "error"), step.get("reason", "error")
            final_text = step.get("text")
            if reason == "context_exceeded":
                loop["turn_at_exceed"] = turn
            return _finish()
        tk = step.get("tokens") or {}
        tokens_in += tk.get("input") or 0
        tokens_out += tk.get("output") or 0
        if step.get("thinking"):
            thinking_parts.append(str(step["thinking"]))

        # --- decide: tool call(s) or final answer ---------------------------------------------
        if protocol == "native":
            calls = step.get("tool_calls") or []
            if not calls:
                final_text = step.get("text") or ""
                break
            messages.append(step["message"])
            for tc in calls:
                fn = (tc.get("function") or {})
                name, args = fn.get("name"), fn.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        loop["tool_call_parse_ok"] = False
                        args = {}
                if not isinstance(args, dict):
                    loop["tool_call_parse_ok"] = False
                    args = {}
                text, meta, arg_ok = run_tool(name, args)
                loop["tool_arg_schema_ok"] = loop["tool_arg_schema_ok"] and arg_ok
                meta["turn"] = turn
                tool_log.append(meta)
                n_tools += 1
                messages.append({"role": "tool", "content": text, "tool_name": name})
        else:
            act = _parse_action(step.get("text") or "")
            if act is None:
                loop["tool_call_parse_ok"] = False
                status, reason = "error", "parse_fail"
                final_text = step.get("text")
                return _finish()
            if act["action"] == "answer":
                final_text = act.get("answer") if isinstance(act.get("answer"), str) else json.dumps(act.get("answer"))
                break
            messages.append({"role": "assistant", "content": step.get("text") or json.dumps(act)})
            text, meta, arg_ok = run_tool(act["action"], act)
            loop["tool_arg_schema_ok"] = loop["tool_arg_schema_ok"] and arg_ok
            meta["turn"] = turn
            tool_log.append(meta)
            n_tools += 1
            messages.append({"role": "user", "content": f"[{act['action']} result]\n{text}"})
        if n_tools >= LOOP_BUDGET["max_tool_calls"]:
            messages.append({"role": "user", "content": "Tool budget exhausted. Give the final answer now, "
                                                        "in exactly the format the task asks for."})
    else:
        # turn cap hit without a final answer: one last no-tools turn asking for the answer
        messages.append({"role": "user", "content": "Turn limit reached. Give the final answer now, in exactly "
                                                    "the format the task asks for."})
        step = (_apple_turn(messages, schema=None) if backend == "apple_ondevice"
                else _ollama_turn(messages, tools=False, schema=rolespecs.role_schema(role), backend=backend))
        loop["turns"] = cap + 1
        if step.get("status") != "ok":
            status, reason = step.get("status", "error"), step.get("reason", "error")
            final_text = step.get("text")
            return _finish()
        final_text = step.get("text") or ""

    # Ollama: a plain-text final that does not parse gets ONE schema-constrained re-ask (recorded).
    if rb.is_ollama(backend) and final_text is not None:
        parse = (rolespecs.spec(role) or {}).get("parse")
        if parse and parse(final_text) is None:
            loop["format_retry"] = True
            messages.append({"role": "assistant", "content": final_text})
            messages.append({"role": "user", "content": "Return that final answer again as the JSON the task specifies, nothing else."})
            step = _ollama_turn(messages, tools=False, schema=rolespecs.role_schema(role), backend=backend)
            if step.get("status") == "ok":
                final_text = step.get("text") or final_text
                tk = step.get("tokens") or {}
                tokens_in += tk.get("input") or 0
                tokens_out += tk.get("output") or 0
    return _finish()


def _tool_failure(tool_log: list[dict]) -> bool:
    """A run whose searches ALL returned nothing while engines reported errors is a tool failure, not
    a model result: excluded from agreement/kappa, counted under its own bar (plan C, R13)."""
    searches = [t for t in tool_log if t.get("name") == "search" and t.get("status") != "repeated"]
    if not searches:
        return False
    all_empty = all((t.get("result_count") or 0) == 0 for t in searches)
    engine_errors = any(t.get("unresponsive_engines") or str(t.get("status", "")).startswith(("transport", "http"))
                        for t in searches)
    return all_empty and engine_errors


def _lag_hours(run_ts: str | None) -> float | None:
    try:
        return round((datetime.now() - datetime.fromisoformat(run_ts)).total_seconds() / 3600, 1)
    except Exception:
        return None


def lag_slice(hours: float | None) -> str:
    if hours is None:
        return "unknown"
    return "<24h" if hours < 24 else "24-72h" if hours < 72 else ">72h"
