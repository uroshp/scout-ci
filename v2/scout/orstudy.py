"""The OpenRouter study (2026-11-13): which of Scout's jobs can a free model do as well as production?

One key and one API format reach every free model in OpenRouter's catalogue. The study replays the
captured production calls (the same bundles the on-device lane replays, exact mode only) through each
of them and through the free router itself, and grades every answer against what production decided
on the same call (scout.modelcompare.compare_call). Nothing here is part of the nightly lane:
results live in a local folder on the mini, never in the private store, and no paid referee runs.

His settings (2026-11-12): every model as OpenRouter serves it out of the box (no sampling
parameters, no output schema), reasoning on where the model offers it, the same request for all;
only providers he has ruled on ever receive a prompt; the free router is an entry of its own.
A call production made with a forced tool (the screen) is replayed with that same tool, forced, in
OpenRouter's format: the tool carries the output contract, and without it the model is never told
what to return.

Every request is logged (attempts.jsonl): rate limits, refusals, who served the call. That log is
the study's second result. One graded result is stored per (model, call); a rate limit, a network
error or a broken response is never stored as the model's answer.
"""
import json
import os
import re
import threading
import time
from collections import Counter
from datetime import datetime

import httpx

from . import modelcompare, replaybackends as rb, rolespecs

STUDY_DIR = os.environ.get("SCOUT_ORSTUDY_DIR", os.path.expanduser("~/scout-replay/openrouter_study"))
DAILY_CAP = int(os.environ.get("SCOUT_ORSTUDY_DAILY_CAP", "900"))       # the account allows 1,000 a day with credit bought
MIN_GAP_S = float(os.environ.get("SCOUT_ORSTUDY_MIN_GAP_S", "3.3"))     # 18 a minute, under the 20 allowed
ROUTER = "openrouter/free"
MAX_OUT = 32768               # output room asked for: the endpoint's own ceiling, up to this
GIVE_UP_AFTER = 3             # a call a provider has rejected this many times is stored as never answered
# Provider slugs (the part of an endpoint tag before "/") he has ruled on. A prompt goes only to these.
APPROVED_PROVIDERS = ("cohere", "google-ai-studio", "liquid", "nvidia", "particle-ai", "recursive", "thinkingmachines")
NOT_APPROVED = {"baidu": "hosted in China", "atlas-cloud": "hosting country not established",
                "novita": "hosting country not established"}
HARMLESS_PROMPT = "Reply with the single word: ready"

# --- the paid section (2026-11-13, his rulings): can a cheaper model do the judging Opus does today? ---
# One endpoint per model, named by its full tag, at the price he was shown. A tag that is gone or
# whose price moved is refused before anything is sent. `price` is dollars per million tokens (in, out).
# Rule for who may see a prompt: the model's maker, or a host that runs in the US.
PAID_DIR = os.path.expanduser("~/scout-replay/openrouter_paid")
PAID_BUDGET = float(os.environ.get("SCOUT_ORSTUDY_BUDGET", "35"))     # hard stop, dollars, the whole section
PAID_SINCE = "20261001T000000"
PAID_ROLES = ("judge", "audience_judge", "gate_judge")
PAID_MODELS = (
    {"model": "x-ai/grok-4.7", "tag": "xai/zdr/us", "name": "xAI", "price": (2.20, 6.60)},
    {"model": "nvidia/nemotron-3-ultra-550b-a55b", "tag": "baseten/fp4/us", "name": "BaseTen", "price": (0.60, 2.40)},
    {"model": "mistralai/mistral-large-4-0", "tag": "mistral/zdr", "name": "Mistral", "price": (0.70, 2.10)},
    {"model": "openai/gpt-6.1-terra", "tag": "openai", "name": "OpenAI", "price": (1.00, 5.00)},
    {"model": "openai/gpt-6.1-sol", "tag": "openai", "name": "OpenAI", "price": (2.00, 10.00)},
    {"model": "google/gemini-4-hydrogen", "tag": "google-vertex/global", "name": "Google", "price": (1.50, 7.50)},
    {"model": "deepseek/deepseek-v4-pro-0813", "tag": "together", "name": "Together", "price": (1.32, 3.96)},
    {"model": "moonshotai/kimi-k3.1-pro", "tag": "fireworks", "name": "Fireworks", "price": (2.00, 10.00)},
    {"model": "cohere/command-a-plus", "tag": "cohere/fp8", "name": "Cohere", "price": (0.30, 1.50)},
    # production's own judge, answering the same calls a second time: the yardstick for the others
    {"model": "anthropic/claude-opus-5.5", "tag": "anthropic", "name": "Anthropic", "price": (4.00, 20.00)},
)
COVERAGE = 0.9                # a model is compared on a role once it has attempted this share of the role's calls

# --- routing as OpenRouter intends it (2026-11-13, his ruling) -------------------------------------------
# Two kinds of entry. The Auto Router: no model named, OpenRouter picks the model and the host. A named
# model with the host left to OpenRouter (and its failover). Guardrail on both: only these providers,
# companies that run in the US or the model's own maker, may receive a prompt; and a price ceiling,
# enforced by OpenRouter (provider.max_price), bounds what one request can cost.
ROUTED_DIR = os.path.expanduser("~/scout-replay/openrouter_routed")
ROUTED_BUDGET = float(os.environ.get("SCOUT_ORSTUDY_ROUTED_BUDGET", "12"))
REP_BUDGET = 2.25              # one extra pass of Opus over the verification-judge calls
TOTAL_BUDGET = 38.0            # every paid folder together; his $40 less a margin
SCREEN_SINCE = "20261030T000000"      # the free study's screening calls, so the routed arms meet the free models on them
US_PROVIDERS = {"anthropic": "Anthropic", "openai": "OpenAI", "azure": "Azure", "amazon-bedrock": "Amazon Bedrock",
                "google-vertex": "Google", "google-ai-studio": "Google AI Studio", "xai": "xAI", "mistral": "Mistral",
                "cohere": "Cohere", "together": "Together", "fireworks": "Fireworks", "baseten": "BaseTen",
                "deepinfra": "DeepInfra", "digitalocean": "DigitalOcean", "cloudflare": "Cloudflare",
                "coreweave": "CoreWeave", "parasail": "Parasail", "nvidia": "Nvidia"}
ROUTED_MODELS = (
    {"model": "openrouter/auto", "ceiling": (5.00, 25.00), "plugins": [{"id": "auto-router"}]},
    # the models the Mac mini runs, served by whichever US host OpenRouter picks
    {"model": "openai/gpt-oss-20b", "ceiling": (0.50, 2.00)},
    {"model": "google/gemma-4-26b-a4b-it", "ceiling": (0.50, 2.00)},
    {"model": "nvidia/nemotron-3-nano-30b-a3b", "ceiling": (0.50, 2.00)},
)

_gate = threading.Lock()
_log_lock = threading.Lock()
_last_start = [0.0]
_stop = {"why": None}          # set once: every worker ends (the day's cap, the key, a charge, an unapproved provider)
_used = {"day": None, "n": 0}
_money_lock = threading.Lock()
_money = {"reserved": 0.0}     # worst-case cost of the paid requests in flight


def slug(model: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", model)


def stop_reason():
    return _stop["why"]


def reset_stop():
    _stop["why"] = None


# --- the request counter (this study's own; the account's day is the UTC day) -------------------------
def _day() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _usage_path() -> str:
    return os.path.join(STUDY_DIR, "usage.json")


def used_today() -> int:
    day = _day()
    if _used["day"] != day:
        try:
            with open(_usage_path()) as fh:
                n = int((json.load(fh) or {}).get(day, 0))
        except (OSError, ValueError, TypeError):
            n = 0
        _used["day"], _used["n"] = day, n
    return _used["n"]


def _count() -> None:
    """One more request today. Called with the gate held. A counter that cannot be written stops the run."""
    n = used_today() + 1
    _used["n"] = n
    os.makedirs(STUDY_DIR, exist_ok=True)
    tmp = _usage_path() + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({_used["day"]: n}, fh)
    os.replace(tmp, _usage_path())


# --- the catalogue -----------------------------------------------------------------------------------
def _get(url: str, timeout: float = 30.0):
    r = httpx.get(url, timeout=timeout, headers={"User-Agent": "scout-study"})
    r.raise_for_status()
    return r.json()


def _is_free(m: dict) -> bool:
    p = m.get("pricing") or {}
    return str(p.get("prompt")) == "0" and str(p.get("completion")) == "0"


def _text_out(m: dict) -> bool:
    return ((m.get("architecture") or {}).get("output_modalities") or ["text"]) == ["text"]


def catalogue(fetch=_get) -> dict:
    """{"models": [entry], "dropped": [{"model", "why"}]} from OpenRouter's public catalogue. An entry:
    {"model", "providers": [slug], "provider_names": [str], "reasoning": bool, "max_out": int|None, "router": bool}.
    The router's entry carries every approved provider that serves a usable model tonight."""
    data = fetch(f"{rb.OPENROUTER_URL}/models").get("data") or []
    models, dropped, router = [], [], None
    for m in sorted(data, key=lambda m: m.get("id") or ""):
        mid = m.get("id") or ""
        if not _is_free(m):
            continue
        if not _text_out(m):
            dropped.append({"model": mid, "why": "not a text-only model"}); continue
        sp = set(m.get("supported_parameters") or [])
        if mid == ROUTER:
            router = {"model": mid, "providers": [], "provider_names": [], "reasoning": "reasoning" in sp,
                      "max_out": None, "router": True}
            continue
        if not mid.endswith(":free"):
            dropped.append({"model": mid, "why": "priced at zero but not a ':free' variant"}); continue
        try:
            eps = (fetch(f"{rb.OPENROUTER_URL}/models/{mid}/endpoints").get("data") or {}).get("endpoints") or []
        except Exception as e:
            dropped.append({"model": mid, "why": f"endpoint list unreadable ({type(e).__name__})"}); continue
        ok = [e for e in eps if str(e.get("tag") or "").split("/")[0] in APPROVED_PROVIDERS]
        if not ok:
            bad = sorted({str(e.get("tag") or "").split("/")[0] for e in eps})
            why = "; ".join(f"{b}: {NOT_APPROVED.get(b, 'provider not ruled on yet')}" for b in bad) or "no endpoint"
            dropped.append({"model": mid, "why": why}); continue
        outs = [e.get("max_completion_tokens") for e in ok if e.get("max_completion_tokens")]
        models.append({"model": mid, "providers": sorted({str(e.get("tag") or "").split("/")[0] for e in ok}),
                       "provider_names": sorted({str(e.get("provider_name")) for e in ok}),
                       "reasoning": "reasoning" in sp, "max_out": min(outs) if outs else None, "router": False})
    if not models:
        raise RuntimeError("the catalogue lists no usable free model (fetch problem or every endpoint list unreadable)")
    if router and models:
        router["providers"] = sorted({p for m in models for p in m["providers"]})
        router["provider_names"] = sorted({p for m in models for p in m["provider_names"]})
        models.append(router)
    return {"models": models, "dropped": dropped}


def save_catalogue(cat: dict) -> str:
    p = os.path.join(STUDY_DIR, "catalogue", time.strftime("%Y-%m-%dT%H%M%S") + ".json")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as fh:
        json.dump(cat, fh, indent=1)
    return p


# --- the paid section: entries, and the money ------------------------------------------------------------
def paid_entries(fetch=_get, models=PAID_MODELS) -> dict:
    """The paid entries, each checked against the live endpoint list: the tag must exist and its
    price must be the price he approved. Same shape as catalogue()."""
    out, dropped = [], []
    for m in models:
        try:
            eps = (fetch(f"{rb.OPENROUTER_URL}/models/{m['model']}/endpoints").get("data") or {}).get("endpoints") or []
        except Exception as e:
            dropped.append({"model": m["model"], "why": f"endpoint list unreadable ({type(e).__name__})"}); continue
        ep = next((e for e in eps if e.get("tag") == m["tag"]), None)
        if ep is None:
            dropped.append({"model": m["model"], "why": f"endpoint {m['tag']} is no longer listed"}); continue
        pr = ep.get("pricing") or {}
        try:
            live = (round(float(pr.get("prompt")) * 1e6, 4), round(float(pr.get("completion")) * 1e6, 4))
        except (TypeError, ValueError):
            live = None
        if live != tuple(round(x, 4) for x in m["price"]):
            dropped.append({"model": m["model"], "why": f"price moved since it was approved: {m['price']} then, {live} now"}); continue
        if str(ep.get("provider_name")) != m["name"]:
            dropped.append({"model": m["model"], "why": f"endpoint {m['tag']} is now run by {ep.get('provider_name')!r}"}); continue
        out.append({"model": m["model"], "providers": [m["tag"]], "provider_names": [m["name"]],
                    "reasoning": "reasoning" in set(ep.get("supported_parameters") or []),
                    "max_out": ep.get("max_completion_tokens"), "router": False, "paid": True, "price": tuple(m["price"])})
    return {"models": out, "dropped": dropped}


def routed_entries(fetch=_get, models=ROUTED_MODELS) -> dict:
    """The routed entries. A named model keeps only the US providers that list it under the ceiling;
    the Auto Router carries the whole allow-list (it resolves the model per request)."""
    out, dropped = [], []
    for m in models:
        entry = {"model": m["model"], "router": True, "routed": True, "paid": True, "price": tuple(m["ceiling"]),
                 "reasoning": True, "max_out": MAX_OUT, "plugins": m.get("plugins"),
                 "provider_extra": {"max_price": {"prompt": m["ceiling"][0], "completion": m["ceiling"][1]}, "require_parameters": True}}
        if m["model"] == "openrouter/auto":
            entry.update(providers=sorted(US_PROVIDERS), provider_names=sorted(US_PROVIDERS.values()))
            out.append(entry); continue
        try:
            eps = (fetch(f"{rb.OPENROUTER_URL}/models/{m['model']}/endpoints").get("data") or {}).get("endpoints") or []
        except Exception as e:
            dropped.append({"model": m["model"], "why": f"endpoint list unreadable ({type(e).__name__})"}); continue
        ok = []
        for e in eps:
            base = str(e.get("tag") or "").split("/")[0]
            pr = e.get("pricing") or {}
            try:
                under = float(pr.get("prompt")) * 1e6 <= m["ceiling"][0] and float(pr.get("completion")) * 1e6 <= m["ceiling"][1]
            except (TypeError, ValueError):
                under = False
            if base in US_PROVIDERS and under and str(e.get("provider_name")) == US_PROVIDERS[base]:
                ok.append(e)
        if not ok:
            dropped.append({"model": m["model"], "why": "no US provider lists it under the price ceiling"}); continue
        entry.update(providers=sorted({str(e["tag"]).split("/")[0] for e in ok}),
                     provider_names=sorted({str(e["provider_name"]) for e in ok}),
                     reasoning=any("reasoning" in set(e.get("supported_parameters") or []) for e in ok))
        out.append(entry)
    return {"models": out, "dropped": dropped}


def _spend_path() -> str:
    return os.path.join(STUDY_DIR, "spend.json")


def spend() -> dict:
    """{"billed": what the stored answers cost, "assumed": worst case for requests whose bill was never
    seen (a timeout), "account_start"/"account_now": OpenRouter's own usage figure for the key}."""
    try:
        with open(_spend_path()) as fh:
            d = json.load(fh) or {}
    except (OSError, ValueError):
        d = {}
    return {"billed": float(d.get("billed") or 0), "assumed": float(d.get("assumed") or 0),
            "account_start": d.get("account_start"), "account_now": d.get("account_now")}


def _spend_write(d: dict) -> None:
    os.makedirs(STUDY_DIR, exist_ok=True)
    tmp = _spend_path() + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(d, fh)
    os.replace(tmp, _spend_path())


def spent(d: dict | None = None) -> float:
    """This folder's own ledger: the bills on its replies plus the allowance for bills never seen."""
    d = d or spend()
    return d["billed"] + d["assumed"]


def _ledgers() -> list:
    out, root = [], os.path.dirname(PAID_DIR)
    for name in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        if name.startswith("openrouter_paid") or name.startswith("openrouter_routed"):
            try:
                with open(os.path.join(root, name, "spend.json")) as fh:
                    out.append((os.path.join(root, name), json.load(fh) or {}))
            except (OSError, ValueError):
                pass
    return out


def spent_total() -> float:
    """All paid work on the key: the larger of every folder's ledger added up and what OpenRouter says
    the key has used since the first paid request (the folders share one key, so OpenRouter's figure
    can only be read against all of them together)."""
    led = _ledgers()
    own = sum(float(x.get("billed") or 0) + float(x.get("assumed") or 0) for _, x in led)
    starts = [float(x["account_start"]) for _, x in led if x.get("account_start") is not None]
    nows = [float(x["account_now"]) for _, x in led if x.get("account_now") is not None]
    return max(own, max(nows) - min(starts)) if starts and nows else own


def account_usage(key: str, fetch=None):
    """Dollars the key has used, by OpenRouter's count. None when it cannot be read."""
    try:
        if fetch is not None:
            data = fetch(f"{rb.OPENROUTER_URL}/key")
        else:
            r = httpx.get(f"{rb.OPENROUTER_URL}/key", timeout=20, headers={"Authorization": f"Bearer {key}"})
            r.raise_for_status()
            data = r.json()
        return float((data.get("data") or {}).get("usage"))
    except Exception:
        return None


def open_ledger(key: str) -> dict:
    """Before the first paid request: record OpenRouter's usage figure for the key as the starting point."""
    now = account_usage(key)
    with _money_lock:
        d = spend()
        if now is not None:
            if d["account_start"] is None:
                d["account_start"] = now - (d["billed"] + d["assumed"])
            d["account_now"] = now
        _spend_write(d)
    return d


def worst_case(entry: dict, system: str, user: str, body: dict) -> float:
    """The most one request can cost: a generous count of the prompt, and all the output room, at the listed price."""
    pin, pout = entry["price"]
    return ((len(system) + len(user)) / 2.5 * pin + float(body.get("max_tokens") or MAX_OUT) * pout) / 1e6


def _reserve(entry: dict, worst: float) -> str | None:
    """Hold the worst case for one paid request, or say why not: this folder's own limit, or the total."""
    with _money_lock:
        d = spend()
        if spent(d) + _money["reserved"] + worst > PAID_BUDGET:
            return (f"${spent(d):.2f} spent, ${_money['reserved']:.2f} in flight: the next request could pass "
                    f"the ${PAID_BUDGET:.2f} limit")
        total = spent_total()
        if total + _money["reserved"] + worst > TOTAL_BUDGET:
            return (f"${total:.2f} spent across the paid work: the next request could pass the ${TOTAL_BUDGET:.0f} total")
        _money["reserved"] += worst
    return None


def _settle(worst: float, *, billed: float = 0.0, assumed: float = 0.0, key: str | None = None) -> None:
    """Release a reservation and record what the request cost (or, unseen, could have cost)."""
    now = account_usage(key) if key else None
    with _money_lock:
        _money["reserved"] = max(0.0, _money["reserved"] - worst)
        d = spend()
        d["billed"] += billed
        d["assumed"] += assumed
        if now is not None:
            d["account_now"] = now
            if d["account_start"] is None:
                d["account_start"] = now - billed
        _spend_write(d)


# --- one call ----------------------------------------------------------------------------------------
def forced_tool(record: dict):
    """(tools, tool_choice) in OpenRouter's format when production forced one tool on this call, else None."""
    tc, tools = record.get("tool_choice") or {}, record.get("tools") or []
    if not (isinstance(tc, dict) and tc.get("type") == "tool" and tc.get("name")):
        return None
    t = next((t for t in tools if isinstance(t, dict) and t.get("name") == tc["name"]), None)
    if not t:
        return None
    return ([{"type": "function", "function": {"name": t["name"], "description": t.get("description") or "",
                                                "parameters": t.get("input_schema") or {"type": "object"}}}],
            {"type": "function", "function": {"name": t["name"]}})


def body_for(entry: dict, system: str, user: str, provider: dict | None = None, record: dict | None = None) -> dict:
    """The request. No sampling parameter and no output schema, for any model. Providers are an
    allow-list: only the approved providers that serve this entry. A call production made with a
    forced tool carries the same tool, forced."""
    body = {"model": entry["model"], "stream": False,
            "messages": ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": user}],
            "provider": provider or {"only": list(entry["providers"]), **(entry.get("provider_extra") or {})}}
    if entry.get("plugins"):
        body["plugins"] = entry["plugins"]
    ft = forced_tool(record or {})
    if ft:
        body["tools"], body["tool_choice"] = ft
    if entry.get("max_out"):
        body["max_tokens"] = min(MAX_OUT, entry["max_out"])
    if entry.get("reasoning"):
        body["reasoning"] = {"enabled": True}
    return body


def _attempt_log(row: dict, log: str = "attempts.jsonl") -> None:
    row = {k: (v[:300] if isinstance(v, str) else v) for k, v in row.items()}
    try:
        with _log_lock:
            os.makedirs(STUDY_DIR, exist_ok=True)
            with open(os.path.join(STUDY_DIR, log), "a") as fh:
                fh.write(json.dumps(row) + "\n")
    except OSError:
        pass


def load_attempts(log: str = "attempts.jsonl") -> list:
    out = []
    try:
        with open(os.path.join(STUDY_DIR, log)) as fh:
            for line in fh:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass                      # a torn line is skipped, never fatal
    except OSError:
        pass
    return out


def _send(body: dict, key: str, timeout: float, meta: bool = False):
    """One paced, counted request. Returns (response|None, ms, error text|None, why not sent|None)."""
    with _gate:
        if _stop["why"]:
            return None, 0, None, "stopped"
        if used_today() >= DAILY_CAP:
            return None, 0, None, "cap"
        gap = MIN_GAP_S - (time.monotonic() - _last_start[0])
        if gap > 0:
            time.sleep(gap)
        try:
            _count()
        except OSError as e:
            _stop["why"] = f"the request counter could not be written ({type(e).__name__})"
            return None, 0, None, "stopped"
        _last_start[0] = time.monotonic()
    t0 = time.monotonic()
    try:
        r = httpx.post(f"{rb.OPENROUTER_URL}/chat/completions", json=body, timeout=timeout,
                       headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                **({"X-OpenRouter-Metadata": "enabled"} if meta else {})})
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        return None, int((time.monotonic() - t0) * 1000), f"not sent: {type(e).__name__}", None
    except httpx.TimeoutException as e:
        return None, int((time.monotonic() - t0) * 1000), f"timeout: {type(e).__name__}", None
    except Exception as e:
        return None, int((time.monotonic() - t0) * 1000), f"{type(e).__name__}: {e}"[:300], None
    return r, int((time.monotonic() - t0) * 1000), None, None


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(p.get("text") or "") for p in content if isinstance(p, dict))
    return ""


def call(entry: dict, record: dict, system: str, user: str, *, provider: dict | None = None,
         timeout: float = 420.0, log: str = "attempts.jsonl") -> dict:
    """One model answer for one captured call. `outcome` tells the runner what to do with it:
    "store"       a result: an answer, or a failure that belongs to the model on this call;
    "retry_later" not a result (a rate limit, the network, a broken response): the same call is tried again;
    "model_off"   the model cannot be called this way at all: it rests until the next run;
    "stop_all"    the day's cap, the key, a charge, or a provider outside the approved list served the call."""
    model, role = entry["model"], record.get("role")
    ver = {"model": model, "vendor": model.split("/")[0], "study": "openrouter-2026-11", "settings": "provider defaults, no schema",
           "reasoning_requested": bool(entry.get("reasoning")), "served_by": None, "served_model": None}
    base = dict(backend_model=model, backend_version=ver, reasoning="thinking" if entry.get("reasoning") else "none")
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        return rb._result(status="error", reason="no_key", outcome="stop_all", text="OPENROUTER_API_KEY is not set", **base)
    body = body_for(entry, system, user, provider, record)
    ver["forced_tool"] = bool(body.get("tools"))
    paid, worst = bool(entry.get("paid")), 0.0
    if paid:
        ver["study"] = "openrouter-routed-2026-11" if entry.get("routed") else "openrouter-paid-2026-11"
        ver["endpoint"] = None if entry.get("routed") else entry["providers"][0]
        ver["routed"] = bool(entry.get("routed"))
        worst = worst_case(entry, system, user, body)
        full = _reserve(entry, worst)
        if full:
            return rb._result(status="skipped", reason="budget", outcome="stop_all", text=full, **base)
    r, ms, neterr, unsent = _send(body, key, timeout, meta=bool(entry.get("routed")))
    if paid and unsent:
        _settle(worst)
    elif paid and r is None and str(neterr).startswith("not sent"):
        _settle(worst)                                # the connection was never made: nothing to bill
    elif paid and r is None:
        _settle(worst, assumed=worst, key=key)        # sent, no reply seen: it may still be billed
    row = {"at": datetime.now().isoformat(timespec="seconds"), "model": model, "call_id": record.get("call_id"), "role": role,
           "rule": body["provider"]}
    if unsent:
        return rb._result(status="skipped", reason="daily_cap" if unsent == "cap" else "stopped", outcome="stop_all",
                          text=f"{DAILY_CAP} requests used today" if unsent == "cap" else str(_stop["why"]), **base)

    def no_result(reason, outcome, detail, **more):
        if paid and isinstance(data, dict) and isinstance(data.get("usage"), dict) and data["usage"].get("cost"):
            more["cost"] = float(data["usage"]["cost"])
        _attempt_log({**row, "reason": reason, "ms": ms, "detail": detail, **more}, log)
        return rb._result(status="error", reason=reason, outcome=outcome, text=str(detail)[:400], duration_ms=ms, **base)

    data = None
    if r is None:
        return no_result("timeout" if str(neterr).startswith("timeout") else "network", "retry_later", neterr, http=None)
    try:
        data = r.json()
    except Exception:
        data = None
    if paid:
        # every reply settles its reservation once, here, with whatever bill the reply carries; a reply
        # that hides its bill is caught by OpenRouter's own usage figure, read in the same step
        seen = (data.get("usage") or {}).get("cost") if isinstance(data, dict) and isinstance(data.get("usage"), dict) else None
        _settle(worst, billed=float(seen or 0.0), key=key)
    if not isinstance(data, dict):
        return no_result("broken_response", "retry_later", (r.text or "")[:200], http=r.status_code)
    err = data.get("error")
    if r.status_code != 200 or err:
        msg = str((err or {}).get("message") if isinstance(err, dict) else err or r.text)[:400]
        code = (err or {}).get("code") if isinstance(err, dict) else None
        code = int(code) if str(code).isdigit() else r.status_code
        meta = json.dumps((err or {}).get("metadata"))[:300] if isinstance(err, dict) and err.get("metadata") else None
        low = msg.lower()
        if code == 429:
            if "free-models-per-day" in low:
                return no_result("account_daily_limit", "stop_all", msg, http=code)
            return no_result("rate_limited", "retry_later", msg, http=code, meta=meta)
        if code in (401, 402):
            return no_result({401: "auth", 402: "no_credit"}[code], "stop_all", msg, http=code)
        if code == 403:
            if "moderat" in low or "flagged" in low:
                _attempt_log({**row, "http": code, "reason": "moderated", "ms": ms, "detail": msg}, log)
                return rb._result(status="error", reason="moderated", outcome="store", text=msg, duration_ms=ms, **base)
            return no_result("model_refused", "model_off", msg, http=code)
        if "context" in low and ("length" in low or "exceed" in low or "maximum" in low):
            _attempt_log({**row, "http": code, "reason": "context_exceeded", "ms": ms, "detail": msg}, log)
            return rb._result(status="skipped", reason="context_exceeded", outcome="store", text=msg, duration_ms=ms, **base)
        if body.get("tools") and code in (400, 404, 422) and ("tool" in low or "function" in low):
            _attempt_log({**row, "http": code, "reason": "forced_tool_rejected", "ms": ms, "detail": msg}, log)
            return rb._result(status="error", reason="forced_tool_rejected", outcome="store", text=msg, duration_ms=ms, **base)
        if code in (400, 404, 422):
            return no_result("provider_error", "retry_later", msg, http=code, meta=meta)      # counted toward giving up
        return no_result("upstream_error", "retry_later", msg, http=code, meta=meta)          # 5xx, 408: never counted
    choices = data.get("choices") or []
    ch = choices[0] if choices and isinstance(choices[0], dict) else None
    if ch is None or ch.get("error") or ch.get("finish_reason") == "error":
        return no_result("broken_response", "retry_later", json.dumps((ch or {}).get("error") or "no choices")[:300], http=200)
    msg = ch.get("message") or {}
    usage = data.get("usage") or {}
    ver["served_by"], ver["served_model"] = data.get("provider"), data.get("model")
    cost = float(usage.get("cost") or 0.0)
    finish = ch.get("finish_reason")
    if entry.get("routed"):
        md = data.get("openrouter_metadata") if isinstance(data.get("openrouter_metadata"), dict) else {}
        ver["generation_id"] = data.get("id")
        ver["routing"] = {"attempt": md.get("attempt"), "strategy": md.get("strategy"), "summary": md.get("summary"),
                          "attempts": md.get("attempts"),
                          "candidates": len(md.get("endpoints") or []) if isinstance(md.get("endpoints"), list) else None}
    out = dict(duration_ms=ms, schema_enforced=False, observed_token_count=usage.get("prompt_tokens"),
               tokens={"input": usage.get("prompt_tokens"), "output": usage.get("completion_tokens"),
                       "thinking": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")},
               cost_usd=cost, done_reason=finish, **base)
    row.update(http=200, ms=ms, served_by=ver["served_by"], served_model=ver["served_model"], cost=cost,
               finish=finish, out_tokens=usage.get("completion_tokens"))
    if ver["served_by"] not in entry.get("provider_names", []):
        # who served it is not on the approved list (or was not reported): nothing stored, everything stops
        _attempt_log({**row, "reason": "unapproved_provider_served"}, log)
        return rb._result(status="error", reason="unapproved_provider_served", outcome="stop_all",
                          text=f"served by {ver['served_by']!r}, not one of {entry.get('provider_names')}", **out)
    if paid:
        listed = ((usage.get("prompt_tokens") or 0) * entry["price"][0] + (usage.get("completion_tokens") or 0) * entry["price"][1]) / 1e6
        if cost > listed * 1.25 + 0.002:
            _attempt_log({**row, "reason": "billed_above_listed_price", "listed": listed}, log)
            return rb._result(status="error", reason="billed_above_listed_price", outcome="stop_all",
                              text=f"billed ${cost:.4f}, the listed price gives ${listed:.4f} ({ver['served_model']} via {ver['served_by']})", **out)
    elif cost > 0:
        _attempt_log({**row, "reason": "charged"}, log)
        return rb._result(status="error", reason="charged", outcome="stop_all",
                          text=f"a free call was billed ${cost:.6f} ({ver['served_model']} via {ver['served_by']})", **out)
    text, thinking = _text_of(msg.get("content")), msg.get("reasoning")
    calls_ = [t for t in (msg.get("tool_calls") or []) if isinstance(t, dict) and isinstance(t.get("function"), dict)]
    ver["answer_via"] = "content"
    if calls_:
        args = calls_[0]["function"].get("arguments")
        text = args if isinstance(args, str) else json.dumps(args)
        ver["answer_via"] = "tool_call"
    row["via"] = ver["answer_via"]
    if finish == "length":
        # the output room ran out: on reasoning alone (nothing written), or part-way through the answer.
        # Either way the model did not deliver this call; a cut-off answer is never graded as an answer.
        reason = "cut_off" if text.strip() else "reasoning_used_all_output"
        _attempt_log({**row, "reason": reason}, log)
        return rb._result(status="error", reason=reason, outcome="store", text=text, thinking=thinking, **out)
    if not text.strip():
        _attempt_log({**row, "reason": "empty"}, log)
        return rb._result(status="error", reason="empty", outcome="store", text="", thinking=thinking, **out)
    _attempt_log({**row, "reason": "ok", "trace": bool(thinking)}, log)
    return rb._result(outcome="store", text=text, thinking=thinking, **out)


# --- the local result store --------------------------------------------------------------------------
def result_path(model: str, call_id: str) -> str:
    return os.path.join(STUDY_DIR, "results", slug(model), f"{slug(call_id)}.json")


def have(model: str) -> set:
    d = os.path.join(STUDY_DIR, "results", slug(model))
    try:
        return {f[:-5] for f in os.listdir(d) if f.endswith(".json")}
    except OSError:
        return set()


def format_ok(role: str, text: str) -> bool:
    """The answer is an object carrying the keys the job's output format requires. The lane's parsers
    read an object without them as "nothing found", which would score a non-answer as a quiet verdict."""
    required = (rolespecs.role_schema(role) or {}).get("required") or []
    try:
        obj = rolespecs._extract(text or "")
    except Exception:
        return False
    return isinstance(obj, dict) and all(k in obj for k in required)


def grade_and_store(entry: dict, record: dict, replay: dict) -> dict:
    backend = "or:" + entry["model"]
    cmp_ = modelcompare.compare_call(record, {**replay, "backend": backend})
    res = modelcompare.result_record(record, {k: v for k, v in replay.items() if k != "outcome"}, cmp_,
                                     backend=backend, mode="exact", call_ref=record.get("_call_ref"))
    res["study"] = {"format_ok": format_ok(record.get("role"), replay.get("text")) if replay.get("status") == "ok" else None}
    p = result_path(entry["model"], record["call_id"])
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(res, fh)
    os.replace(tmp, p)
    return res


def load_results() -> list:
    out, root = [], os.path.join(STUDY_DIR, "results")
    for d in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        for f in sorted(os.listdir(os.path.join(root, d))):
            if f.endswith(".json"):
                try:
                    with open(os.path.join(root, d, f)) as fh:
                        out.append(json.load(fh))
                except (OSError, ValueError):
                    pass
    return out


# --- scoring: did the model make the same decision as production on this call? -------------------------
LOUD = {"screen": "substantial"}       # jobs decided per run: the decision is whether anything is flagged loud


def items_score(cmp_: dict, role: str) -> dict:
    """Item-level: every item production judged must get the same verdict. An item the model left out
    counts against it, except a quiet item in the roles where leaving it unnamed is the quiet verdict."""
    quiet = modelcompare.MISS_IS_VERDICT.get(role)
    agree = judged = 0
    for it in cmp_.get("items") or []:
        st = it.get("status")
        if st == "abstain":
            if quiet is not None and it.get("reference") == quiet:
                continue
            judged += 1
        elif st in ("agree", "disagree"):
            judged += 1
            agree += st == "agree"
    return {"agree": agree, "judged": judged}


def loud_flag(role: str, text: str):
    """For a per-run job: did this answer flag anything loud? None when the answer cannot be read."""
    parse = (rolespecs.spec(role) or {}).get("parse")
    try:
        parsed = parse(text or "") if parse else None
    except Exception:
        parsed = None
    if parsed is None:
        return None
    return any(v == LOUD[role] for v in (parsed.get("items") or {}).values())


def score(cmp_: dict, role: str, status: str = "ok", reason: str | None = None, fmt_ok: bool | None = True,
          text: str | None = None, ref_text: str | None = None) -> dict:
    """{"same": bool, "why": str, ...} for one graded call. A call is the same decision only when the
    model answered, readably, and decided what production decided:
    - a per-run job (the screen): the decision is the one the pipeline acts on, whether anything was
      flagged loud. Which findings were named is reported beside it ("found"/"loud": of the findings
      production flagged loud, how many the model flagged loud too);
    - picking the lead: the same winner and the same margin;
    - every other job: the same verdict on every item production judged.
    A call the model failed to answer is never the same decision."""
    if status != "ok":
        return {"same": False, "why": f"no answer ({reason})"}
    s = cmp_.get("summary") or {}
    if fmt_ok is False or s.get("candidate_parse") not in (None, "ok"):
        return {"same": False, "why": "unreadable format"}
    it = items_score(cmp_, role)
    if role in LOUD:
        cand = loud_flag(role, text)
        if cand is None:
            return {"same": False, "why": "unreadable format"}
        ref_loud = [i for i in cmp_.get("items") or [] if i.get("reference") == LOUD[role]]
        ref = bool(ref_loud) if ref_text is None else bool(loud_flag(role, ref_text))
        found = sum(1 for i in ref_loud if i.get("status") == "agree")
        return {"same": cand == ref, "why": "same" if cand == ref else ("missed it" if ref else "false alarm"),
                "ref_loud": ref, "cand_loud": cand, "found": found, "loud": len(ref_loud), **it}
    same = it["agree"] == it["judged"]
    if role == "lead_election" and (cmp_.get("extra") or {}).get("winner_agree") is False:
        same = False
    return {"same": same, "why": "same" if same else "different verdict", **it}


def score_row(row: dict) -> dict:
    return score(row.get("comparison") or {}, row.get("role"), row.get("status"), row.get("reason"),
                 (row.get("study") or {}).get("format_ok"), row.get("text"))


def reference_ok(c: dict) -> str | None:
    """None when production's stored answer on this call can serve as the reference, else why not:
    the lane's own eligibility rule (a fallback model answered, an unparsed answer), the output
    format, and the grader agreeing with production's answer fed back to it."""
    ok, why = rolespecs.reference_eligible(c)
    if not ok:
        return str(why)
    ref_text = (c.get("result") or {}).get("text") or ""
    if not format_ok(c["role"], ref_text):
        return "production's stored answer is not in the job's format"
    cmp_ = modelcompare.compare_call(c, {"status": "ok", "text": ref_text, "backend": "control"})
    if (cmp_.get("summary") or {}).get("agree") is None:
        return "no verdicts to compare (free-text or set output)"
    sc = score(cmp_, c["role"], text=ref_text)
    it = items_score(cmp_, c["role"])
    if not sc["same"] or it["agree"] != it["judged"]:
        return "production's answer does not score as itself"
    return None


def gradable(calls: list, min_good: float = 0.9) -> tuple[list, dict]:
    """(calls the study can score, {role: {why: count}} for what is left out). A job stays in when at
    least 90% of its calls have a usable reference and at least one verdict to compare; its other
    calls are left out one by one, never the whole job for one bad capture."""
    by_role, out, left = {}, [], {}
    for c in calls:
        by_role.setdefault(c["role"], []).append(c)
    for role, cs in sorted(by_role.items()):
        why = [(c, reference_ok(c)) for c in cs]
        good = [c for c, w in why if w is None]
        bad = Counter(w for _, w in why if w is not None)
        judged = sum(items_score(modelcompare.compare_call(c, {"status": "ok", "text": (c.get("result") or {}).get("text") or "",
                                                                 "backend": "control"}), role)["judged"] for c in good)
        if len(good) < min_good * len(cs) or judged == 0:
            left[role] = dict(bad) or {"no verdicts to compare": len(cs)}
            if len(good) >= min_good * len(cs) and judged == 0:
                left[role] = {"no verdicts to compare": len(cs)}
            continue
        out.extend(good)
        if bad:
            left[role] = dict(bad)
    return out, left


def baselines(role: str, rows: list) -> dict:
    """What a model that always gives one answer would score on these rows' calls (one row per call)."""
    out = {}
    if role in LOUD:
        loud = [bool([i for i in (r.get("comparison") or {}).get("items") or [] if i.get("reference") == LOUD[role]]) for r in rows]
        out["never flags anything"] = sum(1 for x in loud if not x)
        out["always flags something"] = sum(1 for x in loud if x)
        return out
    for label in (rolespecs.spec(role) or {}).get("label_set") or ():
        out[f"always '{label}'"] = sum(1 for r in rows if ((r.get("comparison") or {}).get("items") or [])
                                       and all(i.get("reference") == label for i in (r.get("comparison") or {}).get("items") or []))
    return out


# --- one model's night -------------------------------------------------------------------------------
def run_model(entry: dict, calls: list, quota: int, *, prior_errors: Counter | None = None,
              should_stop=lambda: False, say=print) -> dict:
    """Work one model through `calls` in order until its quota of requests is spent. A call that could
    not be answered for a reason outside the model is tried again, never skipped over, so every
    model's stored calls are the same front part of the list. Returns {"model", "stored", "attempts", "ended"}."""
    done = have(entry["model"])
    prior_errors = prior_errors if prior_errors is not None else Counter()
    stored = attempts = 0
    ended = "done"
    for c in calls:
        if slug(c["call_id"]) in done:
            continue
        system, user = rb.exact_prompt(c)
        strikes = 0
        while True:
            if _stop["why"]:
                return {"model": entry["model"], "stored": stored, "attempts": attempts, "ended": "stopped"}
            if should_stop():
                return {"model": entry["model"], "stored": stored, "attempts": attempts, "ended": "time"}
            if attempts >= quota:
                return {"model": entry["model"], "stored": stored, "attempts": attempts, "ended": "quota"}
            replay = call(entry, c, system, user)
            what = replay.get("outcome")
            if what == "stop_all":
                if replay.get("reason") not in ("stopped",):
                    _stop["why"] = _stop["why"] or f"{entry['model']}: {replay.get('reason')}: {str(replay.get('text'))[:160]}"
                return {"model": entry["model"], "stored": stored, "attempts": attempts, "ended": replay.get("reason")}
            attempts += 1
            if what == "model_off":
                return {"model": entry["model"], "stored": stored, "attempts": attempts, "ended": replay.get("reason")}
            if what == "retry_later":
                if replay.get("reason") in ("provider_error", "timeout"):
                    prior_errors[(entry["model"], c["call_id"])] += 1
                    if prior_errors[(entry["model"], c["call_id"])] >= GIVE_UP_AFTER:
                        gave = "timed_out_repeatedly" if replay.get("reason") == "timeout" else "provider_rejected_repeatedly"
                        replay = {**replay, "reason": gave, "outcome": "store"}
                        break
                strikes += 1
                if strikes >= 2:
                    return {"model": entry["model"], "stored": stored, "attempts": attempts, "ended": replay.get("reason")}
                time.sleep(65 if replay.get("reason") == "rate_limited" else 10)
                continue
            break
        res = grade_and_store(entry, c, replay)
        done.add(slug(c["call_id"]))
        stored += 1
        sc = score_row(res)
        say(f"  {entry['model'][:44]:44} {c['role']:14} {replay['status']:7} {(replay.get('reason') or ''):26} "
            f"{sc['why'][:28]:28} {replay.get('duration_ms') or 0}ms via {(replay.get('backend_version') or {}).get('served_by')}")
    return {"model": entry["model"], "stored": stored, "attempts": attempts, "ended": ended}
