"""Call capture for the on-device model comparison (2026-09-28, plan: ok-a-thing-i-typed-biscuit).

Records EVERY paid model call's exact inputs and output during a real run so the same inputs can be
REPLAYED later on the Mac mini against other models (Apple on-device, Magistral via Ollama) and the
verdicts compared. This module is a pure observer, same contract as scout/shadow.py:

  - gated by SCOUT_CALL_CAPTURE=1 (independent of SCOUT_SHADOW_EVAL); off = every entry point is a
    no-op that returns immediately, so the live path is byte-identical;
  - nothing is captured unless a caller that KNOWS the spend is real has opened a run
    (monitor.run_all, scripts/run_selfserve.py, scripts/run_challenger.py, the roster baseline);
    the _drive hooks are no-ops otherwise (warn once);
  - every public function is wrapped so it can only print "[calllog] ... skipped", never raise into
    the live path; a failed flush loses captures, never the run;
  - one bundle per run, written ONCE at flush (never per call), only to the PRIVATE store
    (selfserve.use_github()); prompts hold third-party page text and card IP and never land in a
    checkout;
  - the exact-replay fields (system, user, result.text) are NEVER clipped — clipping applies only to
    transcript rows, and tool results are stored whole for the tools-on monitor roles because they
    are the frozen-mode input.

Record schema (one element of bundle["calls"]):
  call_id, seq, run_ts, source, slug, phase, role, model, fidelity ("exact" | "toolsful"),
  system {kind: "string"|"preset", text | preset+append}, user, options {...}, tools, tool_choice,
  transcript [{seq, role, kind: text|tool_use|tool_result, parent_tool_use_id, tool_use_id, name,
  input|content}], result {text, cost_usd, duration_ms, duration_api_ms, num_turns, by_role,
  model_usage}, status "ok"|"failed", error, sizes {...}, truncated [field paths], captured_at.
"""
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime

from scout import config, selfserve

SCHEMA_VERSION = 1
CALLS_DIR = "calls"
PART_MAX_BYTES = 900_000            # the Contents API returns empty content above 1 MB on READ
TRANSCRIPT_ROW_MAX = 4_000          # generation transcripts only (tools-on monitor roles stay whole)
TRANSCRIPT_CALL_MAX = 300_000
BUFFER_MAX_CALLS = 2_000            # drop-on-overflow, never grow without bound
WHOLE_TRANSCRIPT_ROLES = ("triage", "materiality", "my_facts", "ask_research")

_RUN: dict | None = None
_CTX: dict = {}
_WARNED_NO_RUN = False
_SECRET = re.compile(r"(sk-ant-[A-Za-z0-9_\-]{8,}|ghp_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{8,})")


def enabled() -> bool:
    return bool(config.CALL_CAPTURE_ENABLED)


# --- run lifecycle ---------------------------------------------------------------------------------
def begin_run(source: str, started: datetime | None = None) -> None:
    """Open a capture run. Only callers that know the spend is real call this."""
    global _RUN
    if not enabled():
        return
    try:
        now = started or datetime.now()
        _RUN = {"source": source, "run_ts": now.isoformat(timespec="seconds"),
                "stamp": now.strftime("%Y%m%dT%H%M%S"),
                "run_id": hashlib.sha256(f"{source}|{now.isoformat()}|{os.getpid()}".encode()).hexdigest()[:6],
                "calls": [], "seq": 0, "dropped": 0}
    except Exception as e:
        print(f"[calllog] begin_run skipped ({type(e).__name__}: {e})", file=sys.stderr)


def set_context(**kw) -> None:
    """Attach slug/phase to subsequent captures (set_context(slug=None) clears)."""
    try:
        for k, v in kw.items():
            if v is None:
                _CTX.pop(k, None)
            else:
                _CTX[k] = v
    except Exception:
        pass


def run_open() -> bool:
    return _RUN is not None


def trace_summary() -> str:
    """A compact stdout trace of the open run (2026-09-28): per call, the role, model, cost, and every
    tool it used with its first argument. For DRY test runs, whose bundle is never written, this is
    the only way to see whether a new tool was actually called. Never raises."""
    try:
        if not _RUN:
            return "[calllog] no open run"
        lines = [f"[calllog] TRACE {_RUN['source']} {_RUN['run_ts']}: {len(_RUN['calls'])} call(s)"]
        for c in _RUN["calls"]:
            res = c.get("result") or {}
            lines.append(f"  {c.get('role'):14} {c.get('model') or '?':28} ${res.get('cost_usd') or 0:.3f} "
                         f"turns={res.get('num_turns')} status={c.get('status')} slug={c.get('slug')}")
            for row in c.get("transcript") or []:
                if row.get("kind") == "tool_use":
                    inp = row.get("input") or {}
                    first = next((f"{k}={str(v)[:60]!r}" for k, v in inp.items() if v), "")
                    lines.append(f"      tool {row.get('name')} {first}")
        return "\n".join(lines)
    except Exception as e:
        return f"[calllog] trace skipped ({type(e).__name__}: {e})"


def flush_run(write: bool = True) -> list[str]:
    """Write the run's bundle ONCE (split into parts under PART_MAX_BYTES). Returns the paths
    written ([] when disabled / no run / write=False / no creds). Never raises; resets the run."""
    global _RUN
    run, _RUN = _RUN, None
    if not enabled() or run is None:
        return []
    try:
        if not write:
            print(f"[calllog] dry run: {len(run['calls'])} call(s) captured, not written", file=sys.stderr)
            return []
        if not run["calls"]:
            return []
        if not selfserve.use_github():
            print(f"[calllog] no private-store creds: {len(run['calls'])} call(s) dropped "
                  f"(prompts never land in a checkout)", file=sys.stderr)
            return []
        parts = _split_bundles(_bundle(run), PART_MAX_BYTES)
        paths = []
        for i, part in enumerate(parts, 1):
            suffix = "" if len(parts) == 1 else f".p{i}"
            path = (f"{CALLS_DIR}/{run['stamp'][:4]}-{run['stamp'][4:6]}/"
                    f"{run['source']}_{run['stamp']}_{run['run_id']}{suffix}.json")
            text = _dumps(part)
            if _write_with_retry(path, text, f"calls: capture {run['source']} {run['stamp']} "
                                             f"part {i}/{len(parts)} ({part['n_calls']} calls)"):
                paths.append(path)
            else:
                # the store is unreachable: do not re-burn the retry budget per remaining part
                print(f"[calllog] store unreachable: {len(parts) - i + 1} of {len(parts)} part(s) lost",
                      file=sys.stderr)
                break
        print(f"[calllog] {len(run['calls'])} call(s) -> {len(paths)}/{len(parts)} part(s) written"
              + (f", {run['dropped']} dropped on overflow" if run["dropped"] else ""), file=sys.stderr)
        return paths
    except Exception as e:
        print(f"[calllog] flush skipped ({type(e).__name__}: {e}); "
              f"{len(run.get('calls') or [])} call(s) lost", file=sys.stderr)
        return []


def _write_with_retry(path: str, text: str, message: str, attempts: int = 3) -> bool:
    """Retry ONLY on HTTP statuses that mean 'try again' (GitHub 5xx blips, 409/422 sha races from
    a concurrent writer on the private branch); transport errors were already retried inside
    selfserve._gh_read and anything else is a bug, so those fail the part fast. Jittered backoff so
    two writers desynchronise. A landed-but-timed-out PUT is idempotent (the sha is re-fetched)."""
    import random
    delay = 1.5
    for i in range(attempts):
        try:
            selfserve.write_data(path, text, message)
            return True
        except Exception as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            retryable = status in selfserve._TRANSIENT_STATUS or status in (409, 422)
            if not retryable or i == attempts - 1:
                print(f"[calllog] write {path} failed ({type(e).__name__}: {e}); part lost", file=sys.stderr)
                return False
            time.sleep(delay + random.uniform(0, 1.0))
            delay *= 2
    return False


# --- record construction ---------------------------------------------------------------------------
def _system_of(options) -> dict:
    sp = getattr(options, "system_prompt", None)
    if isinstance(sp, dict):
        return {"kind": "preset", "preset": sp.get("preset"), "append": _redact(sp.get("append") or "")}
    return {"kind": "string", "text": _redact(sp or "")}


def _fidelity(options) -> str:
    return "exact" if isinstance(getattr(options, "system_prompt", None), str) \
        and not (getattr(options, "allowed_tools", None) or []) else "toolsful"


def _redact(text) -> str:
    try:
        return _SECRET.sub("[REDACTED]", str(text))
    except Exception:
        return str(text)


def _call_id(run_ts: str, seq: int, role: str, prompt: str) -> str:
    ph = hashlib.sha256(str(prompt).encode("utf-8", "replace")).hexdigest()
    return "c_" + hashlib.sha256(f"{run_ts}|{seq}|{role}|{ph}".encode()).hexdigest()[:12]


def record_from(prompt: str, options, top_role: str) -> dict:
    """The single record builder: used by the _drive hook AND as drive_replay's input type."""
    run = _RUN or {"run_ts": "", "seq": 0, "source": None}
    seq = run.get("seq", 0)
    mcp = getattr(options, "mcp_servers", None) or {}
    return {
        "schema_version": SCHEMA_VERSION,
        "judgment_version": _judgment_version(),          # which private pack produced this prompt
        "instructions_sha": _instructions_sha(_system_of(options)),   # the role's instructions, as sent
        "call_id": _call_id(run.get("run_ts", ""), seq, top_role, prompt),
        "seq": seq, "run_ts": run.get("run_ts"), "source": run.get("source"),
        "slug": _CTX.get("slug"), "phase": _CTX.get("phase"),
        "context": {k: v for k, v in _CTX.items() if k not in ("slug", "phase")},   # e.g. job_id
        "role": top_role, "model": getattr(options, "model", None),
        "fidelity": _fidelity(options),
        "system": _system_of(options),
        "user": _redact(prompt),
        "options": {
            "max_turns": getattr(options, "max_turns", None),
            "max_budget_usd": getattr(options, "max_budget_usd", None),
            "allowed_tools": list(getattr(options, "allowed_tools", None) or []),
            "disallowed_tools": list(getattr(options, "disallowed_tools", None) or []),
            "mcp_servers": sorted(mcp.keys()) if isinstance(mcp, dict) else [],
            "permission_mode": getattr(options, "permission_mode", None),
        },
        "tools": None, "tool_choice": None,
        "transcript": [], "result": None, "status": None, "error": None,
        "sizes": {}, "truncated": [], "captured_at": None,
    }


class _Capture:
    """Per-call collector handed to _drive. Every method swallows its own exceptions."""

    def __init__(self, record: dict):
        self.rec = record
        self._t0 = time.monotonic()
        self._tseq = 0
        self._tbytes = 0
        self._whole = record.get("role") in WHOLE_TRANSCRIPT_ROLES

    def event(self, message, role: str | None = None) -> None:
        try:
            kind = type(message).__name__
            parent = getattr(message, "parent_tool_use_id", None)
            content = getattr(message, "content", None)
            if kind == "AssistantMessage":
                for b in content or []:
                    bk = type(b).__name__
                    if bk == "TextBlock":
                        self._row({"role": role, "kind": "text", "parent_tool_use_id": parent,
                                   "content": getattr(b, "text", "")})
                    elif bk == "ToolUseBlock":
                        self._row({"role": role, "kind": "tool_use", "parent_tool_use_id": parent,
                                   "tool_use_id": getattr(b, "id", None), "name": getattr(b, "name", None),
                                   "input": _jsonable(getattr(b, "input", None))})
            elif kind == "UserMessage":
                blocks = content if isinstance(content, list) else [content]
                for b in blocks:
                    if type(b).__name__ == "ToolResultBlock" or (isinstance(b, dict) and b.get("type") == "tool_result"):
                        g = (lambda k: b.get(k) if isinstance(b, dict) else getattr(b, k, None))
                        self._row({"role": role, "kind": "tool_result", "parent_tool_use_id": parent,
                                   "tool_use_id": g("tool_use_id"), "is_error": bool(g("is_error")),
                                   "content": _flatten_content(g("content"))})
                    elif isinstance(b, str) and b:
                        self._row({"role": role, "kind": "text", "parent_tool_use_id": parent, "content": b})
        except Exception as e:
            print(f"[calllog] event skipped ({type(e).__name__}: {e})", file=sys.stderr)

    def _row(self, row: dict) -> None:
        row = {"seq": self._tseq, **row}
        self._tseq += 1
        if isinstance(row.get("content"), str):
            row["content"] = _redact(row["content"])
        if isinstance(row.get("input"), (dict, list)) and row["input"]:
            try:
                row["input"] = json.loads(_redact(json.dumps(row["input"], ensure_ascii=False, default=str)))
            except Exception:
                row["input"] = _redact(str(row["input"]))
        if not self._whole:
            for k in ("content", "input"):
                if isinstance(row.get(k), str) and len(row[k]) > TRANSCRIPT_ROW_MAX:
                    row[k] = row[k][:TRANSCRIPT_ROW_MAX]
                    self._mark(f"transcript[{row['seq']}].{k}")
        size = len(_dumps(row))
        if not self._whole and self._tbytes + size > TRANSCRIPT_CALL_MAX:
            self._mark("transcript")
            return
        self._tbytes += size
        self.rec["transcript"].append(row)

    def _mark(self, path: str) -> None:
        if path not in self.rec["truncated"]:
            self.rec["truncated"].append(path)

    def finish(self, out: dict) -> None:
        try:
            self.rec["result"] = {k: _jsonable(out.get(k)) for k in
                                  ("text", "cost_usd", "duration_ms", "duration_api_ms", "num_turns",
                                   "by_role", "model_usage")}
            self.rec["result"]["text"] = _redact(out.get("text") or "")
            self.rec["status"] = "ok"
            self._close()
        except Exception as e:
            print(f"[calllog] finish skipped ({type(e).__name__}: {e})", file=sys.stderr)

    def fail(self, exc: BaseException, by_role: dict | None = None) -> None:
        try:
            self.rec["result"] = {"text": None, "cost_usd": None, "by_role": _jsonable(by_role or {})}
            self.rec["status"] = "failed"
            self.rec["error"] = _redact(f"{type(exc).__name__}: {exc}")[:2000]
            self._close()
        except Exception as e:
            print(f"[calllog] fail-record skipped ({type(e).__name__}: {e})", file=sys.stderr)

    def _close(self) -> None:
        r = self.rec
        r["captured_at"] = datetime.now().isoformat(timespec="seconds")
        r["wall_ms"] = int((time.monotonic() - self._t0) * 1000)
        sys_text = r["system"].get("text") or r["system"].get("append") or ""
        r["sizes"] = {"system_chars": len(sys_text), "user_chars": len(r["user"] or ""),
                      "transcript_rows": len(r["transcript"]), "transcript_chars": self._tbytes,
                      "output_chars": len((r.get("result") or {}).get("text") or ""),
                      "est_tokens_in": (len(sys_text) + len(r["user"] or "")) // 4}
        _append(r)


def start(top_role: str, prompt: str, options) -> _Capture | None:
    """Hook for _drive. None when disabled or no run is open (so per-message overhead is zero)."""
    global _WARNED_NO_RUN
    if not enabled():
        return None
    if _RUN is None:
        if not _WARNED_NO_RUN:
            print("[calllog] enabled but no run is open; not capturing (open a run in the caller)",
                  file=sys.stderr)
            _WARNED_NO_RUN = True
        return None
    try:
        rec = record_from(prompt, options, top_role)
        _RUN["seq"] += 1
        return _Capture(rec)
    except Exception as e:
        print(f"[calllog] start skipped ({type(e).__name__}: {e})", file=sys.stderr)
        return None


def _instructions_sha(system):
    try:
        from scout import judgment
        return judgment.instructions_sha(system)
    except Exception:
        return None


def _judgment_version():
    try:
        from scout import judgment
        return judgment.version()
    except Exception:
        return None


def record_direct(*, role: str, model: str, system: str, user: str, tools=None, tool_choice=None,
                  result_text: str | None, usage=None, duration_ms: int | None = None,
                  status: str = "ok", error: str | None = None) -> None:
    """A model call made outside _drive (the persona classifier). No-op with no open run."""
    if not enabled() or _RUN is None:
        return
    try:
        rec = record_from(user, _Opts(model, system), role)
        _RUN["seq"] += 1
        rec["tools"] = _jsonable(tools)
        rec["tool_choice"] = _jsonable(tool_choice)
        rec["fidelity"] = "exact"
        rec["result"] = {"text": _redact(result_text or ""), "cost_usd": None, "duration_ms": duration_ms,
                         "by_role": {role: {"input": _u(usage, "input_tokens"), "output": _u(usage, "output_tokens"),
                                            "cache_read": _u(usage, "cache_read_input_tokens"),
                                            "cache_creation": _u(usage, "cache_creation_input_tokens"),
                                            "messages": 1}}}
        rec["status"] = status
        rec["error"] = error
        rec["captured_at"] = datetime.now().isoformat(timespec="seconds")
        rec["sizes"] = {"system_chars": len(system or ""), "user_chars": len(user or ""),
                        "transcript_rows": 0, "transcript_chars": 0,
                        "output_chars": len(result_text or ""),
                        "est_tokens_in": (len(system or "") + len(user or "")) // 4}
        _append(rec)
    except Exception as e:
        print(f"[calllog] record_direct skipped ({type(e).__name__}: {e})", file=sys.stderr)


class _Opts:
    """Minimal options shape for direct-SDK calls (plain string system, no tools)."""

    def __init__(self, model, system):
        self.model = model
        self.system_prompt = system or ""
        self.allowed_tools = []
        self.disallowed_tools = []
        self.mcp_servers = {}
        self.max_turns = None
        self.max_budget_usd = None
        self.permission_mode = None


# --- helpers ----------------------------------------------------------------------------------------
def _dumps(obj) -> str:
    """Compact JSON that is always valid UTF-8. A lone surrogate (from a CLI \\udXXX escape) makes
    ensure_ascii=False output unencodable; fall back to ASCII escapes, which json.loads re-reads to
    the same string, so exact fields survive byte-for-byte on reload."""
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        return json.dumps(obj, ensure_ascii=True, separators=(",", ":"), default=str)


def _append(rec: dict) -> None:
    if _RUN is None:
        return
    if len(_RUN["calls"]) >= BUFFER_MAX_CALLS:
        _RUN["dropped"] += 1
        return
    _RUN["calls"].append(rec)


def _u(usage, key: str) -> int:
    if usage is None:
        return 0
    try:
        v = usage.get(key) if isinstance(usage, dict) else getattr(usage, key, None)
        return int(v or 0)
    except Exception:
        return 0


def _jsonable(x):
    try:
        json.dumps(x)
        return x
    except Exception:
        try:
            return json.loads(json.dumps(x, default=str))
        except Exception:
            return str(x)


def _flatten_content(content) -> str:
    """ToolResultBlock.content is str | list[dict] | None -> one string, whole."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict):
                parts.append(str(c.get("text") if c.get("type") == "text" else json.dumps(c, default=str)))
            else:
                parts.append(str(c))
        return "\n".join(parts)
    return str(content)


def _bundle(run: dict) -> dict:
    return {"schema_version": SCHEMA_VERSION, "run_ts": run["run_ts"], "source": run["source"],
            "stamp": run["stamp"], "run_id": run["run_id"], "part": 1, "parts": 1,
            "n_calls": len(run["calls"]),
            "total_cost_usd": round(sum((c.get("result") or {}).get("cost_usd") or 0.0
                                        for c in run["calls"]), 6),
            "dropped": run["dropped"], "calls": run["calls"]}


def _split_bundles(doc: dict, max_bytes: int) -> list[dict]:
    """Split by calls so every part serializes under max_bytes (a single oversized call gets its
    own part; it is still written whole — exact fields are never clipped)."""
    calls = doc["calls"]
    head = {k: v for k, v in doc.items() if k != "calls"}
    base = len(_dumps({**head, "calls": []}).encode("utf-8"))
    parts, cur, size = [], [], base
    for c in calls:
        cs = len(_dumps(c).encode("utf-8")) + 1
        if cur and size + cs > max_bytes:
            parts.append(cur)
            cur, size = [], base
        cur.append(c)
        size += cs
    if cur or not parts:
        parts.append(cur)
    out = []
    for i, chunk in enumerate(parts, 1):
        out.append({**head, "part": i, "parts": len(parts), "n_calls": len(chunk), "calls": chunk})
    return out
