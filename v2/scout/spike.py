"""Build step 1 (v2-agent-spec.md §15): SDK scaffold spike on the headless runtime.

Goal: prove the Agent SDK loop runs here with one orchestrator + one subagent,
that WebSearch/WebFetch actually execute, that per-agent model assignment and the
guard knobs (max_turns, max_budget_usd) work, and observe the real tool name the
orchestrator uses to invoke a subagent. This is a throwaway probe, not the engine.

Run:  python -m scout.spike
"""
import asyncio

from claude_agent_sdk import AgentDefinition, ClaudeAgentOptions, query

from scout import config

config.require_api_key()

# One subagent: the researcher. Its only capabilities are the two web tools —
# WebSearch (reused from v1) and WebFetch (the new "read the source" capability).
RESEARCHER = AgentDefinition(
    description="Researches a company using web search and by reading sources.",
    prompt=(
        "You are a competitive-intelligence researcher. Use WebSearch to find "
        "recent, reputable sources and WebFetch to read them. Report concise, "
        "sourced findings with the URL you actually read."
    ),
    tools=["WebSearch", "WebFetch"],
    model=config.SUBAGENT_MODEL,  # per-agent model assignment (§3)
)

ORCHESTRATOR_PROMPT = (
    "You orchestrate a competitive-intelligence task. Delegate the actual web "
    "research to your 'researcher' subagent — do not search yourself. Ask it to "
    "find Anthropic's most recent model release and the announcement date, with a "
    "source URL it read. Then state the answer in one sentence and stop."
)


async def main() -> None:
    options = ClaudeAgentOptions(
        model=config.ORCHESTRATOR_MODEL,           # orchestrator on Opus (§3)
        agents={"researcher": RESEARCHER},
        # The orchestrator may invoke subagents (the tool is named "Agent" in
        # this SDK version) and, as a fallback, the web tools directly.
        allowed_tools=["Agent", "WebSearch", "WebFetch"],
        permission_mode="bypassPermissions",       # headless: no interactive prompts
        max_turns=config.MAX_TURNS,                 # guard (§10)
        max_budget_usd=config.MAX_BUDGET_USD,       # guard (§10) — native cost ceiling
    )

    tool_calls: list[str] = []
    result = None

    async for message in query(prompt=ORCHESTRATOR_PROMPT, options=options):
        kind = type(message).__name__
        # Surface tool calls so we learn the real subagent-invocation tool name
        # and confirm WebSearch/WebFetch fire.
        for block in getattr(message, "content", []) or []:
            block_kind = type(block).__name__
            if block_kind in ("ToolUseBlock", "ServerToolUseBlock"):
                name = getattr(block, "name", "?")
                tool_calls.append(name)
                print(f"  [tool] {name}")
            elif block_kind == "TextBlock":
                text = (getattr(block, "text", "") or "").strip()
                if text:
                    print(f"  [{kind} text] {text[:200]}")
        if kind == "ResultMessage":
            result = message

    print("\n=== SPIKE SUMMARY ===")
    print("tools invoked:", tool_calls or "(none)")
    if result is not None:
        print("is_error:", getattr(result, "is_error", "?"))
        print("num_turns:", getattr(result, "num_turns", "?"))
        print("total_cost_usd:", getattr(result, "total_cost_usd", "?"))
        final = getattr(result, "result", None)
        if final:
            print("final result:", str(final)[:400])


if __name__ == "__main__":
    asyncio.run(main())
