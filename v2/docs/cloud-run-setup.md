# Cloud Run setup — Agent Scout viewer (Flask)

Deploys `v2/server.py` (the Flask viewer) to Cloud Run, **in parallel** with the live Streamlit
app. Nothing here touches the Streamlit deployment until you map the domain (step 5).

Files involved: `server.py`, `Dockerfile`, `requirements-server.txt`, `.dockerignore` /
`.gcloudignore`. It reuses `scout/`, `battlecards/`, `assets/`, and `docs/mockups/` (the last is
read at runtime, so it ships in the image).

Use the **existing** GCP project (the one you may rename to drop "monitor"). Region below is
`us-west1` (Oregon — closest always-on US-West); `us-west2` (LA) is equally fine.

---

## 0. One-time: gcloud + project  (you run these — interactive)
Install the SDK if needed (https://cloud.google.com/sdk/docs/install). In the Claude Code prompt,
prefix with `!` so the output lands in this session:
```
! gcloud auth login
! gcloud config set project YOUR_PROJECT_ID
```

## 1. Enable the APIs (once)
```
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com
```

## 2. Secrets  (you run these — paste real values)
The viewer renders cards with **no** secrets; secrets only enable *Create-your-own* (the GitHub
data repo + dispatch) and the server-side GA event.

**⚠ TOKEN PERMISSION CHECKLIST — verify BEFORE deploying** (added 2026-07-18 after the dispatch
outage: the fine-grained PAT lacked Actions:write, every visitor dispatch 403'd silently for
weeks, and this doc had no verification step). The fine-grained PAT in `scout-gh-token` needs
BOTH, explicitly granted per repo:
1. `SELFSERVE_REPO` (the private data repo): **Contents: Read and write**
2. `uroshp/scout-ci` (the code repo): **Actions: Read and write** — the app POSTs a
   `workflow_dispatch` to start generation; without this the request queues forever and the
   visitor sees an endless "Generating…" page.

Verify with the side-effect-free probe (an invalid ref can never trigger a run):
```
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  -H "Authorization: Bearer $PAT" -H "Accept: application/vnd.github+json" \
  https://api.github.com/repos/uroshp/scout-ci/actions/workflows/selfserve.yml/dispatches \
  -d '{"ref":"probe-nonexistent-branch"}'
# 422 ("No ref found")  = permission OK, ship it.
# 403 ("Resource not accessible by personal access token") = Actions:write MISSING, fix the PAT first.
```
The daily canary on the mini (`~/scout-tools/scout-canary`) runs this probe every morning and
emails on 403, so a permission regression can never live longer than a day again.
```
! printf '%s' 'YOUR_GITHUB_PAT'   | gcloud secrets create scout-gh-token   --data-file=-
! printf '%s' 'YOUR_GA_MP_SECRET' | gcloud secrets create scout-ga-mp-secret --data-file=-
# let the Cloud Run runtime service account read them:
PROJ=$(gcloud config get-value project)
SA="$(gcloud projects describe $PROJ --format='value(projectNumber)')-compute@developer.gserviceaccount.com"
gcloud secrets add-iam-policy-binding scout-gh-token    --member="serviceAccount:$SA" --role=roles/secretmanager.secretAccessor
gcloud secrets add-iam-policy-binding scout-ga-mp-secret --member="serviceAccount:$SA" --role=roles/secretmanager.secretAccessor
```
(To rotate later: `gcloud secrets versions add scout-gh-token --data-file=-`.)

## 3. First deploy — gives a `*.run.app` URL (Streamlit stays untouched)
```
cd v2
gcloud run deploy agent-scout \
  --source . \
  --region us-west1 \
  --allow-unauthenticated \
  --min-instances 1 --max-instances 4 \
  --memory 512Mi --cpu 1 --port 8080 \
  --set-env-vars SCOUT_SELFSERVE_APP_URL=https://agent-scout.ai,SCOUT_SELFSERVE_EMAIL=1,SELFSERVE_REPO=uroshp/scout-user-data \
  --set-secrets SELFSERVE_GH_TOKEN=scout-gh-token:latest,GA_MP_API_SECRET=scout-ga-mp-secret:latest
```
- `--min-instances 1` = always warm (your decision; no cold starts).
- `GA_MEASUREMENT_ID` defaults to `G-MR1Z8NB7BP` in code — override via `--set-env-vars` only if it changes.
- The GA tag **self-gates** to fire only when the page host == `SCOUT_SELFSERVE_APP_URL`'s host. So
  on the temporary `*.run.app` URL GA stays **off** (correct — we don't want test traffic in GA);
  it switches on once the domain is mapped and the host matches.

## 4. Verify on the `*.run.app` URL — the gate (no live risk)
Open every card, the create form, a print sheet. Check the **sticky rail**, the **feed collapse**,
`$` figures, deep-link anchors, mobile width. Ask me to run a **card-by-card HTML diff** against the
live Streamlit cards before you commit to the domain.

## 5. Map the domain (only after verifying)
```
gcloud run domain-mappings create --service agent-scout --domain agent-scout.ai --region us-west1
```
Add the DNS records it prints at your registrar; TLS auto-provisions (~15–60 min). `agent-scout.ai`
already equals `SCOUT_SELFSERVE_APP_URL`, so GA + result links light up automatically.

Legacy links: leave `agent-scout.streamlit.app` running as the rollback, then later reduce it to a
one-line redirect to `agent-scout.ai`.

---

## 6. How to let me (Claude) publish updates directly

The deploy is a single command (`gcloud run deploy --source .`). Two ways to let me run it:

**Option A — gcloud authenticated on the machine where I run (recommended for the migration).**
You authenticate once on that box (the Mac mini):
```
! gcloud auth login
! gcloud config set project YOUR_PROJECT_ID
```
Your account needs: **Cloud Run Admin**, **Service Account User**, **Cloud Build Editor**,
**Artifact Registry Writer** (or hand me a dedicated deploy service account). After that, when we've
verified a change I run the `gcloud run deploy` from Bash and it builds + publishes — you say "go," I
deploy, you watch the output. This keeps your verify-before-live gate.

**Option B — push-to-deploy (hands-off CD), for later.**
Connect the repo to Cloud Build with a trigger on a **dedicated branch** (not `main`, so engine/docs
commits don't redeploy the viewer):
```
gcloud builds triggers create github \
  --repo-name=scout-ci --repo-owner=uroshp \
  --branch-pattern='^deploy$' --build-config=v2/cloudbuild.yaml
```
Then `git push origin deploy` auto-builds + deploys — I need no GCP creds, just git. Trade-off: every
push to that branch goes live, skipping the manual gate. Good once the app is stable.

**Recommendation:** Option A during the migration (control + verification), add Option B later for
convenience.
