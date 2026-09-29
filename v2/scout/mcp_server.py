"""Scout over MCP (WS4, 2026-09-29): the cards and Ask as tools another agent can call.

  list_battlecards()                       the roster with freshness
  get_battlecard(slug, section?, persona?) a card as markdown (the whole card, or one section)
  recent_changes(slug, days=7)             the card's material changes and applied updates
  sources(slug)                            every citation on the card with its kind and tier
  ask_scout(question, mode, competitor?)   Ask Scout (quick: from what Scout already verified,
                                           ~1 min; deep: web research, minutes); metered

Reads are free and unauthenticated. `ask_scout` spends money, so over HTTP it needs an owner key
(ASK_API_KEYS) and goes through the same ledger as the panel (Uroš 2026-09-29: owner-key only).

Transports: `python -m scout.mcp_server` is stdio (the "add Scout to Claude Code" demo:
`claude mcp add scout -- python -m scout.mcp_server`, run from v2/); engine/app.py mounts the
same server at /mcp (Streamable HTTP) behind SCOUT_MCP=1.
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, timedelta

import functools

import anyio
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from scout import config, display, store
from scout.render import claims_to_markdown
from scout.schema import SECTIONS, PERSONAS

SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{2,120}$")
# DNS-rebinding protection is for localhost servers; this one is served at a public Cloud Run
# hostname behind a bearer gate for the only tool that spends, so the host check is off.
# stateless + JSON responses: Cloud Run may serve consecutive requests from different instances,
# so no server-side session state; a client gets a plain JSON body per call.
mcp = FastMCP("scout", streamable_http_path="/mcp", stateless_http=True, json_response=True,
              transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
              instructions=(
    "Scout keeps verified competitive battlecards. Every claim on a card was checked against its source by "
    "code and a second model; the Cut Log lists what did not survive. Use get_battlecard for the prepared "
    "brief, sources for the evidence, recent_changes for what moved, and ask_scout for a question the cards "
    "do not answer directly (quick answers come from what Scout already verified; deep answers search)."))


def _label(meta: dict) -> str:
    return f"{meta.get('my_company')} vs {meta.get('competitor')}" if meta.get("my_company") and meta.get("competitor") else meta.get("competitor") or ""


def _slug_ok(slug: str) -> str:
    slug = str(slug or "").strip()
    if not SLUG.fullmatch(slug) or slug not in display.list_battlecards():
        raise ValueError(f"unknown battlecard: {slug!r}; call list_battlecards")
    return slug


@mcp.tool()
def list_battlecards() -> list[dict]:
    """The battlecards Scout keeps: slug, the two companies, the focus, when it was last checked, how many claims it holds."""
    out = []
    for slug in display.list_battlecards():
        meta = store.load_meta(slug) or {}
        claims = [c for c in store.load_claims(slug) if c.get("status", "active") != "retired"]
        out.append({"slug": slug, "card": _label(meta), "competitor": meta.get("competitor"), "my_company": meta.get("my_company"),
                    "focus": meta.get("focus"), "last_checked": meta.get("last_checked"), "claims": len(claims),
                    "url": f"https://agent-scout.ai/c/{slug}"})
    return out


@mcp.tool()
def get_battlecard(slug: str, section: str | None = None, persona: str | None = None) -> str:
    """A battlecard as markdown. `section` narrows to one of: executive_summary, snapshot, recent_moves, positioning, pricing, battlecard, sentiment, objection_handling. `persona` (eng_led, technical_evaluator, economic_buyer, security_regulated, exec_top_down) puts that audience's plays and objections first."""
    slug = _slug_ok(slug)
    meta = store.load_meta(slug) or {}
    claims = [c for c in store.load_claims(slug) if c.get("status", "active") != "retired"]
    if section:
        if section not in SECTIONS:
            raise ValueError(f"unknown section {section!r}; one of {', '.join(SECTIONS)}")
        claims = [c for c in claims if c.get("section") == section]
    if persona:
        if persona not in PERSONAS:
            raise ValueError(f"unknown persona {persona!r}; one of {', '.join(PERSONAS)}")
        claims.sort(key=lambda c: 0 if c.get("persona") == persona else 1)
    md = claims_to_markdown(claims, _label(meta), my_company=meta.get("my_company"), competitor=meta.get("competitor"))
    return md + f"\n\n---\nVerified by Scout · last checked {meta.get('last_checked') or 'unknown'} · https://agent-scout.ai/c/{slug}\n"


@mcp.tool()
def recent_changes(slug: str, days: int = 7) -> list[dict]:
    """What moved on a card in the last `days` days: material changes (old -> new, why it matters, source) and applied updates."""
    slug = _slug_ok(slug)
    since = (date.today() - timedelta(days=max(1, min(int(days), 90)))).isoformat()
    rows = []
    for a in display.load_alerts(slug):
        when = str(a.get("detected_at") or a.get("date") or "")[:10]
        if when < since:
            continue
        rows.append({k: a.get(k) for k in ("detected_at", "headline", "old_value", "new_value", "so_what", "severity", "source_url", "subject_key", "triggered_by") if a.get(k) is not None})
    return sorted(rows, key=lambda r: str(r.get("detected_at") or ""), reverse=True)


@mcp.tool()
def sources(slug: str) -> list[dict]:
    """Every citation the card rests on: url, kind (filing, company statement, news, research, review site, forum, government, unknown), tier, as-of date, and the claims it supports."""
    slug = _slug_ok(slug)
    from scout import page
    meta = store.load_meta(slug) or {}
    active, _ = page._prepare_display(store.load_claims(slug), meta)
    by_url: dict = {}
    for c in active:
        url = c.get("source_url")
        if not url:
            continue
        s = by_url.setdefault(url, {"url": url, "class": c.get("source_class") or "unknown", "tier": c.get("source_tier"),
                                    "as_of": c.get("as_of"), "claims": []})
        s["claims"].append({"id": c.get("id"), "subject_key": c.get("subject_key"), "claim": (c.get("claim") or "")[:300]})
    return list(by_url.values())


def _run_ask(question: str, mode: str, competitor: str | None) -> dict:
    """The metered call, in a worker thread: ask.* drives the Agent SDK with asyncio.run, which
    cannot run inside the MCP server's own event loop. Same ledger discipline as the panel."""
    from scout import ask, ledger
    L = ledger.Ledger("ask/state.json", float(os.environ.get("SCOUT_ASK_DAILY_CEILING_USD", "10")), config.ASK_MAX_USD)
    reserve = config.ASK_MAX_USD if mode == "deep" else config.ASK_QUICK_MAX_USD
    ok, state = L.start(reserve)
    if not ok:
        raise RuntimeError("today's Ask budget is spent; try again tomorrow")
    try:
        fn = ask.ask if mode == "deep" else ask.quick_ask
        a = fn(question, competitor=competitor, persist=True)
        L.settle(a["cost_usd"], reserve)
        return a
    except Exception as e:
        spent = getattr(e, "scout_cost_usd", None)
        L.settle(reserve if spent is None else float(spent), reserve)     # unknown cost: fail closed
        raise


@mcp.tool()
async def ask_scout(question: str, mode: str = "quick", competitor: str | None = None, ctx: Context = None) -> dict:
    """Ask Scout a question about a tracked competitor. mode "quick" (default) answers in about a minute from everything Scout already verified; "deep" searches the web and takes minutes. Every sentence returned passed Scout's verifier; `cut_log` says what did not. Metered: over HTTP this needs the owner's bearer key."""
    question = str(question or "").strip()
    if not question or len(question) > 400:
        raise ValueError("a question of up to 400 characters")
    mode = "deep" if mode == "deep" else "quick"
    # auth inside the tool (Uroš: owner-key only): over HTTP the request carries the bearer; over
    # stdio there is no request and the caller is the local owner
    req = None
    try:
        req = ctx.request_context.request if ctx is not None else None
    except Exception:
        req = None
    if req is not None:
        from scout.asktoken import owner_key_ok
        if not owner_key_ok(req.headers.get("authorization")):
            raise PermissionError("ask_scout needs the owner's bearer key (Authorization: Bearer …)")
    a = await anyio.to_thread.run_sync(functools.partial(_run_ask, question, mode, competitor))
    return {"id": a["id"], "kind": a.get("kind", mode), "about": a.get("card") or a.get("competitor"),
            "paragraphs": a["paragraphs"], "sources": [{k: s.get(k) for k in ("n", "url", "class", "tier", "as_of", "excerpt")} for s in a["sources"]],
            "cut_log": a["cut_log"], "unanswered": a["unanswered"], "verified": a["verified"], "seconds": a["seconds"],
            "cost_usd": a["cost_usd"], "permalink": f"https://agent-scout.ai/answers/{a['id']}"}


def stdio():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    stdio()
