#!/usr/bin/env bash
#
# One-time setup of the Ask Scout engine service (WS2 step 4b, 2026-09-28): secrets, the first
# build + deploy, and pointing a viewer at it. Idempotent.
#
# WHY a second service: the viewer holds no model key by design. The engine is the one place with
# ANTHROPIC_API_KEY; it answers POST /ask over SSE, authenticates callers with an owner key or a
# viewer-minted page token (shared ASK_VIEWER_SECRET), and refuses to start a question that could
# cross the daily ceiling (scout/ledger.py, private store). One worker, --concurrency 1, 600 s.
#
# ── RUN ─────────────────────────────────────────────────────────────────────────────────────
#   export GCP_PROJECT_ID=scout-monitor
#   export ENGINE_SERVICE=scout-engine-rc VIEWER_SERVICE=agent-scout-rc DATA_PREFIX=rc   # or the prod pair
#   export ANTHROPIC_API_KEY=sk-ant-...                 # written to Secret Manager, never to the plist/repo
#   # Optional, and only to CHANGE a setting: ASK_CANNED, MCP_ENABLED, CALL_CAPTURE,
#   # ASK_DAILY_CEILING_USD. Left unset, a redeploy keeps what the live service already has.
#   bash v2/scripts/setup_engine_service.sh
#
# The viewer secret and the owner key are generated on first run and kept in Secret Manager
# (scout-ask-viewer-secret, scout-ask-api-keys); print the owner key with
#   gcloud secrets versions access latest --secret scout-ask-api-keys

set -euo pipefail
: "${GCP_PROJECT_ID:?set GCP_PROJECT_ID}"
: "${ANTHROPIC_API_KEY:?set ANTHROPIC_API_KEY}"
REGION="${GCP_REGION:-us-west1}"
ENGINE_SERVICE="${ENGINE_SERVICE:-scout-engine-rc}"
VIEWER_SERVICE="${VIEWER_SERVICE:-agent-scout-rc}"
DATA_PREFIX="${DATA_PREFIX-rc}"     # unset -> rc; EMPTY means production (":-" would turn "" into rc)
DATA_REPO="${SELFSERVE_REPO:-uroshp/scout-user-data}"
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

gcloud config set project "$GCP_PROJECT_ID" >/dev/null

# A redeploy must never change a setting nobody asked to change (2026-10-02: a production redeploy
# without the three knobs exported switched MCP off, cleared the probe's canned answer and stopped
# call capture). Each knob: the caller's value if exported, otherwise what the live service has now,
# otherwise the first-deploy default. The resolved values are printed before and after the deploy.
live_env() {     # live_env NAME -> the plain value on the running engine service, empty if none
  gcloud run services describe "$ENGINE_SERVICE" --region "$REGION" --format=json 2>/dev/null \
    | python3 -c 'import json,sys
try:
    env = json.load(sys.stdin)["spec"]["template"]["spec"]["containers"][0].get("env", [])
except Exception:
    env = []
print(next((e.get("value", "") for e in env if e["name"] == sys.argv[1]), ""))' "$1"
}
keep() {         # keep CALLER_VAR SERVICE_ENV DEFAULT
  local caller="$1" live
  if [ -n "${!caller+x}" ]; then printf '%s' "${!caller}"; return; fi
  live="$(live_env "$2")"
  printf '%s' "${live:-$3}"
}
CEILING="$(keep ASK_DAILY_CEILING_USD SCOUT_ASK_DAILY_CEILING_USD 10)"
CALL_CAPTURE="$(keep CALL_CAPTURE SCOUT_CALL_CAPTURE 0)"
ASK_CANNED="$(keep ASK_CANNED SCOUT_ASK_CANNED '')"
MCP_ENABLED="$(keep MCP_ENABLED SCOUT_MCP 0)"
echo "  settings for $ENGINE_SERVICE: ceiling=\$$CEILING call_capture=$CALL_CAPTURE mcp=$MCP_ENABLED canned=${ASK_CANNED:-none} prefix='${DATA_PREFIX}'"

secret_set() {   # secret_set NAME VALUE  (new version if it exists)
  if gcloud secrets describe "$1" >/dev/null 2>&1; then
    printf '%s' "$2" | gcloud secrets versions add "$1" --data-file=- >/dev/null; echo "  ✓ $1: new version"
  else
    printf '%s' "$2" | gcloud secrets create "$1" --data-file=- --replication-policy=automatic >/dev/null; echo "  ✓ $1: created"
  fi
}
secret_ensure() {   # secret_ensure NAME  (generate once, keep thereafter)
  if ! gcloud secrets describe "$1" >/dev/null 2>&1; then
    secret_set "$1" "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  else
    echo "  · $1: kept"
  fi
}

# 1. secrets
secret_set scout-anthropic-key "$ANTHROPIC_API_KEY"
secret_ensure scout-ask-viewer-secret
secret_ensure scout-ask-api-keys
secret_ensure scout-ask-slack-key      # Agent Scout in Slack (2026-10-02): the bot's own caller key

# 2. build + deploy the image (Cloud Build, the engine's cloudbuild file)
SHA=$(git -C "$REPO_ROOT" rev-parse --short HEAD)
IMAGE="us-west1-docker.pkg.dev/${GCP_PROJECT_ID}/cloud-run-source-deploy/${ENGINE_SERVICE}:${SHA}"
echo "  building ${IMAGE} ..."
gcloud builds submit "$REPO_ROOT" --config "$REPO_ROOT/v2/cloudbuild-engine.yaml" \
  --substitutions "_SERVICE=${ENGINE_SERVICE},_IMAGE=${IMAGE}" --quiet >/dev/null

# 3. the service config (the build's deploy step only swapped the image; this sets the rest)
VIEWER_URL=$(gcloud run services describe "$VIEWER_SERVICE" --region "$REGION" --format='value(status.url)')
# Cloud Run serves every service at two hostnames: the hashed one (`status.url`) and the
# deterministic `<service>-<project number>.<region>.run.app`. The browser's Origin is whichever
# the reader typed, so both go on the allowlist.
PROJECT_NUMBER=$(gcloud run services describe "$VIEWER_SERVICE" --region "$REGION" --format='value(metadata.namespace)')   # = the project number; `projects describe` needs an API the deploy account lacks
VIEWER_URL2="https://${VIEWER_SERVICE}-${PROJECT_NUMBER}.${REGION}.run.app"
ORIGINS="$VIEWER_URL"; [ "$VIEWER_URL2" != "$VIEWER_URL" ] && ORIGINS="$ORIGINS,$VIEWER_URL2"
[ "$DATA_PREFIX" = "" ] && ORIGINS="$ORIGINS,https://agent-scout.ai,https://www.agent-scout.ai"
gcloud run deploy "$ENGINE_SERVICE" --image "$IMAGE" --region "$REGION" --allow-unauthenticated --quiet \
  --min-instances 0 --max-instances 2 --memory 1Gi --cpu 1 --concurrency 1 --timeout 600 --port 8081 \
  --set-env-vars "^|^SCOUT_SELFSERVE_DATA_PREFIX=${DATA_PREFIX}|SCOUT_SELFSERVE_DATA_READ_FALLBACK=1|SELFSERVE_REPO=${DATA_REPO}|SCOUT_ASK_DAILY_CEILING_USD=${CEILING}|SCOUT_CALL_CAPTURE=${CALL_CAPTURE}|SCOUT_ASK_CANNED=${ASK_CANNED}|SCOUT_MCP=${MCP_ENABLED}|ASK_ALLOWED_ORIGINS=${ORIGINS}" \
  --set-secrets "ANTHROPIC_API_KEY=scout-anthropic-key:latest,ASK_VIEWER_SECRET=scout-ask-viewer-secret:latest,ASK_API_KEYS=scout-ask-api-keys:latest,ASK_SLACK_KEY=scout-ask-slack-key:latest,SELFSERVE_GH_TOKEN=scout-gh-token:latest" >/dev/null
ENGINE_URL=$(gcloud run services describe "$ENGINE_SERVICE" --region "$REGION" --format='value(status.url)')
echo "  ✓ $ENGINE_SERVICE at $ENGINE_URL (origins: $ORIGINS)"

# 4. point the viewer at it (engine mode replaces the canned replay)
gcloud run services update "$VIEWER_SERVICE" --region "$REGION" --quiet \
  --update-env-vars "SCOUT_ASK=1,SCOUT_ASK_ENGINE_URL=${ENGINE_URL}" --remove-env-vars SCOUT_ASK_CANNED \
  --update-secrets "ASK_VIEWER_SECRET=scout-ask-viewer-secret:latest" >/dev/null 2>&1 || \
gcloud run services update "$VIEWER_SERVICE" --region "$REGION" --quiet \
  --update-env-vars "SCOUT_ASK=1,SCOUT_ASK_ENGINE_URL=${ENGINE_URL}" \
  --update-secrets "ASK_VIEWER_SECRET=scout-ask-viewer-secret:latest" >/dev/null
echo "  ✓ $VIEWER_SERVICE -> engine mode"
echo
echo "healthcheck: $(curl -s -m 20 "$ENGINE_URL/healthcheck")"
echo "now live:    ceiling=\$$(live_env SCOUT_ASK_DAILY_CEILING_USD) call_capture=$(live_env SCOUT_CALL_CAPTURE) mcp=$(live_env SCOUT_MCP) canned=$(live_env SCOUT_ASK_CANNED)"
echo "owner key:   gcloud secrets versions access latest --secret scout-ask-api-keys"
echo "slack key:   gcloud secrets versions access latest --secret scout-ask-slack-key   (goes in ~/scout-slack/env on the mini)"
