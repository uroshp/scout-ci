import asyncio
from claude_agent_sdk import query, ClaudeAgentOptions

async def main():
    try:
        async for message in query(
            prompt="Search the web for Anthropic's latest model release and tell me the date.",
            options=ClaudeAgentOptions(
                model="claude-opus-4-8",
                allowed_tools=["WebSearch", "WebFetch"],
            ),
        ):
            print("---- MESSAGE TYPE:", type(message).__name__)
            print(message)
    except Exception as e:
        print("CAUGHT:", repr(e))

asyncio.run(main())
