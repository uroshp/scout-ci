# Agent Scout in Slack (V1)

A Bolt for Python app that runs on the Mac mini over Socket Mode and answers through the Ask Scout
engine. It holds no model key: it calls `POST /ask` with its own key (`ASK_SLACK_KEY`), and the
engine records its traffic as `asked_by: slack` under a $3 daily cap inside the engine's ceiling.

## What it does
- DM it, or @mention it in a channel: it answers in the thread. Follow-ups in the same thread keep
  the scope (the engine's `history`).
- While it works, the message it posted is edited in place with the engine's stage lines.
- The answer carries a "Verified" line and buttons: Research deeper (quick answers only), Sources,
  What was cut, Open on agent-scout.ai.
- A new member gets a welcome DM with three suggested questions. In a workspace with the Agents &
  AI Apps feature, an assistant thread opens with the same three as suggested prompts.
- Per user per day: 10 quick answers, 1 deep. A Slack retry of the same event replays the finished
  answer instead of paying twice (the request token is a hash of team, channel and message ts).

## Setup (the owner's part, about 20 minutes)
1. Create the free workspace "Agent Scout Demo". At api.slack.com/apps choose *Create New App* ->
   *From a manifest*, paste `manifest.yml`, install it to the workspace.
2. Copy the bot token (`xoxb-…`) from *OAuth & Permissions* and an app-level token (`xapp-…`, scope
   `connections:write`) from *Basic Information -> App-Level Tokens*.
3. On the mini, put them in `~/scout-slack/env` (mode 600) with `SCOUT_ENGINE_URL` and
   `ASK_SLACK_KEY` (the same value set on the engine service), then load the launchd job
   (`v2/scripts/setup_slack_bot.sh`).
4. Create a shared invite link that never expires; set `SCOUT_SLACK_INVITE_URL`, `SCOUT_SLACK_APP_ID`
   and `SCOUT_SLACK_TEAM_ID` on the viewer service so the site shows the "Agent Scout in Slack"
   page with its live actions.

## Tests
`python -m unittest tests.test_slackbot` from `v2/` (render, engine client against a fake SSE
server, state, handlers against a fake Slack client). No test reaches Slack or the engine.
