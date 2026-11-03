"""Replay backends for the on-device model comparison (2026-09-28).

`drive_replay(record, backend)` sends a captured call record (scout/calllog.py, the SAME record type
the live hook builds) to a candidate model and returns a uniform result dict. Backends:

  apple_ondevice  Apple's on-device SystemLanguageModel via macOS 27's `fm serve` chat-completions
                  server (loopback). stream:false, temperature:0, response_format json_schema per
                  role. Fit is pre-flighted with `fm count-tokens` (exact); a call that does not fit
                  is persisted as `context_exceeded` and never sent. No reasoning level exists on
                  this model (verified 2026-09-28); guardrails are not settable over HTTP (default).
  ollama*         the Ollama arms (OLLAMA_MODELS: Magistral, Nemotron Nano, Gemma 4) via `ollama serve`
                  on loopback, reasoning on, seed, `format` = the role schema; pre-flight estimate
                  with a reasoning reserve; a call still reasoning at the bound is continued once
                  with its trace closed; the server log is scanned for silent prompt truncation.
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
         think: bool = True, think_reserve: int = 2048, temperature: float = 0.0,
         top_p: float | None = None, top_k: int | None = None, think_level: str | None = None,
         system_prefix: str | None = None) -> dict:
    # think_reserve: output tokens a model may spend reasoning before its answer, on top of the
    # role's output reserve; together they bound generation (num_predict) and feed the fit check.
    # A call still reasoning at that bound is continued once with the trace closed (see _ollama).
    # temperature / top_p / top_k: the arm's sampling. Magistral keeps temperature 0; the two
    # small models run their vendor's recommended reasoning-mode sampling (2026-11-02). Per-arm env:
    # SCOUT_OLLAMA_THINK_<KEY> (1/0), SCOUT_OLLAMA_TEMPERATURE_<KEY>, SCOUT_OLLAMA_TOP_P_<KEY>,
    # SCOUT_OLLAMA_TOP_K_<KEY>, SCOUT_OLLAMA_THINK_RESERVE_<KEY> (smoke tests; the lane sets none).
    def _f(name, default, cast):
        raw = os.environ.get(f"SCOUT_OLLAMA_{name}_{key}")
        if raw is None or raw == "":
            return default
        return cast(raw)
    think = _f("THINK", think, lambda v: v.strip().lower() in ("1", "true", "yes", "on"))
    return {"tag": os.environ.get(f"SCOUT_OLLAMA_TAG_{key}", tag), "vendor": vendor,
            "num_ctx": int(os.environ.get(f"SCOUT_OLLAMA_NUM_CTX_{key}", num_ctx)),
            "chars_per_token": chars_per_token, "think": think,
            "think_reserve": _f("THINK_RESERVE", think_reserve, int),
            "temperature": _f("TEMPERATURE", temperature, float),
            "top_p": _f("TOP_P", top_p, float), "top_k": _f("TOP_K", top_k, int),
            # a reasoning LEVEL for models that take one (gpt-oss: low / medium / high); sent as the
            # `think` value in place of the boolean; part of the settings key
            "think_level": _f("THINK_LEVEL", think_level, str),
            # text prepended to Scout's system prompt for a model whose reasoning is triggered by its
            # own system prompt (Magistral); part of the settings key as a hash
            "system_prefix": system_prefix}


# Mistral's own default system prompt for Magistral, verbatim from the Ollama model's `system` layer
# (magistral:24b). It is what makes the model open a <think> block. Scout's judge/challenger prompts
# REPLACE the system prompt, so with it absent Magistral never reasoned: 910 of 910 stored answers
# carry an empty trace (found 2026-11-02). Public vendor text, not Scout methodology.
MAGISTRAL_SYSTEM = (
    "A user will ask you to solve a task. You should first draft your thinking process (inner monologue) "
    "until you have derived the final answer. Afterwards, write a self-contained summary of your thoughts "
    "(i.e. your summary should be succinct but contain all the critical steps you needed to reach the "
    "conclusion). You should use Markdown and Latex to format your response. Write both your thoughts and "
    "summary in the same language as the task posed by the user.\n\nYour thinking process must follow the "
    "template below:\n<think>\nYour thoughts or/and draft, like working through an exercise on scratch paper. "
    "Be as casual and as long as you want until you are confident to generate a correct answer.\n</think>")

# Arms retired from the nightly rotation keep their history: the scorecard reads their rows under the
# settings they last ran with (2026-11-02: Nemotron 3 Nano 4B retired, Bolt E4B is NVIDIA's edge arm).
RETIRED_OLLAMA = {
    "ollama_nemotron": {"tag": "nemotron-3-nano:4b-q8_0", "num_ctx": 49152, "thinking": False, "vendor": "NVIDIA"},
}

OLLAMA_MODELS = {
    # Defaults are the LANE's settings (2026-11-02): the period filter compares stored results with
    # these from any process (the nightly read-out, the 1st/15th check-in Action, the canary), so a
    # value that only lives in ~/scout-replay/env would set every real result aside as history.
    # Magistral: reasoning REAL since 2026-11-02 (Uroš). Mistral's own reasoning instruction goes in
    # front of Scout's prompt (MAGISTRAL_SYSTEM) and the model runs its recommended 0.7 / 0.95; both
    # are in the settings key, so the 948 earlier rows (no trace, temperature 0) are history.
    "ollama": _arm("MAGISTRAL", os.environ.get("SCOUT_OLLAMA_TAG", "magistral:24b"), "Mistral AI",
                   num_ctx=int(os.environ.get("SCOUT_OLLAMA_NUM_CTX", "49152")),     # 49152 since 2026-09-29
                   temperature=0.7, top_p=0.95, system_prefix=MAGISTRAL_SYSTEM),
    # NVIDIA Nemotron 3 Nano 4B ran as the NVIDIA arm 2026-09-30 to 2026-11-02 (reasoning off; the
    # 30B MoE builds, 23-25 GB at Q4, cannot sit fully on a 24 GB box) and is RETIRED in favour of
    # Bolt E4B below (Uroš, 11/2); its 1,117 results stay as history (RETIRED_OLLAMA).
    # Gemma 4 26B is a 26B/4B-active MoE; the QAT build is 16 GB, Magistral's footprint.
    "ollama_gemma4": _arm("GEMMA4", "gemma4:26b-a4b-it-qat", "Google", num_ctx=32768,     # 48k KV spilled off the GPU (9/30)
                          think=True, think_reserve=8192, temperature=1.0, top_p=0.95, top_k=64),
    # OpenAI gpt-oss 20B (21B total, 3.6B active, MXFP4, 13.8 GB; added 2026-11-02, Uroš: "the
    # absolute top that fits"). Reasoning is native and levelled; "medium" is OpenAI's default and
    # what the Ollama template assumes. Temperature 1 is the model's own parameter file; no top_p.
    # Its cache is small (sliding-window attention on half the layers), so 49152 fits in 24 GB.
    "ollama_gptoss": _arm("GPTOSS", "gpt-oss:20b", "OpenAI", think=True, think_reserve=8192,
                          temperature=1.0, think_level="medium"),
    # NVIDIA Nemotron 3.5 Bolt E4B (8B total, 4B effective, Gemma-4-E4B architecture, released
    # 2026-10-23; q8_0 9.8 GB; 64k window): NVIDIA's current edge model and the Nano 4B's successor
    # (its card: MultiChallenge 53 vs 35). Added 2026-11-02 with the Ollama 0.40.13 upgrade (0.34.4
    # refused the pull). Reasoning on; the card and the model's own parameter file say temperature
    # 1.0, top_p 0.95, top_k 64. Smoke 11/2: 10 of 10 answered and parsed, 27 s to 196 s a call.
    "ollama_bolt": _arm("BOLT", "nemotron-3.5-bolt:e4b-q8_0", "NVIDIA", think=True, think_reserve=8192,
                        temperature=1.0, top_p=0.95, top_k=64),
    # NVIDIA Nemotron 3 Nano 30B-A3B, the peer to Gemma 4 26B-A4B (30B total, ~3.5B active, hybrid
    # Mamba-2 MoE). Ollama's own builds are 24 GB (GGUF) or MLX-only (18 GB, gated to 32 GB Macs);
    # this is unsloth's IQ4_XS GGUF, 18.2 GB, which fits once macOS lets the GPU take 21 GB
    # (`sudo sysctl iogpu.wired_limit_mb=21504`, Uroš 2026-11-02; resets at reboot). NVIDIA's Nano
    # card: reasoning on at temperature 0.6, top_p 0.95; budget 8192 like the other arms.
    "ollama_nano30b": _arm("NANO30B", "hf.co/unsloth/Nemotron-3-Nano-30B-A3B-GGUF:IQ4_XS", "NVIDIA",
                           think=True, think_reserve=8192, temperature=0.6, top_p=0.95),
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
FIT_MARGIN = 256            # tokens kept free below num_ctx (chat framing, estimate noise)
MIN_THINK_BUDGET = 512      # a reasoning arm skips a call that leaves less room than this to reason
ESTIMATE_SLACK = 1.15       # the chars/3 estimate ran 0.78 to 1.12 of the server's count (11/2 smoke)
_count_cache: dict = {}
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
        budget, n, _how = ollama_budget(backend, record.get("role"), system, user)
        return budget is not None, (None if budget is not None else "context_exceeded"), n
    return True, None, None


def ollama_count_tokens(backend: str, system: str, user: str) -> int | None:
    """Exact prompt token count from the server: a one-token chat call, reasoning off, no schema
    (this Ollama build has no tokenize endpoint, and num_predict 0 means unlimited). The prompt
    stays in the server's cache, so the real call that follows re-uses the evaluation. None when
    the server does not answer."""
    cfg = ollama_cfg(backend)
    key = (backend, cfg["tag"], hash(system), hash(user))
    if key in _count_cache:
        return _count_cache[key]
    body = {"model": cfg["tag"], "stream": False, "think": False, "keep_alive": "20m",
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "options": {"num_ctx": cfg["num_ctx"], "num_predict": 1, "temperature": 0}}
    n = None
    try:
        r = httpx.post(f"{OLLAMA_URL}/api/chat", json=body, timeout=600)
        if r.status_code == 200:
            n = r.json().get("prompt_eval_count")
    except Exception as e:
        print(f"[replay] token count failed ({type(e).__name__}: {e})", file=sys.stderr)
    _count_cache[key] = n
    return n


def ollama_budget(backend: str, role: str | None, system: str, user: str) -> tuple[int | None, int, str]:
    """(reasoning budget for this call, or None when it cannot run; prompt tokens; how counted).
    The budget adapts to the room the prompt leaves instead of excluding the call (2026-11-02,
    Uroš: no exclusions, September cost two months): a prompt that leaves the role's answer reserve
    plus MIN_THINK_BUDGET runs, with a smaller budget when it must, and the continuation rescues an
    overrun. The common case needs no count (the estimate, with slack, leaves the full reserve);
    a borderline prompt is counted exactly on the server."""
    cfg = ollama_cfg(backend)
    reserve = rolespecs.output_reserve(role)
    est = int(len(system + user) / cfg["chars_per_token"])
    if est * ESTIMATE_SLACK + reserve + FIT_MARGIN + cfg["think_reserve"] <= cfg["num_ctx"]:
        return cfg["think_reserve"], est, "estimate"
    n = ollama_count_tokens(backend, system, user)
    how = "counted"
    if n is None:
        n, how = int(est * ESTIMATE_SLACK), "estimate+slack"
    room = cfg["num_ctx"] - n - reserve - FIT_MARGIN
    if room < (MIN_THINK_BUDGET if cfg["think"] else 0):
        return None, n, how
    return min(cfg["think_reserve"], max(room, 0)), n, how


# --- backends ----------------------------------------------------------------------------------------
def _result(**kw) -> dict:
    base = {"status": "ok", "reason": None, "text": None, "thinking": None, "cost_usd": 0.0,
            "duration_ms": None, "tokens": None, "backend_model": None, "backend_version": None,
            "observed_token_count": None, "schema_enforced": False, "reasoning": None}
    base.update(kw)
    return base


_apple_asset_cache: dict = {}


def apple_asset() -> str:
    """The on-device model `fm` serves as "system", from `fm models` (2026-11-02: after macOS 27.2 the
    line reads "system (AFM 3 Core Advanced)", the 20B sparse model, where the arm had assumed the
    3B "instruct_3b" since 9/28). Recorded in the arm's version so a model change is a new period.
    Falls back to the historical label when fm cannot answer (licence not accepted, older macOS)."""
    if "asset" not in _apple_asset_cache:
        asset = "instruct_3b"
        try:
            out = subprocess.run(["fm", "models"], capture_output=True, text=True, timeout=20).stdout
            m = re.search(r"system\s*\(([^)]+)\)", re.sub(r"\x1b\[[0-9;]*m", "", out))
            if m:
                asset = m.group(1).strip()
        except Exception:
            pass
        _apple_asset_cache["asset"] = asset
    return _apple_asset_cache["asset"]


def apple_version() -> dict:
    try:
        build = subprocess.run(["sw_vers", "-buildVersion"], capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        build = None
    return {"macos_build": build, "asset": apple_asset(), "guardrails": "default", "use_case": "general",
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


def settings_key(backend: str) -> dict:
    """The settings that define an Ollama arm's period: tag, context, reasoning, vendor and, for an
    arm that samples (temperature above 0 or top_p / top_k set), its sampling and reasoning budget.
    An arm at temperature 0 with no top_p / top_k carries no `sampling` block, so Magistral's key is
    byte-identical to every result it has already written (2026-11-02)."""
    cfg = ollama_cfg(backend)
    key = {"tag": cfg["tag"], "num_ctx": cfg["num_ctx"], "thinking": cfg["think"], "vendor": cfg["vendor"]}
    if cfg["temperature"] or cfg["top_p"] is not None or cfg["top_k"] is not None or cfg["think_level"]:
        key["sampling"] = {"temperature": cfg["temperature"], "top_p": cfg["top_p"], "top_k": cfg["top_k"],
                           "think_reserve": cfg["think_reserve"]}
        if cfg["think_level"]:
            key["sampling"]["think_level"] = cfg["think_level"]
    if cfg.get("system_prefix"):
        import hashlib
        key["system_prefix"] = hashlib.sha256(cfg["system_prefix"].encode()).hexdigest()[:12]
    return key


def is_current_period(result: dict) -> bool:
    """True unless the result is an Ollama arm's and was written under settings other than the
    arm's current ones (a different tag, context, reasoning flag or sampling). The scorecard keeps
    only current-period rows per arm, so a settings change never pools with what came before
    (2026-11-02: the doc said "never pooled" while the window held Magistral's 16k and 48k results,
    and the 9/30 reasoning-on smoke rows, side by side)."""
    backend = result.get("backend")
    if backend in RETIRED_OLLAMA:
        bv = result.get("backend_version") or {}
        if not bv.get("tag"):
            return True
        cur = RETIRED_OLLAMA[backend]
        return all(bv.get(k) == v for k, v in cur.items()) and "sampling" not in bv
    if backend not in OLLAMA_MODELS:
        return True
    bv = result.get("backend_version") or {}
    if not bv.get("tag"):                  # no settings on record (fixtures): kept, never dropped unseen
        return True
    cur = settings_key(backend)
    return all(bv.get(k) == v for k, v in cur.items()) and ("sampling" in cur or "sampling" not in bv)


def ollama_server_version() -> str | None:
    """The running server's version (provenance only; an engine upgrade is recorded on every result
    but is NOT part of the settings key, or no upgrade could ever happen without resetting every
    arm; 2026-11-02, the 0.34.4 -> 0.40.13 upgrade for Bolt)."""
    try:
        return httpx.get(f"{OLLAMA_URL}/api/version", timeout=10).json().get("version")
    except Exception:
        return None


def ollama_version(backend: str = "ollama") -> dict:
    cfg = ollama_cfg(backend)
    base = settings_key(backend)
    try:
        r = httpx.post(f"{OLLAMA_URL}/api/show", json={"model": cfg["tag"]}, timeout=30).json()
        det = r.get("details") or {}
        return {**base, "digest": (r.get("modelinfo") or {}).get("general.uuid") or r.get("digest"),
                "quant": det.get("quantization_level"), "family": det.get("family"),
                "parameter_size": det.get("parameter_size"), "server": ollama_server_version()}
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


BUDGET_NUDGE = ("Your reasoning budget is spent; the trace above is everything you worked out. "
                "Answer now, in the required output format, without further reasoning.")


def _cut_trace(thinking: str) -> str:
    """NVIDIA's budget control ends the trace at the next newline after the budget; we have the trace
    up to the bound, so cut back to its last newline (never below half of it)."""
    cut = thinking.rfind("\n")
    return thinking[:cut] if cut >= len(thinking) // 2 else thinking


def _ollama(record: dict, system: str, user: str, timeout: float = 900.0, backend: str = "ollama",
            budget: tuple | None = None) -> dict:
    role = record.get("role")
    schema = rolespecs.role_schema(role)
    cfg = ollama_cfg(backend)
    reserve = rolespecs.output_reserve(role)
    # num_predict bounds a runaway generation (2026-09-30: Gemma 4 spent 15+ min on one
    # ask_rewrite call): the role's output reserve plus this call's reasoning budget (the arm's
    # reserve, or less when the prompt leaves less room: ollama_budget).
    think_budget, prompt_n, counted = budget or ollama_budget(backend, role, system, user)
    if think_budget is None:
        return _result(status="skipped", reason="context_exceeded", observed_token_count=prompt_n,
                       backend_model=cfg["tag"], backend_version=ollama_version(backend),
                       reasoning="thinking" if cfg["think"] else "none")
    options = {"num_ctx": cfg["num_ctx"], "temperature": cfg["temperature"], "seed": OLLAMA_SEED,
               "num_predict": reserve + think_budget}
    if cfg["top_p"] is not None:
        options["top_p"] = cfg["top_p"]
    if cfg["top_k"] is not None:
        options["top_k"] = cfg["top_k"]
    if cfg.get("system_prefix"):
        system = cfg["system_prefix"] + "\n\n" + system
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    think_value = cfg["think_level"] if (cfg["think"] and cfg["think_level"]) else cfg["think"]
    body = {"model": cfg["tag"], "stream": False, "think": think_value, "keep_alive": "20m",
            "messages": messages, "options": options}
    if schema:
        body["format"] = schema
    reasoning = "thinking" if cfg["think"] else "none"
    ver = ollama_version(backend)
    t0 = time.monotonic()
    try:
        r = httpx.post(f"{OLLAMA_URL}/api/chat", json=body, timeout=timeout)
    except Exception as e:
        return _result(status="error", reason="transport", text=f"{type(e).__name__}: {e}",
                       backend_model=cfg["tag"], backend_version=ver, reasoning=reasoning)
    ms = int((time.monotonic() - t0) * 1000)
    if r.status_code != 200:
        return _result(status="error", reason="error", text=r.text[:500], duration_ms=ms,
                       backend_model=cfg["tag"], backend_version=ver, reasoning=reasoning)
    data = r.json()
    msg = data.get("message") or {}
    content, thinking = msg.get("content") or "", msg.get("thinking")
    done_reason = data.get("done_reason")
    prompt_tokens, out_tokens = data.get("prompt_eval_count"), data.get("eval_count")
    budget_hit = continuation = None
    # Reasoning budget (2026-11-02): the generation bound ran out while the model was reasoning
    # (done_reason "length", no content) or after a long trace left no room for the answer (the
    # smoke's judge call: trace done, JSON cut mid-way). NVIDIA's budget control closes the trace
    # and lets the model answer; Ollama has no budget and treats no trailing assistant message as
    # a prefill, so the trace goes back as the assistant's thinking in its own turn (the renderer
    # puts it in the model's think block), reasoning off, and the model answers under the schema
    # with the role's full output reserve; a partial answer is discarded. One continuation, never a
    # second; its tokens and time are added.
    if cfg["think"] and done_reason == "length" and thinking:
        budget_hit = True
        trace = _cut_trace(thinking)
        # A levelled model (gpt-oss) keeps reasoning with think=False and answered nothing in the
        # 11/2 forced pass; it continues at its lowest level with room for a short trace instead.
        follow_think = "low" if cfg["think_level"] else False
        follow = {**body, "think": follow_think,
                  "messages": messages + [{"role": "assistant", "content": "", "thinking": trace},
                                          {"role": "user", "content": BUDGET_NUDGE}],
                  "options": {**options, "num_predict": reserve + (1024 if cfg["think_level"] else 0)}}
        t1 = time.monotonic()
        try:
            r2 = httpx.post(f"{OLLAMA_URL}/api/chat", json=follow, timeout=timeout)
            ms += int((time.monotonic() - t1) * 1000)
            if r2.status_code == 200:
                d2 = r2.json()
                m2 = d2.get("message") or {}
                content = m2.get("content") or ""
                continuation = "chat_think_off"
                done_reason = d2.get("done_reason")
                out_tokens = (out_tokens or 0) + (d2.get("eval_count") or 0)
            else:
                continuation = f"failed:{r2.status_code}"
        except Exception as e:
            ms += int((time.monotonic() - t1) * 1000)
            continuation = f"failed:{type(e).__name__}"
    elif cfg["think"]:
        budget_hit = False
    # a model that returns no `thinking` field (or thinks inline) still yields its content
    return _result(text=content, thinking=thinking, duration_ms=ms,
                   tokens={"input": prompt_tokens, "output": out_tokens, "thinking": None,
                           "thinking_chars": len(thinking) if thinking else 0},
                   backend_model=cfg["tag"], backend_version=ver, schema_enforced=bool(schema),
                   observed_token_count=prompt_tokens, reasoning=reasoning,
                   done_reason=done_reason, budget_hit=budget_hit, continuation=continuation,
                   think_budget=think_budget, prompt_count=counted)


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
    budget = None
    if is_ollama(backend):
        budget = ollama_budget(backend, record.get("role"), system, user)     # once; _ollama re-uses it
        ok, reason, n = budget[0] is not None, (None if budget[0] is not None else "context_exceeded"), budget[1]
    elif backend in LOCAL_BACKENDS:
        ok, reason, n = fits(record, backend, system, user)
    if backend in LOCAL_BACKENDS:
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
        return _ollama(record, system, user, backend=backend, budget=budget)
    return _anthropic(record, system, user, allow_spend)
