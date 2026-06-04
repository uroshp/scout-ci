"""Custom in-process fetch tool that REPLACES WebFetch (build: WebFetch swap).

Why: the SDK's WebFetch is model-mediated — it runs every page through Haiku and
returns a SUMMARY. That cost ~46% of a run AND caused grounding failures (agents
copied "excerpts" from a paraphrase, so independent re-fetch couldn't find them).

This tool fetches the REAL page over our own httpx (provenance: same fetcher as
grounding, NO model in the path) and returns KEYWORD-WINDOWED passages around the
agent's `query` — not a blunt first-N-chars cap — so facts deep in long pages still
surface. Every call is logged for the coverage read.
"""
import asyncio
import re

from claude_agent_sdk import tool, create_sdk_mcp_server

from scout.grounding import _fetch_response, _extract_text

WINDOW_BUDGET_CHARS = 12000   # total returned per fetch
WINDOW_RADIUS = 700           # chars of context around each keyword hit
BLUNT_CAP = 12000             # the hypothetical first-N-chars cap we compare against

FETCH_LOG: list[dict] = []    # per-call coverage instrumentation (in-process, shared)

_STOP = set((
    "the a an of to in on for and or is are was were be by with at from as that this "
    "it its their our your they we you have has had will would can could".split()
))


def reset_log():
    FETCH_LOG.clear()


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _terms(query: str) -> list[str]:
    toks = re.findall(r"[a-z0-9$%.]+", (query or "").lower())
    return [t for t in toks if len(t) >= 3 and t not in _STOP]


def _window(page: str, query: str):
    """Return (text, windowed, returned_len, max_end). Keyword windows merged in
    document order, budget-capped; head fallback if no query terms are found."""
    terms = _terms(query)
    low = page.lower()
    hits = []
    for t in terms:
        start = 0
        while len(hits) < 400:
            i = low.find(t, start)
            if i == -1:
                break
            hits.append(i)
            start = i + len(t)
    if not hits:
        head = page[:WINDOW_BUDGET_CHARS]
        return head, False, len(head), len(head)

    hits.sort()
    spans = []
    for h in hits:
        s, e = max(0, h - WINDOW_RADIUS), min(len(page), h + WINDOW_RADIUS)
        if spans and s <= spans[-1][1]:
            spans[-1] = (spans[-1][0], max(spans[-1][1], e))
        else:
            spans.append((s, e))

    out, total, max_end = [], 0, 0
    for s, e in spans:
        if total >= WINDOW_BUDGET_CHARS:
            break
        chunk = page[s:e]
        out.append(chunk)
        total += len(chunk)
        max_end = max(max_end, e)
    return " […] ".join(out), True, total, max_end


@tool(
    "fetch_page",
    "Fetch a URL and return the REAL page text (no AI summary). Pass `query` = the "
    "specific thing you are looking for; the tool returns the passages around it so "
    "you can copy a verbatim span. Use this instead of any web-fetch tool.",
    {"url": str, "query": str},
)
async def fetch_page(args):
    url = args.get("url", "")
    query = args.get("query", "")
    try:
        resp = await asyncio.to_thread(_fetch_response, url)
        if resp is None or resp.status_code >= 400:
            FETCH_LOG.append({"url": url, "query": query, "error": f"HTTP {getattr(resp,'status_code','?')}"})
            return {"content": [{"type": "text", "text": f"FETCH_ERROR HTTP {getattr(resp,'status_code','?')}"}]}
        text, _kind = _extract_text(resp)
        page = _collapse(text)
        windowed_text, windowed, ret_len, max_end = _window(page, query)
        FETCH_LOG.append({
            "url": url, "query": query, "page_len": len(page),
            "returned_len": ret_len, "windowed": windowed, "max_end": max_end,
        })
        return {"content": [{"type": "text", "text": windowed_text}]}
    except Exception as e:
        FETCH_LOG.append({"url": url, "query": query, "error": type(e).__name__})
        return {"content": [{"type": "text", "text": f"FETCH_ERROR {type(e).__name__}"}]}


FETCH_SERVER = create_sdk_mcp_server("scoutfetch", tools=[fetch_page])
FETCH_TOOL_NAME = "mcp__scoutfetch__fetch_page"
