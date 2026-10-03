# Agent Scout in Slack

V1 (2026-10-02): a free demo workspace where Agent Scout answers like Claude in Slack. The site's
"Agent Scout in Slack" item opens `/slack`, which shows only the actions that are live: join the
demo workspace, open Agent Scout. V2 ("Add to your Slack", a public install) follows V1.

## Architecture

```
agent-scout.ai ──item──▶ /slack ──invite link──▶ Slack "Agent Scout Demo" (free workspace)
                                                        │ events over Socket Mode (outbound websocket)
                                                        ▼
                      Mac mini: v2/slackbot (Bolt for Python, launchd com.urosh.scout-slack)
                                                        │ HTTPS, bearer ASK_SLACK_KEY
                                                        ▼
                      scout-engine  POST /ask (SSE)  ──▶ done frame carries `answer` (structured)
```

- The bot holds no model key and renders its own surface from the engine's structured answer.
- The engine tells Slack traffic apart (`asked_by: slack`) and caps it at $3 a day
  (`SCOUT_ASK_SLACK_DAILY_USD`) inside its own daily ceiling.
- Per Slack user per day: 10 quick answers, 1 deep (`~/scout-slack/env`).
- A Slack retry of the same event replays the finished answer: the request token is a hash of
  team, channel and message ts, and the engine maps it to the answer id.
- The canary (`~/scout-tools/scout-canary`) fails with "Scout could not run: slack bot" when the
  launchd job is down or the bot token stops authenticating.

## Free vs paid workspaces

Slack's Agents & AI Apps features (assistant pane, suggested prompts, status, streamed text) need
a paid workspace or a Developer Program sandbox. The demo workspace is free, so there the bot is a
classic bot: DMs and @mentions, answers in the thread, progress by editing its message in place,
Block Kit buttons. The same code sets suggested prompts when an assistant thread opens, so a paid
workspace (V2) gets the pane without a code change.

## Setup
See `slackbot/README.md` (the owner's 20 minutes) and `scripts/setup_slack_bot.sh` (the mini).
The engine side is `scripts/setup_engine_service.sh`, which mints `scout-ask-slack-key` once.

## Review path
RC first: the bot runs against `scout-engine-rc` (`SCOUT_ENGINE_URL` in the env file) and the
`/slack` page is reviewed on the RC URL; then the env points at the production engine, the item
goes live on agent-scout.ai, and the first production answer is observed end to end.
