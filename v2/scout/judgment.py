"""The judgment pack: Scout's instruction text (prompts, contracts, rules, methodology) lives in
the PRIVATE store, not in this public repository (Uroš, 2026-10-01: "all my judgement passed to it
is what makes it unique"). This module is the only door to it.

A block is addressed as "<module>.<NAME>" (e.g. "propagate._JUDGE_SYSTEM"). Modules keep their
names: `_JUDGE_SYSTEM = judgment.get("propagate._JUDGE_SYSTEM")`. A block composed from code values
is stored as a template whose ⟦expr⟧ tokens are filled from the `subs` the module passes
(`judgment.get("monitor._TRIAGE_SYSTEM", {"config.TRIAGE_MAX_SEARCHES": config.TRIAGE_MAX_SEARCHES})`).

Where the pack comes from, first hit wins:
  1. SCOUT_JUDGMENT_PACK   a file path (tests use tests/fixtures/judgment_stub.json; offline work)
  2. the local mirror      ~/code/scout-user-data/[rc/]judgment/pack.json (the mini, development)
  3. the private store     judgment/pack.json over the API, with the store's own retries
     (GitHub Actions, the engine on Cloud Run; honours the rc data prefix)

Failing closed without taking a process down: if no source answers at import time, `get` returns a
marked placeholder so modules still import (the engine's read-only tools keep working), and
`require()`, called at every model-call choke point, retries the load, re-fills every block in the
modules that asked for one, and raises JudgmentUnavailable if the pack still cannot be read. No
prompt containing a placeholder can reach a model.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

SENTINEL = "⟦JUDGMENT-UNAVAILABLE⟧"
MIRROR = os.path.expanduser(os.environ.get("SCOUT_STORE_MIRROR", "~/code/scout-user-data"))
PACK_PATH = "judgment/pack.json"
# The pack frozen from the public modules on 2026-10-02: byte-identical to the text the models
# received before the move, so results captured without a version belong to the same period.
BASELINE_VERSION = "4375a93e1804ed5f"

_pack: dict | None = None
_asked: list[tuple[str, dict | None]] = []          # (qualified name, subs) in the order modules asked
_stale = False                                      # a module was handed a placeholder: refill on load


class JudgmentUnavailable(RuntimeError):
    pass


def _prefix() -> str:
    return (os.environ.get("SCOUT_SELFSERVE_DATA_PREFIX") or "").strip().strip("/")


def _read_sources() -> dict | None:
    explicit = os.environ.get("SCOUT_JUDGMENT_PACK")
    if explicit:
        with open(explicit) as f:
            return json.load(f)
    pre = _prefix()
    for rel in ([f"{pre}/{PACK_PATH}"] if pre else []) + [PACK_PATH]:
        fp = os.path.join(MIRROR, rel)
        if os.path.isfile(fp):
            with open(fp) as f:
                return json.load(f)
        if pre:                                    # an rc run reads rc/ first, then production's pack
            continue
    try:
        from scout import selfserve
        raw = selfserve.read_data(PACK_PATH)
        if raw is None and pre:                    # rc prefix without read-through: production's pack
            raw = selfserve._gh_get(PACK_PATH, prefixed=False)[0] if selfserve.use_github() else None
        return json.loads(raw) if raw else None
    except Exception as e:
        print(f"[judgment] pack not readable from the store ({type(e).__name__}: {str(e)[:120]})", file=sys.stderr)
        return None


def _load(force: bool = False) -> dict | None:
    global _pack
    if _pack is None or force:
        try:
            _pack = _read_sources()
        except Exception as e:
            print(f"[judgment] pack not loaded ({type(e).__name__}: {str(e)[:120]})", file=sys.stderr)
            _pack = None
    return _pack


def render(template: str, subs: dict | None) -> str:
    out = template
    for expr, value in (subs or {}).items():
        out = out.replace(f"⟦{expr}⟧", str(value))
    return out


def get(name: str, subs: dict | None = None) -> str:
    """The block's text as the model receives it. Never raises at import: an unavailable pack
    yields a marked placeholder and `require()` settles it before any model call."""
    entry = (name, dict(subs) if subs else None)
    for i, (n, _) in enumerate(_asked):            # one entry per block, the latest subs
        if n == name:
            _asked[i] = entry
            break
    else:
        _asked.append(entry)
    pack = _load()
    if pack is None or name not in (pack.get("blocks") or {}):
        global _stale
        _stale = True
        return f"{SENTINEL} {name}"
    return render(pack["blocks"][name], subs)


def text(name: str, subs: dict | None = None) -> str:
    """A block rendered for a prompt that is built inside a function (per run), not held in a
    module constant. Same placeholder rule as `get`; `assert_clean` at the choke point stops a
    prompt that was built before the pack loaded."""
    pack = _load()
    if pack is None or name not in (pack.get("blocks") or {}):
        return f"{SENTINEL} {name}"
    return render(pack["blocks"][name], subs)


def optional(name: str, subs: dict | None = None) -> str | None:
    """A block a feature may do without: None when the pack or the block is absent. Unlike `get`
    it registers nothing, so `require()` never fails other model calls over a block that is not
    there yet (audience leads, 2026-10-02: the feature skips itself instead)."""
    pack = _load()
    if pack is None or name not in (pack.get("blocks") or {}):
        return None
    return render(pack["blocks"][name], subs)


def assert_clean(*texts) -> None:
    """No prompt carrying a placeholder may reach a model."""
    for t in texts:
        if t is not None and SENTINEL in str(t):
            raise JudgmentUnavailable("a prompt was built before the judgment pack was readable; "
                                      "nothing was sent. The next attempt rebuilds it.")


def available() -> bool:
    return _load() is not None


def version() -> str | None:
    pack = _load()
    return (pack or {}).get("version")


def _refill() -> None:
    """Re-fill every block in the modules that asked for one (after a late load). Template values
    are re-evaluated in the asking module's namespace so blocks composed from other blocks settle."""
    pack = _pack or {}
    for name, subs in _asked:
        if name not in (pack.get("blocks") or {}):
            continue
        mod_name, _, attr = name.partition(".")
        mod = sys.modules.get(f"scout.{mod_name}")
        if mod is None:
            continue
        fresh = {k: eval(k, mod.__dict__) for k in (subs or {})} if subs else None   # noqa: S307 (our own source exprs)
        value = render(pack["blocks"][name], fresh)
        target, _, field = attr.partition(".")
        if field:                                  # "RESEARCHER.prompt": a field on a module object
            setattr(getattr(mod, target), field, value)
        else:
            setattr(mod, target, value)


def require() -> None:
    """Call before any model call. Raises JudgmentUnavailable unless every asked block is real."""
    global _stale
    pack = _load() or _load(force=True)
    if pack is None:
        raise JudgmentUnavailable("Scout's judgment pack could not be read from the private store; "
                                  "nothing is generated without it.")
    if _stale:                                      # the pack arrived after a module imported
        _refill()
        _stale = False
    missing = [n for n, _ in _asked if n not in (pack.get("blocks") or {})]
    if missing:
        raise JudgmentUnavailable(f"the judgment pack has no block for: {', '.join(sorted(set(missing)))}")


def period_tag(v: str | None) -> str | None:
    """None for the baseline pack (and for results captured before versions were recorded): a prompt
    edit yields a new tag, which opens a new eval period by construction."""
    return None if v in (None, BASELINE_VERSION) else v


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
