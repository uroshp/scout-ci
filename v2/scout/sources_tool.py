"""`scoutsources`: the structured-source tools as an in-process Agent SDK MCP server (WS1, 2026-09-28),
the sibling of scout/fetch_tool.py's `scoutfetch`. Declared once in scout/sources/toolspec.py; this
file only adapts. The header line travels as its own content block so it can never be copied into
an evidence_excerpt (plan C22).

Wired into the tools-on call sites behind config.SOURCES_TOOLS_ENABLED (SCOUT_SOURCES_TOOLS=1):
materiality, my_facts, the generation orchestrator and its subagents, Ask (WS2); triage gets only
job_postings (cheap). Off = the live prompts are byte-identical to before.
"""
from __future__ import annotations

from claude_agent_sdk import create_sdk_mcp_server, tool

from scout.sources import toolspec
from scout import judgment

_SCHEMA_TYPES = {"string": str, "integer": int, "number": float, "boolean": bool}


def _sdk_schema(spec: toolspec.ToolSpec) -> dict:
    # The SDK's simple form ({name: type}); it turns this into a JSON schema itself.
    return {k: _SCHEMA_TYPES.get(v.get("type", "string"), str) for k, v in spec.params.items()}


def _make(spec: toolspec.ToolSpec):
    async def _handler(args):
        header, body = toolspec.run(spec.name, args)
        content = ([{"type": "text", "text": header}] if header else []) + [{"type": "text", "text": body}]
        return {"content": content}
    _handler.__name__ = spec.name
    return tool(spec.name, spec.description, _sdk_schema(spec))(_handler)


SOURCES_TOOLS = [_make(t) for t in toolspec.TOOLS]
SOURCES_SERVER = create_sdk_mcp_server("scoutsources", tools=SOURCES_TOOLS)
SOURCES_TOOL_NAMES = [f"mcp__scoutsources__{t.name}" for t in toolspec.TOOLS]
TRIAGE_TOOL_NAMES = ["mcp__scoutsources__job_postings"]      # the cheap one for the triage gate


# --- wiring helpers (flag-gated; everything is a no-op when SCOUT_SOURCES_TOOLS is off) ------------
# The tools are DEFERRED under the claude_code preset: the model loads them by EXACT name through
# ToolSearch (a dry run on 2026-09-28 showed `select:WebSearch,job_postings` failing to load the tool
# because the short name was used), so the notes name them in full.
PROMPT_NOTE = (
    judgment.get("sources_tool.PROMPT_NOTE")
)
TRIAGE_NOTE = (
    judgment.get("sources_tool.TRIAGE_NOTE")
)


def _on() -> bool:
    from scout import config
    return bool(config.SOURCES_TOOLS_ENABLED)


def servers() -> dict:
    """`mcp_servers` entries to merge in (empty when off)."""
    return {"scoutsources": SOURCES_SERVER} if _on() else {}


def names(role: str = "full") -> list:
    """`allowed_tools` names to add: the cheap one for triage, all four elsewhere (empty when off)."""
    if not _on():
        return []
    return list(TRIAGE_TOOL_NAMES) if role == "triage" else list(SOURCES_TOOL_NAMES)


def note(role: str = "full") -> str:
    """The system-prompt paragraph to append (empty when off, so live prompts stay byte-identical)."""
    if not _on():
        return ""
    return TRIAGE_NOTE if role == "triage" else PROMPT_NOTE
