# Scout over MCP (WS4, 2026-09-29)

Scout's cards and Ask as tools another agent can call. Reads are free; `ask_scout` spends money
and needs the owner's key over HTTP.

## Tools

| tool | what it returns |
|---|---|
| `list_battlecards()` | the roster: slug, the two companies, focus, last checked, claim count, URL |
| `get_battlecard(slug, section?, persona?)` | the card as markdown; `section` narrows to one of the eight sections; `persona` puts that audience's plays and objections first |
| `recent_changes(slug, days=7)` | material changes (old → new, why it matters, source, `triggered_by`) |
| `sources(slug)` | every citation with its kind (filing, company statement, news, …), tier, as-of date and the claims resting on it |
| `ask_scout(question, mode="quick"\|"deep", competitor?)` | an Ask Scout answer: paragraphs with cites, sources, the cut log, what could not be verified, cost, permalink |

## Local (stdio): the "add Scout to Claude Code" demo

```bash
cd scout-ci/v2
claude mcp add scout -- ./.venv/bin/python -m scout.mcp_server
```

Over stdio the caller is the local owner: `ask_scout` runs with the store creds in the
environment (`SELFSERVE_GH_TOKEN`, `SELFSERVE_REPO`, `ANTHROPIC_API_KEY`) and the same daily ledger
as the panel (`SCOUT_ASK_DAILY_CEILING_USD`).

## Remote (Streamable HTTP) on the engine

`https://<engine>/mcp` when the engine runs with `SCOUT_MCP=1` (stateless, JSON responses; no
session affinity needed on Cloud Run). Reads need no auth. `ask_scout` needs
`Authorization: Bearer <a key from ASK_API_KEYS>`; without it the tool returns an error and
nothing runs. Claude Code and the API take a static bearer; Claude.ai's connector UI needs OAuth
2.1, which is the upgrade path, not built.

```bash
curl -s https://<engine>/mcp -H 'Accept: application/json, text/event-stream' -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"list_battlecards","arguments":{}}}'
```

Tests: `tests/test_mcp.py` (tool shapes on the real roster, the ledger around a faked ask, the
HTTP gate). Verified 2026-09-29 on the RC engine (reads, refusal without a key) and over stdio
(a real quick `ask_scout`: 3 verified sentences, 62 s, $0.78).
