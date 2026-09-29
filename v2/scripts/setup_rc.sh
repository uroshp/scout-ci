#!/usr/bin/env bash
#
# One-time setup of the RC environment (2026-09-28): the release-candidate viewer service and the
# Cloud Build trigger that deploys it from the `rc` branch.
#
# WHY: production (agent-scout.ai, built from main) is never the test surface. Every new screen is
# reviewed on the RC URL first (desktop + iPad), then promoted to main. The RC service runs the
# SAME image recipe (v2/cloudbuild.yaml with _SERVICE=agent-scout-rc), shows the same cards (the
# sync-rc workflow merges main into rc on every push), and is isolated from production data by
# SCOUT_SELFSERVE_DATA_PREFIX=rc (RC writes land under rc/ in the private data repo; reads fall
# through to production so pages look real). Analytics off, ribbon on, cookie gate on, robots
# Disallow, noindex. min-instances 0, so idle cost is ~$0.
#
# ── PREREQUISITES ───────────────────────────────────────────────────────────────────────────
#   1. gcloud authenticated on the project that hosts agent-scout (gcloud auth login).
#   2. The production service exists (its current image seeds the RC service's first revision).
#   3. Secrets scout-gh-token and scout-ga-mp-secret exist (created for production).
#
# ── RUN ─────────────────────────────────────────────────────────────────────────────────────
#   export GCP_PROJECT_ID=scout-monitor
#   export SCOUT_RC_PASSWORD='choose-a-password'      # the review gate; do NOT commit this
#   bash v2/scripts/setup_rc.sh
#
# Re-running is safe: the secret gets a new version, the service and trigger are updated in place.

set -euo pipefail

: "${GCP_PROJECT_ID:?set GCP_PROJECT_ID}"
: "${SCOUT_RC_PASSWORD:?set SCOUT_RC_PASSWORD (the RC review gate)}"
REGION="${GCP_REGION:-us-west1}"
PROD_SERVICE="${PROD_SERVICE:-agent-scout}"
RC_SERVICE="${RC_SERVICE:-agent-scout-rc}"
REPO_OWNER="${SCOUT_REPO_OWNER:-uroshp}"
REPO_NAME="${SCOUT_REPO_NAME:-scout-ci}"
TRIGGER="${RC_TRIGGER:-agent-scout-rc-deploy}"
DATA_REPO="${SELFSERVE_REPO:-uroshp/scout-user-data}"

gcloud config set project "$GCP_PROJECT_ID" >/dev/null

# 1. The RC password lives in Secret Manager, never in the plist / the repo / the trigger.
if gcloud secrets describe scout-rc-password >/dev/null 2>&1; then
  printf '%s' "$SCOUT_RC_PASSWORD" | gcloud secrets versions add scout-rc-password --data-file=-
  echo "  ✓ scout-rc-password: new version"
else
  printf '%s' "$SCOUT_RC_PASSWORD" | gcloud secrets create scout-rc-password --data-file=- --replication-policy=automatic
  echo "  ✓ scout-rc-password: created"
fi

# 2. The RC service, seeded from production's current image (the RC trigger replaces it on the
#    first push to rc). Same shape as docs/cloud-run-setup.md's first deploy, minus min-instances.
IMAGE=$(gcloud run services describe "$PROD_SERVICE" --region "$REGION" \
          --format='value(spec.template.spec.containers[0].image)')
echo "  seeding $RC_SERVICE from $IMAGE"
gcloud run deploy "$RC_SERVICE" --image "$IMAGE" --region "$REGION" --allow-unauthenticated \
  --min-instances 0 --max-instances 2 --memory 512Mi --cpu 1 --port 8080 --quiet \
  --set-env-vars "SCOUT_RC=1,SCOUT_ANALYTICS=0,SCOUT_SELFSERVE_DATA_PREFIX=rc,SCOUT_SELFSERVE_DATA_READ_FALLBACK=1,SCOUT_SELFSERVE_EMAIL=1,SELFSERVE_REPO=${DATA_REPO}" \
  --set-secrets "SELFSERVE_GH_TOKEN=scout-gh-token:latest,SCOUT_RC_PASSWORD=scout-rc-password:latest"
RC_URL=$(gcloud run services describe "$RC_SERVICE" --region "$REGION" --format='value(status.url)')
gcloud run services update "$RC_SERVICE" --region "$REGION" --quiet \
  --update-env-vars "SCOUT_SELFSERVE_APP_URL=${RC_URL}"
echo "  ✓ $RC_SERVICE at $RC_URL"

# 3. The RC Cloud Build trigger: a mirror of the production trigger (same repo connection, same
#    deploy service account, same include/ignore globs), on branch rc, with _SERVICE set.
#    Imported from YAML because `triggers create github` rejects the substitution + service-account
#    combination the production trigger uses (INVALID_ARGUMENT on first attempt, 2026-09-28).
DEPLOY_SA=$(gcloud builds triggers describe "${PROD_TRIGGER:-agent-scout-deploy}" --region=global \
              --format='value(serviceAccount)')
TMP=$(mktemp)
cat > "$TMP" <<YAML
name: ${TRIGGER}
description: RC viewer deploy (branch rc -> ${RC_SERVICE}); mirrors agent-scout-deploy
filename: v2/cloudbuild.yaml
github:
  name: ${REPO_NAME}
  owner: ${REPO_OWNER}
  push:
    branch: ^rc$
includedFiles:
- v2/**
ignoredFiles:
- v2/docs/**
substitutions:
  _SERVICE: ${RC_SERVICE}
serviceAccount: ${DEPLOY_SA}
YAML
gcloud builds triggers import --region=global --source="$TMP" >/dev/null
rm -f "$TMP"
echo "  ✓ trigger $TRIGGER imported (branch ^rc$, _SERVICE=${RC_SERVICE}, sa=${DEPLOY_SA##*/})"

echo
echo "RC is up: $RC_URL  (gate: the password you just set; ribbon + Disallow + analytics off)"
echo "Next: push to rc -> the trigger builds and deploys; review on the URL; promote via PR rc -> main."
