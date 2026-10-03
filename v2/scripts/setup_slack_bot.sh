#!/usr/bin/env bash
# Agent Scout in Slack: the bot's home on the Mac mini (2026-10-02). Idempotent.
#   ~/scout-slack/env      tokens + engine URL + key, mode 600 (you fill it in once)
#   ~/scout-slack/venv     slack_bolt, slack_sdk, httpx (NOT in the repo's requirements: the bot
#                          never ships in a Cloud Run image)
#   ~/scout-slack/run.sh   loads env, runs `python -m slackbot.app` from the checkout
#   launchd com.urosh.scout-slack  KeepAlive, log at ~/scout-slack/log/bot.log
# RUN:  bash v2/scripts/setup_slack_bot.sh            (from any checkout)
#       SCOUT_SLACK_REPO=~/code/scout-ci-rc/v2 bash v2/scripts/setup_slack_bot.sh   (review against rc)
set -euo pipefail
HOME_DIR="$HOME/scout-slack"
# default: the v2 folder this script lives in (so the rc worktree reviews against rc); SCOUT_SLACK_REPO overrides
REPO="${SCOUT_SLACK_REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
LABEL="com.urosh.scout-slack"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
mkdir -p "$HOME_DIR/log"
[ -d "$HOME_DIR/venv" ] || python3 -m venv "$HOME_DIR/venv"
"$HOME_DIR/venv/bin/pip" install -q -r "$REPO/slackbot/requirements.txt"
if [ ! -f "$HOME_DIR/env" ]; then
  cat > "$HOME_DIR/env" <<'ENV'
# Agent Scout in Slack. Fill in, then: launchctl kickstart -k gui/$(id -u)/com.urosh.scout-slack
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
SCOUT_ENGINE_URL=https://scout-engine-y5fw7otaqa-uw.a.run.app
ASK_SLACK_KEY=
SCOUT_SLACK_QUICK_PER_USER=10
SCOUT_SLACK_DEEP_PER_USER=1
ENV
  chmod 600 "$HOME_DIR/env"
  echo "  wrote $HOME_DIR/env (fill in the tokens)"
fi
cat > "$HOME_DIR/run.sh" <<RUN
#!/bin/zsh
set -a; source "$HOME_DIR/env"; set +a
export PYTHONUNBUFFERED=1
cd "\${SCOUT_SLACK_REPO:-$REPO}"
exec "$HOME_DIR/venv/bin/python" -m slackbot.app
RUN
chmod +x "$HOME_DIR/run.sh"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$HOME_DIR/run.sh</string></array>
  <key>EnvironmentVariables</key><dict><key>SCOUT_SLACK_REPO</key><string>$REPO</string></dict>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$HOME_DIR/log/bot.log</string>
  <key>StandardErrorPath</key><string>$HOME_DIR/log/bot.log</string>
</dict></plist>
PL
if grep -q "xoxb-\.\.\." "$HOME_DIR/env"; then
  echo "  tokens not set yet: launchd job written but NOT loaded ($PLIST)"
else
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  echo "  ✓ $LABEL loaded (repo: $REPO); log: $HOME_DIR/log/bot.log"
fi
