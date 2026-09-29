"""Company registry for the structured sources (WS1, 2026-09-28): which SEC filer and which public
job board a company name maps to. Hand-maintained data, verified read-only on 2026-09-28 (board
counts in the comments are that day's). A card's `meta.watch` (WS3) can override any of it per card.

Lookup is by name token, the same idea as classify.is_company_host: "OpenAI", "openai" and
"OpenAI, Inc." all resolve to the `openai` entry. Unknown companies resolve to None and the tools
say so rather than guessing.
"""
from __future__ import annotations

import re

_TOKEN = re.compile(r"[a-z0-9]+")

# key -> {cik, ticker, board: (host, token), names: [aliases]}
COMPANIES = {
    "anthropic":  {"cik": None, "ticker": None, "board": ("greenhouse", "anthropic"), "names": ["anthropic", "claude"]},          # 627 postings
    "openai":     {"cik": None, "ticker": None, "board": ("ashby", "openai"), "names": ["openai", "chatgpt"]},                    # 840
    "notion":     {"cik": None, "ticker": None, "board": ("ashby", "notion"), "names": ["notion"]},                               # 130
    "perplexity": {"cik": None, "ticker": None, "board": ("ashby", "perplexity"), "names": ["perplexity"]},                       # 123
    "cursor":     {"cik": None, "ticker": None, "board": ("ashby", "cursor"), "names": ["cursor", "anysphere"]},                  # 128
    "cognition":  {"cik": None, "ticker": None, "board": ("ashby", "cognition"), "names": ["cognition", "devin"]},                # 104
    "mistral":    {"cik": None, "ticker": None, "board": None, "names": ["mistral", "mistralai"]},                                # own careers site
    "atlassian":  {"cik": 1650372, "ticker": "TEAM", "board": None, "names": ["atlassian", "jira", "confluence"]},
    "salesforce": {"cik": 1108524, "ticker": "CRM", "board": None, "names": ["salesforce", "agentforce", "slack"]},
    "hubspot":    {"cik": 1404655, "ticker": "HUBS", "board": None, "names": ["hubspot", "breeze"]},
    "alphabet":   {"cik": 1652044, "ticker": "GOOGL", "board": None, "names": ["alphabet", "google", "gemini", "deepmind"]},
    "microsoft":  {"cik": 789019, "ticker": "MSFT", "board": None, "names": ["microsoft", "azure", "copilot", "teams", "github"]},
    "amazon":     {"cik": 1018724, "ticker": "AMZN", "board": None, "names": ["amazon", "aws", "bedrock"]},
    "meta":       {"cik": 1326801, "ticker": "META", "board": None, "names": ["meta", "facebook", "llama"]},
}

_BY_NAME = {n: key for key, v in COMPANIES.items() for n in v["names"]}


def resolve(name) -> dict | None:
    """The registry entry for a company name (any alias, any casing), with its key, or None."""
    for t in _TOKEN.findall(str(name or "").lower()):
        key = _BY_NAME.get(t)
        if key:
            return {"key": key, **COMPANIES[key]}
    return None


def cik_for(name) -> int | None:
    e = resolve(name)
    return e["cik"] if e else None


def board_for(name) -> tuple[str, str] | None:
    e = resolve(name)
    return tuple(e["board"]) if e and e.get("board") else None
