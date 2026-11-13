"""The OpenRouter study (2026-11-13): which of Scout's jobs can a free model do as well as production?

One key and one API format reach every free model in OpenRouter's catalogue. The study replays the
captured production calls (the same bundles the on-device lane replays, exact mode only) through each
of them and through the free router itself, and grades every answer against what production decided
on the same call (scout.modelcompare.compare_call). Nothing here is part of the nightly lane:
results live in a local folder on the mini, never in the private store, and no paid referee runs.

His settings (2026-11-12): every model as OpenRouter serves it out of the box (no sampling
parameters and no output schema sent to anyone), reasoning on where the model offers it, the same
prompt for all; only providers he has ruled on ever receive a prompt; the free router is an entry of
its own.

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
COVERAGE = 0.9                # a model is compared on a role once it has attempted this share of the role's calls

_gate = threading.Lock()
_log_lock = threading.Lock()
_last_start = [0.0]
_stop = {"why": None}          # set once: every worker ends (the day's cap, the key, a charge, an unapproved provider)
_used = {"day": None, "n": 0}


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
    if router and models:
        router["providers"] = sorted({p for m in models for p in m["providers"]})
        router["provider_names"] = sorted({p for m in models for p in m["provider_names"]})
        models.append(router)
    return {"models": models, "dropped": dropped}


def save_catalogue(cat: dict) -> str:
    p = os.path.join(STUDY_DIR, "catalogue", time.strftime("%Y-%m-%d") + ".json")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as fh:
        json.dump(cat, fh, indent=1)
    return p


# --- one call ----------------------------------------------------------------------------------------
def body_for(entry: dict, system: str, user: str, provider: dict | None = None) -> dict:
    """The request. No sampling parameter and no output schema, for any model. Providers are an
    allow-list: only the approved providers that serve this entry."""
    body = {"model": entry["model"], "stream": False,
            "messages": ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": user}],
            "provider": provider or {"only": list(entry["providers"])}}
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


def _send(body: dict, key: str, timeout: float):
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
                       headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
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
    body = body_for(entry, system, user, provider)
    r, ms, neterr, unsent = _send(body, key, timeout)
    row = {"at": datetime.now().isoformat(timespec="seconds"), "model": model, "call_id": record.get("call_id"), "role": role,
           "rule": body["provider"]}
    if unsent:
        return rb._result(status="skipped", reason="daily_cap" if unsent == "cap" else "stopped", outcome="stop_all",
                          text=f"{DAILY_CAP} requests used today" if unsent == "cap" else str(_stop["why"]), **base)

    def no_result(reason, outcome, detail, **more):
        _attempt_log({**row, "reason": reason, "ms": ms, "detail": detail, **more}, log)
        return rb._result(status="error", reason=reason, outcome=outcome, text=str(detail)[:400], duration_ms=ms, **base)

    if r is None:
        return no_result("network", "retry_later", neterr, http=None)
    try:
        data = r.json()
    except Exception:
        data = None
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
        return no_result("provider_error", "retry_later", msg, http=code, meta=meta)
    choices = data.get("choices") or []
    ch = choices[0] if choices and isinstance(choices[0], dict) else None
    if ch is None or ch.get("error") or ch.get("finish_reason") == "error":
        return no_result("broken_response", "retry_later", json.dumps((ch or {}).get("error") or "no choices")[:300], http=200)
    msg = ch.get("message") or {}
    usage = data.get("usage") or {}
    ver["served_by"], ver["served_model"] = data.get("provider"), data.get("model")
    cost = float(usage.get("cost") or 0.0)
    finish = ch.get("finish_reason")
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
    if cost > 0:
        _attempt_log({**row, "reason": "charged"}, log)
        return rb._result(status="error", reason="charged", outcome="stop_all",
                          text=f"a free call was billed ${cost:.6f} ({ver['served_model']} via {ver['served_by']})", **out)
    text, thinking = _text_of(msg.get("content")), msg.get("reasoning")
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
def score(summary_and_items: dict, role: str, status: str = "ok", reason: str | None = None, fmt_ok: bool | None = True) -> dict:
    """{"same": bool, "why": str, "agree": int, "judged": int} for one graded call.
    A call counts as the same decision only when the model answered, the answer was readable, and
    every item production judged got the same verdict. An item the model left out counts against it,
    except in the roles where leaving a quiet item unnamed is itself the quiet verdict
    (modelcompare.MISS_IS_VERDICT). A call the model failed to answer is never the same decision."""
    if status != "ok":
        return {"same": False, "why": f"no answer ({reason})", "agree": 0, "judged": 0}
    s = summary_and_items.get("summary") or {}
    if fmt_ok is False or s.get("candidate_parse") not in (None, "ok"):
        return {"same": False, "why": "unreadable format", "agree": 0, "judged": 0}
    quiet = modelcompare.MISS_IS_VERDICT.get(role)
    agree = judged = 0
    for it in summary_and_items.get("items") or []:
        st = it.get("status")
        if st == "abstain":
            if quiet is not None and it.get("reference") == quiet:
                continue                       # a quiet item left unnamed: the same quiet verdict, not counted either way
            judged += 1                        # left out: counts as a different decision
        elif st in ("agree", "disagree"):
            judged += 1
            agree += st == "agree"
    return {"same": agree == judged, "why": "same" if agree == judged else "different verdict", "agree": agree, "judged": judged}


def score_row(row: dict) -> dict:
    return score(row.get("comparison") or {}, row.get("role"), row.get("status"), row.get("reason"),
                 (row.get("study") or {}).get("format_ok"))


def gradable_roles(calls: list) -> tuple[list, dict]:
    """Roles the grader can score by agreement, proven on the data: production's own answer, fed back
    as the candidate, must come out as the same decision on every call of the role, with at least one
    judged item in the role. Returns (roles, {role: why not} for the rest)."""
    by_role = {}
    for c in calls:
        by_role.setdefault(c["role"], []).append(c)
    good, bad = [], {}
    for role, cs in sorted(by_role.items()):
        judged = same = 0
        for c in cs:
            ref_text = (c.get("result") or {}).get("text") or ""
            cmp_ = modelcompare.compare_call(c, {"status": "ok", "text": ref_text, "backend": "control"})
            sc = score(cmp_, role, fmt_ok=format_ok(role, ref_text))
            judged += sc["judged"]
            same += sc["same"] and (cmp_.get("summary") or {}).get("agree") is not None
        if judged == 0:
            bad[role] = "no verdicts to compare (free-text or set output)"
        elif same != len(cs):
            bad[role] = f"production's own answer scores {same}/{len(cs)} against itself"
        else:
            good.append(role)
    return good, bad


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
                if replay.get("reason") == "provider_error":
                    prior_errors[(entry["model"], c["call_id"])] += 1
                    if prior_errors[(entry["model"], c["call_id"])] >= GIVE_UP_AFTER:
                        replay = {**replay, "reason": "provider_rejected_repeatedly", "outcome": "store"}
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
            f"{sc['why']:18} {replay.get('duration_ms') or 0}ms via {(replay.get('backend_version') or {}).get('served_by')}")
    return {"model": entry["model"], "stored": stored, "attempts": attempts, "ended": ended}
