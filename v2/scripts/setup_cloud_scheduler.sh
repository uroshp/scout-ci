#!/usr/bin/env bash
#
# Reliable trigger for Scout monitoring: Google Cloud Scheduler -> GitHub workflow_dispatch.
#
# WHY: GitHub's built-in `schedule:` cron is best-effort and routinely runs late or is dropped
# (observed 2026-06-05: a 07:00 ET fire landed 09:46; the 13:00 ET fire never ran). Cloud
# Scheduler has an uptime SLA and fires on time, and it is TIMEZONE-AWARE — "America/New_York"
# auto-adjusts for EDT/EST, so DST stops being a manual edit. It POSTs workflow_dispatch to the
# GitHub API; the existing monitor.yml Action then runs immediately (on-demand, not the
# deprioritized schedule queue). Nothing about the engine or the anchored due-gate changes.
#
# ── PREREQUISITES (one-time, done by you in the console / browser) ──────────────────────────
#   1. A GCP project with billing enabled.  (Cloud Scheduler: 3 jobs free/month — this uses 2.)
#   2. gcloud installed + authenticated:        gcloud auth login
#   3. A GitHub fine-grained PAT scoped to THIS repo with **Actions: Read and write**
#      (separate from the self-serve contents token). Mint at:
#         github.com/settings/personal-access-tokens  ->  Repository access: scout-ci only
#         ->  Permissions: Actions = Read and write.  Set an expiry you'll remember to rotate.
#
# ── RUN ─────────────────────────────────────────────────────────────────────────────────────
#   export GCP_PROJECT_ID=your-project-id
#   export GCP_REGION=us-east1                 # any Cloud Scheduler region; us-east1 is fine
#   export GH_DISPATCH_PAT=github_pat_xxx      # the Actions:write PAT — do NOT commit this
#   bash v2/scripts/setup_cloud_scheduler.sh
#
# Re-running is safe: existing jobs are updated in place, not duplicated.

set -euo pipefail

REPO="${SCOUT_REPO:-uroshp/scout-ci}"
WORKFLOW="monitor.yml"
: "${GCP_PROJECT_ID:?set GCP_PROJECT_ID}"
: "${GCP_REGION:?set GCP_REGION (e.g. us-east1)}"
: "${GH_DISPATCH_PAT:?set GH_DISPATCH_PAT (GitHub PAT with Actions:Read+Write on $REPO)}"

URI="https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches"
HEADERS="Authorization=Bearer ${GH_DISPATCH_PAT},Accept=application/vnd.github+json,X-GitHub-Api-Version=2022-11-28,User-Agent=scout-cloud-scheduler,Content-Type=application/json"
BODY='{"ref":"main"}'

gcloud config set project "$GCP_PROJECT_ID"
gcloud services enable cloudscheduler.googleapis.com

# create_or_update NAME SCHEDULE  — schedule is cron in America/New_York (DST-aware)
create_or_update() {
  local name="$1" schedule="$2"
  local verb=create
  if gcloud scheduler jobs describe "$name" --location="$GCP_REGION" >/dev/null 2>&1; then
    verb=update
  fi
  gcloud scheduler jobs "$verb" http "$name" \
    --location="$GCP_REGION" \
    --schedule="$schedule" \
    --time-zone="America/New_York" \
    --uri="$URI" \
    --http-method=POST \
    --headers="$HEADERS" \
    --message-body="$BODY"
  echo "  ✓ ${verb}d $name  ($schedule America/New_York)"
}

echo "Wiring Cloud Scheduler -> ${URI}"
# ONE daily run (2026-06-28). The 1pm pass was dropped: across weeks of history the 2nd run mostly
# repeated the 1st and, being retrieval-variance, sometimes regressed it (see MONITOR_ANCHORS_UTC).
# DOW 1-6 = Mon–Sat: no rep follows a card on the weekend, and Monday's run scans since last_checked
# so nothing is missed. The engine's due-gate also skips Sunday (config.MONITOR_SKIP_WEEKDAYS), so
# this is belt-and-suspenders.
create_or_update scout-monitor-morning "0 7 * * 1-6"   # 7:00 AM ET, Mon–Sat — daily brief

# The old midday job is retired. Deleting is destructive, so we only WARN + print the command.
if gcloud scheduler jobs describe scout-monitor-midday --location="$GCP_REGION" >/dev/null 2>&1; then
  echo "  ⚠ scout-monitor-midday still exists (retired). Delete it once with:"
  echo "      gcloud scheduler jobs delete scout-monitor-midday --location=$GCP_REGION"
fi

echo
echo "Done. Jobs:"
gcloud scheduler jobs list --location="$GCP_REGION" --filter="name~scout-monitor"
echo
echo "Test a fire now (does NOT wait for the schedule):"
echo "  gcloud scheduler jobs run scout-monitor-morning --location=$GCP_REGION"
echo "Then watch: gh run list --workflow=$WORKFLOW --limit 3"
