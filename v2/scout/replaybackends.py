"""Replay backends for the on-device model comparison (2026-09-28).

`drive_replay(record, backend)` sends a captured call record (scout/calllog.py, the SAME record type
the live hook builds) to a candidate model and returns a uniform result dict. Backends:

  apple_ondevice  Apple's on-device SystemLanguageModel via macOS 27's `fm serve` chat-completions
                  server (loopback). stream:false, temperature:0, response_format json_schema per
                  role. Fit is pre-flighted with `fm count-tokens` (exact); a call that does not fit
                  is persisted as `context_exceeded` and never sent. No reasoning level exists on
                  this model (verified 2026-09-28); guardrails are not settable over HTTP (default).
  ollama          Magistral Small 1.2 24B (thinking on) via `ollama serve` on loopback. num_ctx
                  16384, temperature 0, seed, `format` = the role schema; pre-flight estimate with a
                  thinking reserve; the server log is scanned for silent prompt truncation.
  anthropic       reference self-consistency re-run through generate._drive (the same call path as
                  live). Costs money: refuses unless allow_spend=True.

Every result: {status: ok|skipped|error, reason, text, thinking, cost_usd (0.0 for local), duration_ms,
tokens {input, output, thinking}, backend_model, backend_version {...}, observed_token_count,
schema_enforced, reasoning}. Unknown backend names raise.
"""
import json
import os
import re
import subprocess
import sys
import time

import httpx

from scout import config, rolespecs

# Ollama arms (2026-09-30): one backend NAME per model, each its own results folder, scorecard
# column and streak. "ollama" stays Magistral's name so its labels, snapshots and streaks hold.
# Names carry no dot (modelcompare.load_results skips dotted folders). Per-arm env overrides:
# SCOUT_OLLAMA_TAG_<KEY>, SCOUT_OLLAMA_NUM_CTX_<KEY> where KEY = the name upper-cased minus
# "OLLAMA_" ("MAGISTRAL" for the legacy name; the legacy SCOUT_OLLAMA_TAG/NUM_CTX still apply to it).
def _arm(key: str, tag: str, vendor: str, num_ctx: int = 49152, chars_per_token: float = 3.0,
         think: bool = True, think_reserve: int = 2048) -> dict:
    # think_reserve: output tokens a model may spend reasoning before its answer, on top of the
    # role's output reserve; together they bound generation (num_predict) and feed the fit check.
    # Magistral answers within 2048; Gemma 4 and Nemotron spent all of 4096 thinking on a research
    # prompt and returned nothing (2026-09-30 smoke), so they get room, still bounded (~5 min).
    return {"tag": os.environ.get(f"SCOUT_OLLAMA_TAG_{key}", tag), "vendor": vendor,
            "num_ctx": int(os.environ.get(f"SCOUT_OLLAMA_NUM_CTX_{key}", num_ctx)),
            "chars_per_token": chars_per_token, "think": think,
            "think_reserve": int(os.environ.get(f"SCOUT_OLLAMA_THINK_RESERVE_{key}", think_reserve))}


OLLAMA_MODELS = {
    "ollama": _arm("MAGISTRAL", os.environ.get("SCOUT_OLLAMA_TAG", "magistral:24b"), "Mistral AI",
                   num_ctx=int(os.environ.get("SCOUT_OLLAMA_NUM_CTX", "16384"))),
    # NVIDIA's 30B MoE builds (Lightning, Cascade 2, Nano 30B) are 23-25 GB on disk at Q4 and cannot
    # sit fully on a 24 GB box; Nano 4B (q8, 4 GB, 256k ctx) is NVIDIA's edge-class model and fits.
    "ollama_nemotron": _arm("NEMOTRON", "nemotron-3-nano:4b-q8_0", "NVIDIA", think_reserve=6144),
    # Gemma 4 26B is a 26B/4B-active MoE; the QAT build is 16 GB, Magistral's footprint.
    "ollama_gemma4": _arm("GEMMA4", "gemma4:26b-a4b-it-qat", "Google", think_reserve=6144),
}
BACKENDS = ("apple_ondevice", *OLLAMA_MODELS, "anthropic")
LOCAL_BACKENDS = ("apple_ondevice", *OLLAMA_MODELS)


def is_ollama(backend: str) -> bool:
    return backend in OLLAMA_MODELS


def ollama_cfg(backend: str) -> dict:
    return OLLAMA_MODELS[backend]

APPLE_CONTEXT = 8192
# The bridge counts prompt tokens exactly, but the session transcript the model keeps (system +
# user + its own output, plus schema framing) ran past the window on a prompt the count said fit
# (2026-09-30, an ask_verify call: the bridge then shut down and took the arm with it). Keep a
# margin below the window on top of the role's output reserve.
APPLE_SAFETY_MARGIN = int(os.environ.get("SCOUT_APPLE_SAFETY_MARGIN", "1024"))
FM_URL = os.environ.get("SCOUT_FM_URL", "http://127.0.0.1:18765")
OLLAMA_URL = os.environ.get("SCOUT_OLLAMA_URL", "http://127.0.0.1:11435")
OLLAMA_TAG = OLLAMA_MODELS["ollama"]["tag"]            # Magistral's (legacy aliases; arms read ollama_cfg)
OLLAMA_NUM_CTX = OLLAMA_MODELS["ollama"]["num_ctx"]
OLLAMA_THINK_RESERVE = 2048
OLLAMA_SEED = 7
OLLAMA_CHARS_PER_TOKEN = 3.0
_EXACT_FIELDS = ("system", "user", "result.text")


class UnknownBackend(ValueError):
    pass


# --- prompt assembly --------------------------------------------------------------------------------
def exact_prompt(record: dict) -> tuple[str, str]:
    """(system, user) for an exact replay: byte-identical to what the live model saw."""
    sysd = record.get("system") or {}
    system = sysd.get("text") if sysd.get("kind") == "string" else (sysd.get("append") or "")
    return system or "", record.get("user") or ""


def frozen_prompt(record: dict) -> tuple[str, str]:
    """Judgment-only control for a tools-on record: the Scout instruction (system.append) + the
    captured user prompt + the recorded tool calls/results inlined in order, tools off."""
    system, user = exact_prompt(record)
    rows = record.get("transcript") or []
    parts = []
    for r in rows:
        if r.get("kind") == "tool_use":
            parts.append(f"[tool_use {r.get('name')}] {json.dumps(r.get('input'), ensure_ascii=False)}")
        elif r.get("kind") == "tool_result":
            parts.append(f"[tool_result {r.get('tool_use_id')}]\n{r.get('content') or ''}")
    frame = ("\n\nYou have NO tools in this replay. The tool calls and results below were observed "
             "during the original run; treat them as your only evidence and answer in the same "
             "output format.")
    return system + frame, user + "\n\nOBSERVED TOOL ACTIVITY:\n" + "\n\n".join(parts)


def _truncated_exact(record: dict) -> bool:
    return any(t.split("[")[0] in _EXACT_FIELDS or t in _EXACT_FIELDS for t in record.get("truncated") or [])


# --- token fit ---------------------------------------------------------------------------------------
def fm_count_tokens(system: str, user: str) -> int | None:
    """Exact on-device token count via `fm count-tokens` (equals prompt_tokens). None on failure."""
    try:
        out = subprocess.run(["fm", "count-tokens", "-q", "-i", system, "--text", user],
                             capture_output=True, text=True, timeout=60)
        m = re.search(r"(\d+)\s*$", out.stdout.strip())
        return int(m.group(1)) if m else None
    except Exception as e:
        print(f"[replay] fm count-tokens failed ({type(e).__name__}: {e})", file=sys.stderr)
        return None


def fits(record: dict, backend: str, system: str, user: str) -> tuple[bool, str | None, int | None]:
    """(fits, reason, observed_token_count)."""
    reserve = rolespecs.output_reserve(record.get("role"))
    if backend == "apple_ondevice":
        n = fm_count_tokens(system, user)
        if n is None:
            return False, "token_count_failed", None
        limit = APPLE_CONTEXT - APPLE_SAFETY_MARGIN
        return (n + reserve <= limit), ("context_exceeded" if n + reserve > limit else None), n
    if is_ollama(backend):
        cfg = ollama_cfg(backend)
        est = int(len(system + user) / cfg["chars_per_token"])
        ok = est + reserve + cfg["think_reserve"] <= cfg["num_ctx"]
        return ok, (None if ok else "context_exceeded"), est
    return True, None, None


# --- backends ----------------------------------------------------------------------------------------
def _result(**kw) -> dict:
    base = {"status": "ok", "reason": None, "text": None, "thinking": None, "cost_usd": 0.0,
            "duration_ms": None, "tokens": None, "backend_model": None, "backend_version": None,
            "observed_token_count": None, "schema_enforced": False, "reasoning": None}
    base.update(kw)
    return base


def apple_version() -> dict:
    try:
        build = subprocess.run(["sw_vers", "-buildVersion"], capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        build = None
    return {"macos_build": build, "asset": "instruct_3b", "guardrails": "default", "use_case": "general",
            "context": APPLE_CONTEXT}


def _apple_cli(system: str, user: str, guardrails: str = "default", timeout: float = 300.0) -> dict:
    """`fm respond` fallback (greedy, no stream). The CLI can set the guardrail level, which the HTTP
    server cannot; it takes no plain JSON schema, so the strict parser does the constraining."""
    cmd = ["fm", "respond", "--no-stream", "-g", "-i", system, "--text", user]
    if guardrails != "default":
        cmd += ["--guardrails", guardrails]
    t0 = time.monotonic()
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        return _result(status="error", reason="transport", text=f"{type(e).__name__}: {e}")
    ms = int((time.monotonic() - t0) * 1000)
    text = (out.stdout or "").strip()
    err = (out.stderr or "").strip()
    low = (text + " " + err).lower()
    if out.returncode != 0 or not text or "refused" in low or "guardrail" in low:
        reason = "guardrail" if "guardrail" in low else "refusal" if "refus" in low else "error"
        return _result(status="error", reason=reason, text=(err or text)[:500], duration_ms=ms)
    return _result(text=text, duration_ms=ms)


def _apple(record: dict, system: str, user: str, timeout: float = 180.0) -> dict:
    """HTTP first (schema-constrained); on refusal/guardrail, `fm respond` at default guardrails, then
    permissive. The path that answered is recorded in backend_version.via / guardrails (R3)."""
    r = _apple_http(record, system, user, timeout)
    if r.get("reason") not in ("refusal", "guardrail"):
        r["backend_version"] = {**(r.get("backend_version") or apple_version()), "via": "fm_serve"}
        return r
    first = r
    for level in ("default", "permissive-content-transformations"):
        c = _apple_cli(system, user, level)
        if c.get("status") == "ok":
            c.update(backend_model="SystemLanguageModel", schema_enforced=False, reasoning="unsupported",
                     backend_version={**apple_version(), "via": "fm_respond", "guardrails": level,
                                      "http_reason": first.get("reason")})
            c["observed_token_count"] = fm_count_tokens(system, user)
            return c
    first["backend_version"] = {**(first.get("backend_version") or apple_version()), "via": "fm_serve",
                                "cli_fallback": "refused_at_permissive_too"}
    return first


def _apple_http(record: dict, system: str, user: str, timeout: float = 180.0) -> dict:
    role = record.get("role")
    schema = rolespecs.role_schema(role)
    body = {"model": "system", "stream": False, "temperature": 0,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if schema:
        body["response_format"] = {"type": "json_schema", "json_schema": {"name": role or "verdict", "schema": schema}}
    t0 = time.monotonic()
    try:
        r = httpx.post(f"{FM_URL}/v1/chat/completions", json=body, timeout=timeout)
    except Exception as e:
        return _result(status="error", reason="transport", text=f"{type(e).__name__}: {e}",
                       backend_model="SystemLanguageModel", backend_version=apple_version(), reasoning="unsupported")
    ms = int((time.monotonic() - t0) * 1000)
    ver = apple_version()
    if r.status_code != 200:
        msg = ""
        try:
            msg = str((r.json().get("error") or {}).get("message") or "")
        except Exception:
            msg = r.text[:300]
        low = msg.lower()
        reason = ("context_exceeded" if "context" in low and ("exceed" in low or "size" in low)
                  else "guardrail" if "guardrail" in low
                  else "refusal" if "refus" in low
                  else "rate_limited" if "rate" in low and "limit" in low
                  else "error")
        return _result(status="skipped" if reason == "context_exceeded" else "error", reason=reason, text=msg,
                       duration_ms=ms, backend_model="SystemLanguageModel", backend_version=ver,
                       schema_enforced=bool(schema), reasoning="unsupported")
    data = r.json()
    ch = (data.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    usage = data.get("usage") or {}
    if msg.get("refusal"):
        return _result(status="error", reason="refusal", text=str(msg.get("refusal")), duration_ms=ms,
                       backend_model="SystemLanguageModel", backend_version=ver, schema_enforced=bool(schema),
                       observed_token_count=usage.get("prompt_tokens"), reasoning="unsupported")
    return _result(text=msg.get("content") or "", duration_ms=ms,
                   tokens={"input": usage.get("prompt_tokens"), "output": usage.get("completion_tokens"),
                           "thinking": ((usage.get("completion_tokens_details") or {}).get("reasoning_tokens"))},
                   backend_model="SystemLanguageModel", backend_version=ver, schema_enforced=bool(schema),
                   observed_token_count=usage.get("prompt_tokens"), reasoning="unsupported")


def ollama_version(backend: str = "ollama") -> dict:
    cfg = ollama_cfg(backend)
    base = {"tag": cfg["tag"], "num_ctx": cfg["num_ctx"], "thinking": cfg["think"], "vendor": cfg["vendor"]}
    try:
        r = httpx.post(f"{OLLAMA_URL}/api/show", json={"model": cfg["tag"]}, timeout=30).json()
        det = r.get("details") or {}
        return {**base, "digest": (r.get("modelinfo") or {}).get("general.uuid") or r.get("digest"),
                "quant": det.get("quantization_level"), "family": det.get("family"),
                "parameter_size": det.get("parameter_size")}
    except Exception:
        return base


def ollama_resident(backend: str = "ollama") -> dict | None:
    """/api/ps row for the arm's tag (None if not loaded). The arm aborts unless size_vram == size."""
    tag = ollama_cfg(backend)["tag"]
    try:
        for m in (httpx.get(f"{OLLAMA_URL}/api/ps", timeout=15).json().get("models") or []):
            if m.get("name") == tag or m.get("model") == tag:
                return m
    except Exception:
        return None
    return None


def ollama_unload(backend: str = "ollama") -> None:
    try:
        httpx.post(f"{OLLAMA_URL}/api/generate", json={"model": ollama_cfg(backend)["tag"], "keep_alive": 0}, timeout=60)
    except Exception:
        pass


def _ollama(record: dict, system: str, user: str, timeout: float = 900.0, backend: str = "ollama") -> dict:
    role = record.get("role")
    schema = rolespecs.role_schema(role)
    cfg = ollama_cfg(backend)
    # num_predict bounds a runaway generation (2026-09-30: Gemma 4 spent 15+ min on one
    # ask_rewrite call): the role's output reserve plus the thinking reserve, the same budget the
    # fit check already assumed. A cut-off output fails to parse and is scored as such.
    body = {"model": cfg["tag"], "stream": False, "think": cfg["think"], "keep_alive": "20m",
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "options": {"num_ctx": cfg["num_ctx"], "temperature": 0, "seed": OLLAMA_SEED,
                        "num_predict": rolespecs.output_reserve(role) + cfg["think_reserve"]}}
    if schema:
        body["format"] = schema
    reasoning = "thinking" if cfg["think"] else "none"
    t0 = time.monotonic()
    try:
        r = httpx.post(f"{OLLAMA_URL}/api/chat", json=body, timeout=timeout)
    except Exception as e:
        return _result(status="error", reason="transport", text=f"{type(e).__name__}: {e}",
                       backend_model=cfg["tag"], backend_version=ollama_version(backend), reasoning=reasoning)
    ms = int((time.monotonic() - t0) * 1000)
    if r.status_code != 200:
        return _result(status="error", reason="error", text=r.text[:500], duration_ms=ms,
                       backend_model=cfg["tag"], backend_version=ollama_version(backend), reasoning=reasoning)
    data = r.json()
    msg = data.get("message") or {}
    # a model that returns no `thinking` field (or thinks inline) still yields its content
    return _result(text=msg.get("content") or "", thinking=msg.get("thinking"), duration_ms=ms,
                   tokens={"input": data.get("prompt_eval_count"), "output": data.get("eval_count"),
                           "thinking": None},
                   backend_model=cfg["tag"], backend_version=ollama_version(backend), schema_enforced=bool(schema),
                   observed_token_count=data.get("prompt_eval_count"), reasoning=reasoning)


def _anthropic(record: dict, system: str, user: str, allow_spend: bool) -> dict:
    if not allow_spend:
        return _result(status="skipped", reason="spend_blocked", backend_model=record.get("model"))
    import asyncio
    from claude_agent_sdk import ClaudeAgentOptions
    from scout.generate import _drive
    opts = record.get("options") or {}
    options = ClaudeAgentOptions(model=record.get("model"), system_prompt=system, mcp_servers={},
                                 allowed_tools=[], disallowed_tools=["WebSearch", "WebFetch"],
                                 permission_mode="bypassPermissions",
                                 max_turns=opts.get("max_turns") or config.JUDGE_MAX_TURNS,
                                 max_budget_usd=opts.get("max_budget_usd") or config.JUDGE_MAX_BUDGET_USD)
    t0 = time.monotonic()
    res = asyncio.run(_drive(user, options, record.get("role") or "replay"))
    return _result(text=res.get("text"), cost_usd=res.get("cost_usd") or 0.0,
                   duration_ms=int((time.monotonic() - t0) * 1000), backend_model=record.get("model"),
                   backend_version={"model": record.get("model")}, reasoning="live-default")


def drive_replay(record: dict, backend: str, *, mode: str = "exact", allow_spend: bool = False) -> dict:
    if backend not in BACKENDS:
        raise UnknownBackend(f"unknown backend {backend!r}; choose from {BACKENDS}")
    if _truncated_exact(record):
        return _result(status="skipped", reason="truncated")
    if mode == "frozen":
        system, user = frozen_prompt(record)
    else:
        system, user = exact_prompt(record)
    if backend in LOCAL_BACKENDS:
        ok, reason, n = fits(record, backend, system, user)
        if not ok:
            if backend == "apple_ondevice":
                return _result(status="skipped", reason=reason, observed_token_count=n,
                               backend_model="SystemLanguageModel", backend_version=apple_version(),
                               reasoning="unsupported")
            cfg = ollama_cfg(backend)
            return _result(status="skipped", reason=reason, observed_token_count=n,
                           backend_model=cfg["tag"], backend_version=ollama_version(backend),
                           reasoning="thinking" if cfg["think"] else "none")
    if backend == "apple_ondevice":
        return _apple(record, system, user)
    if is_ollama(backend):
        return _ollama(record, system, user, backend=backend)
    return _anthropic(record, system, user, allow_spend)
