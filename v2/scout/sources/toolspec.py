"""The structured-source tools, declared ONCE (WS1, 2026-09-28) and exposed by thin adapters:
the Agent SDK MCP server (scout/sources_tool.py, for the live monitor / generate / Ask) and the
OpenAI-shaped list the on-device replay loop uses (scout/localagent.py). A test asserts the two
adapters expose identical names and parameter schemas, so the local models get exactly the tools
the paid model had.

Every tool returns (header, body). The header is ONE line the model uses to cite correctly:

    SOURCE url=<the URL to cite> class=<source_class> tier=<source_tier> as_of=<YYYY-MM-DD>

and it travels as a SEPARATE content block (plan C22), never inline with page text, so it cannot be
copied into an evidence_excerpt. The body is what the model reads. Nothing here calls a model.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Callable

from scout.sources import edgar, jobs, registry, wayback


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    params: dict          # JSON-schema `properties`
    required: tuple
    fn: Callable[[dict], tuple[str, str]]


def _header(url: str, cls: str, tier: str, as_of: str) -> str:
    return f"SOURCE url={url} class={cls} tier={tier} as_of={as_of}"


# --- sec_filings ---------------------------------------------------------------------------------
def _sec_filings(args: dict) -> tuple[str, str]:
    company = str(args.get("company") or "").strip()
    cik = args.get("cik") or registry.cik_for(company)
    if not cik:
        return "", f"No SEC filer on file for {company or 'that company'} (a private company, or not in Scout's registry). Use web search for its news instead."
    forms = args.get("forms") or list(edgar.DEFAULT_FORMS)
    if isinstance(forms, str):
        forms = [f.strip() for f in forms.split(",") if f.strip()]
    try:
        rows = edgar.filings(cik, forms=tuple(forms), since=args.get("since"), limit=int(args.get("limit") or 15))
        name = edgar.company_name(cik)
    except Exception as e:
        return "", f"EDGAR unreachable right now ({type(e).__name__}). Do not guess a filing; say the filing index could not be read."
    idx = f"{edgar.SEC_WWW}/cgi-bin/browse-edgar?action=getcompany&CIK={int(cik):010d}"
    return _header(idx, "filing", "primary", edgar.as_of_today()), edgar.render_filings_text(rows, name)


# --- sec_fact ------------------------------------------------------------------------------------
def _sec_fact(args: dict) -> tuple[str, str]:
    company = str(args.get("company") or "").strip()
    cik = args.get("cik") or registry.cik_for(company)
    if not cik:
        return "", f"No SEC filer on file for {company or 'that company'} (a private company, or not in Scout's registry); there is no audited figure to cite."
    concept = str(args.get("concept") or "revenue")
    try:
        r = edgar.fact_any(cik, concept, forms=tuple(args.get("forms") or ("10-Q", "10-K")), limit=int(args.get("limit") or 8))
    except Exception as e:
        return "", f"EDGAR unreachable right now ({type(e).__name__}). Do not guess the figure."
    if not r:
        known = ", ".join(sorted(edgar.CONCEPTS))
        return "", f"No reported values for '{concept}' at CIK {cik}. Plain names Scout knows: {known}; or pass an exact us-gaap concept name."
    as_of = r["rows"][0]["end"] if r["rows"] else edgar.as_of_today()
    return _header(r["url"], "filing", "primary", str(as_of)), edgar.render_fact_text(r)


# --- job_postings --------------------------------------------------------------------------------
def _job_postings(args: dict) -> tuple[str, str]:
    company = str(args.get("company") or "").strip()
    host, token = (args.get("host"), args.get("token")) if args.get("host") and args.get("token") else (registry.board_for(company) or (None, None))
    if not host:
        return "", f"No public job board on file for {company or 'that company'} (it runs its own careers site, or is not in Scout's registry)."
    try:
        rows = jobs.postings(host, token)
    except Exception as e:
        return "", f"The {host} board for {company} is unreachable right now ({type(e).__name__})."
    return _header(jobs.board_url(host, token), "job_posting", "primary", date.today().isoformat()), jobs.summarize(rows)


# --- page_history --------------------------------------------------------------------------------
def _page_history(args: dict) -> tuple[str, str]:
    url = str(args.get("url") or "").strip()
    if not url.startswith("http"):
        return "", "page_history needs an http(s) url."
    ts = args.get("timestamp")
    try:
        if ts:
            text, snap = wayback.snapshot_text(url, str(ts))
            return _header(snap, "page_snapshot", "primary", f"{str(ts)[:4]}-{str(ts)[4:6]}-{str(ts)[6:8]}"), text or "(empty page)"
        rows = wayback.snapshots(url, since=args.get("since"), limit=int(args.get("limit") or 15))
    except Exception as e:
        return "", f"The Wayback Machine is unreachable or rate-limited right now ({type(e).__name__}); try again later."
    return "", wayback.render_snapshots_text(url, rows)


TOOLS: list[ToolSpec] = [
    ToolSpec("sec_filings",
             "SEC EDGAR filings index for a US-listed company (8-K, 10-Q, 10-K, Form D ...), newest first, with the "
             "primary document URL of each filing. Use for anything a public company must disclose: earnings, "
             "material events, executive changes, financing. Private companies have no filings.",
             {"company": {"type": "string", "description": "company name or ticker"},
              "forms": {"type": "string", "description": "comma list, e.g. 8-K,10-Q (default: the common forms)"},
              "since": {"type": "string", "description": "YYYY-MM-DD; only filings on/after this date"},
              "limit": {"type": "integer"}}, ("company",), _sec_filings),
    ToolSpec("sec_fact",
             "An audited XBRL figure straight from a company's SEC filings (revenue, net income, operating income, "
             "RPO, cash, R&D, sales and marketing, employees, shares, or an exact us-gaap concept name), newest "
             "period first. Cite the returned URL and copy ONE returned line verbatim as the evidence_excerpt; "
             "never paraphrase the number.",
             {"company": {"type": "string"}, "concept": {"type": "string", "description": "plain name (revenue) or us-gaap concept"},
              "limit": {"type": "integer"}}, ("company", "concept"), _sec_fact),
    ToolSpec("job_postings",
             "A company's public job board (Greenhouse / Ashby / Lever): open-posting count by department and the "
             "newest postings with URLs. Hiring is a leading indicator of what a company is building; the board is "
             "the company's own statement.",
             {"company": {"type": "string"}}, ("company",), _job_postings),
    ToolSpec("page_history",
             "Archived copies of a web page from the Wayback Machine. Without a timestamp: the list of dated captures "
             "(newest first). With a timestamp (from that list): the page text as it was on that date. Use to see "
             "what a pricing or product page said before a change.",
             {"url": {"type": "string"}, "since": {"type": "string", "description": "YYYYMM or YYYYMMDD"},
              "timestamp": {"type": "string", "description": "a capture timestamp from the list"},
              "limit": {"type": "integer"}}, ("url",), _page_history),
]
BY_NAME = {t.name: t for t in TOOLS}
NAMES = tuple(t.name for t in TOOLS)


def run(name: str, args: dict) -> tuple[str, str]:
    """(header, body) for a tool call; unknown tools and crashes come back as text, never raise."""
    spec = BY_NAME.get(name)
    if not spec:
        return "", f"unknown tool {name!r}"
    try:
        return spec.fn(dict(args or {}))
    except Exception as e:                       # the tool must never take the run down
        return "", f"{name} failed ({type(e).__name__}: {e})"


def openai_tools() -> list[dict]:
    """The OpenAI function-calling shape (the replay loop's tools list)."""
    return [{"type": "function", "function": {"name": t.name, "description": t.description,
                                              "parameters": {"type": "object", "properties": t.params, "required": list(t.required)}}}
            for t in TOOLS]


def joined(header: str, body: str) -> str:
    """One string for transports that carry a single text (the OpenAI-shaped loop)."""
    return f"[{header}]\n\n{body}" if header else body
