# supi-cloud-run

CPU-only, scale-to-zero Cloud Run fork of the [omniServerless](../omniServerless) OmniVoice TTS
service. That repo was **not modified** — this is an independent copy, adapted for a business-hours
traffic pattern (roughly 10am–6pm) where paying for an always-on GPU pod doesn't make sense.

Existing caller migrating from the RunPod GPU service? See **[MIGRATION.md](MIGRATION.md)** for
what changed in the API contract (short answer: almost nothing) and what's operationally
different (cold starts, voice-profile persistence).

What changed vs. the GPU/RunPod original:
- **CPU inference** — no CUDA, no `torch.compile` (it would slow every cold start down), CPU thread
  tuning, optional int8 dynamic quantization, optional lazy ASR loading.
- **One port per Cloud Run service** — no SSH, no dual-port container. The public TTS API
  (`app:app`) and the operator admin console (`console_app:console_app`) are the *same image*,
  deployed as **two separate Cloud Run services** by switching the `APP_MODULE` env var. This repo's
  CI only automates the public API; see "Deploying the admin console" below for the second service.
- **Idle-shutdown watchdog** — the process sends itself `SIGTERM` after `IDLE_TIMEOUT_SECONDS`
  (default 300 = 5 minutes) with no HTTP request, so the container exits deterministically rather
  than relying on Cloud Run's own (undocumented) idle-instance garbage collection. Combined with
  `--min-instances=0`, the service is only ever up while it's being used.
- **Artifact Registry image build** — GitHub Actions authenticates to GCP via Workload Identity
  Federation and pushes the Docker image directly to your project's Artifact Registry on every push
  to `main` — no GHCR, no proxy hop, Cloud Run pulls the same repo it was pushed to.

## Cost model

Cloud Run's default billing mode only charges for CPU/memory while a request is actively being
processed — an idle-but-warm instance costs nothing extra, and `--min-instances=0` lets it scale to
zero entirely between business hours. Free tier (per month, as of writing): 180,000 vCPU-seconds,
360,000 GiB-seconds, 2,000,000 requests. At 4 vCPU / 8 GiB that's roughly **12.5 hours of active
synthesis time free per month** — likely enough for daytime traffic once the CPU optimizations below
are tuned. Beyond that it's pay-per-use (~$0.42/vCPU-hour in Tier 1 regions), which is still far
below a 24/7 GPU pod that sits idle 16 hours a day.

## CPU performance tuning (do this before relying on it in production)

These are the levers, most impactful first. All are env vars — no code changes needed to try them.

| Env var | Default | What it does |
|---|---|---|
| `DEFAULT_NUM_STEP` | `32` | Flow-matching step count; inference cost is linear in this. Lower = faster + cheaper, with a quality tradeoff. Try `16` first. |
| `CPU_INT8` | `false` | Dynamic int8 quantization of Linear layers on load. Roughly halves CPU matmul cost; small quality tradeoff. |
| `LOAD_ASR` | `true` | ASR is only used to auto-transcribe reference audio when a voice-clone call omits `ref_text`. Set `false` to skip loading it if callers always pass `ref_text` — cuts cold-start time and memory. |
| `WARMUP_RUNS` | `1` | Number of real generations run at startup to warm lazy kernels. Each run adds to cold-start latency; `0` disables warmup entirely (fastest cold start, slowest first real request). |
| `CPU_LIMIT` | auto (`os.cpu_count()`) | Overrides the thread count `tts_core.py` sets `torch.set_num_threads()` to. Should generally match the Cloud Run service's `--cpu` value. |

**Before changing `DEFAULT_NUM_STEP` or enabling `CPU_INT8` in production**, validate the audio
quality by ear — both are quality/speed tradeoffs, not free wins. This app already has the tool for
it: `POST /admin/sweep` (enable with `ENABLE_SWEEP=true`, gated behind `ADMIN_API_KEY`) runs an A/B
grid over `num_step` (and other params) on real text so you can listen before locking in a value.

If CPU inference still isn't fast enough after these, the next step up is exporting the model to
ONNX Runtime or OpenVINO (k2-fsa publishes ONNX export tooling for their TTS models) — typically
2-4x faster than eager PyTorch on CPU. Not implemented here; a bigger lift.

## Persistence — what survives a cold start and what doesn't

Cloud Run's filesystem is ephemeral per instance. Two things default to being fine with that, one
needs a decision:

- **Tenants / API keys / credits**: default backend is `MemoryStore`, populated from the `API_KEYS`
  / `API_CREDITS` env vars on every cold start — since the source of truth is env vars, not mutable
  state, this survives cold starts with zero extra setup. Only set `TENANT_DB_PATH` (enabling the
  durable `SqliteStore` + runtime `/admin` tenant CRUD) if you need it, and if you do, point it at a
  mounted [Cloud Run GCS volume](https://cloud.google.com/run/docs/configuring/services/cloud-storage-volume-mounts)
  or the admin API's changes will vanish on the next cold start.
- **Voice-clone prompts (L2 disk cache)**: `VOICE_PROFILES_DIR` defaults to a path inside the
  container. This is just a cache — `ref_audio_url` is the source of truth, so a cache miss after a
  cold start just means the first clone of a given voice re-runs (slower, not broken).
- **Operator-curated "persistent" voice profiles** (warm-loaded at boot, see `voices.py`
  `list_persistent()`): these lose their warm-load benefit on every cold start unless
  `VOICE_PROFILE_DB_PATH` / `VOICE_PROFILES_DIR` also point at a mounted GCS volume.

For an MVP deploy, leave all of this at defaults (fully ephemeral) and add a GCS volume mount later
if you start using the admin console for live tenant/voice management.

## Repo layout

Same module boundaries as the GPU original: `tts_core.py` (synthesis logic), `voices.py` (voice
registry), `store.py`/`tenancy.py`/`credits.py` (multi-tenant auth + metering), `security.py`
(response hardening), `admin.py`/`console_app.py` (operator console), `app.py` (public API). No
`handler.py` — there is no RunPod job handler in this repo, HTTP is the only entry point.

## First-time setup

### 1. Create the GitHub repo and push

This repo has not been pushed anywhere yet. Create an empty repo on GitHub (no README/license —
this directory already has one), then:

```bash
cd ~/Programming/supi-cloud-run
git add -A
git commit -m "Initial CPU/Cloud Run fork of omniServerless"
git remote add origin https://github.com/<your-account-or-org>/supi-cloud-run.git
git branch -M main
git push -u origin main
```

Pushing to `main` triggers `.github/workflows/build-and-push.yml`, which builds the image and
pushes it directly to your Artifact Registry repo (see step 2) as `:latest` + `:sha-<commit-sha>`.

### 2. Cloud Run deploy setup (GCP side — do this once)

Both workflows authenticate via Workload Identity Federation (no long-lived JSON key) as the same
service account, `supi-deployer` — it both pushes the image (`build-and-push.yml`) and issues the
`gcloud run deploy` (`deploy-cloud-run.yml`). You'll need a GCP project with the Cloud Run, IAM
Credentials, and Artifact Registry APIs enabled, then:

```bash
PROJECT_ID=<your-gcp-project-id>
REGION=<e.g. us-central1>
POOL=github-pool
PROVIDER=github-provider
SA=supi-deployer
AR_REPO=supi-cloud-run

gcloud config set project "$PROJECT_ID"
gcloud services enable run.googleapis.com iamcredentials.googleapis.com artifactregistry.googleapis.com

# Workload Identity Pool + Provider trusting this specific GitHub repo
gcloud iam workload-identity-pools create "$POOL" --location=global
gcloud iam workload-identity-pools providers create-oidc "$PROVIDER" \
  --location=global --workload-identity-pool="$POOL" \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
  --attribute-condition="assertion.repository=='<your-account-or-org>/supi-cloud-run'"

# Deploy service account: Cloud Run admin + Artifact Registry writer (pushes AND deploys)
gcloud iam service-accounts create "$SA" --display-name="Supi Cloud Run deployer"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA}@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/run.admin"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA}@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/iam.serviceAccountUser"

# Let GitHub's OIDC token impersonate that service account
gcloud iam service-accounts add-iam-policy-binding \
  "${SA}@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/$(gcloud projects describe $PROJECT_ID --format='value(projectNumber)')/locations/global/workloadIdentityPools/${POOL}/attribute.repository/<your-account-or-org>/supi-cloud-run"

# Artifact Registry repo the image lives in (standard docker repo, not a remote-repository proxy —
# GitHub Actions pushes straight into it, Cloud Run pulls straight out of it)
gcloud artifacts repositories create "$AR_REPO" \
  --repository-format=docker \
  --location="$REGION" \
  --description="Supi Cloud Run TTS images"

gcloud artifacts repositories add-iam-policy-binding "$AR_REPO" \
  --location="$REGION" \
  --member="serviceAccount:${SA}@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/artifactregistry.writer"

# The Cloud Run service's *runtime* identity is a different principal than the deployer above —
# unless you set --service-account in deploy-cloud-run.yml, Cloud Run pulls the image at instance
# startup as the project's default compute service account, so it also needs read access:
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
gcloud artifacts repositories add-iam-policy-binding "$AR_REPO" \
  --location="$REGION" \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role="roles/artifactregistry.reader"

# Print the provider resource name for the GitHub secret below
gcloud iam workload-identity-pools providers describe "$PROVIDER" \
  --location=global --workload-identity-pool="$POOL" --format="value(name)"
```

Then in the GitHub repo, **Settings -> Secrets and variables -> Actions**:

Repo **variables** (not secret — plain config):
- `GCP_PROJECT_ID` = `<your-gcp-project-id>`
- `GCP_REGION` = e.g. `us-central1`
- `GCP_AR_REPO` = `supi-cloud-run` (optional, matches the `gcloud artifacts repositories create` name above — this is the default)
- `CLOUD_RUN_SERVICE_NAME` = `supi-tts` (optional, this is the default)
- `CLOUD_RUN_CPU` / `CLOUD_RUN_MEMORY` / `CLOUD_RUN_MAX_INSTANCES` (optional, tune to taste)

Repo **secrets**:
- `GCP_WORKLOAD_IDENTITY_PROVIDER` = the `name` printed by the last command above
- `GCP_DEPLOY_SERVICE_ACCOUNT` = `supi-deployer@<project-id>.iam.gserviceaccount.com`

Re-run the "Deploy to Cloud Run" workflow (or push again) once these are set.

### 3. Provisioning a tenant API key

The app itself refuses to serve `/tts` (and other protected routes) until at least one tenant
`API_KEY` exists (`REQUIRE_AUTH=true` by default — see `app.py`). Cloud Run env vars are plaintext
to anyone who can `gcloud run services describe` the service, so the key lives in **Secret
Manager** instead, referenced by name via `--set-secrets` (`deploy-cloud-run.yml`'s `secrets:`
input) rather than baked into the deploy spec:

```bash
PROJECT_ID=<your-gcp-project-id>
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')

# generate a strong key and store it — this is the literal value you'll send as X-API-Key
openssl rand -hex 32 | gcloud secrets create supi-api-key --data-file=- --replication-policy=automatic

# both the deployer SA (attaches the secret at deploy time) and the runtime compute SA
# (resolves it when the container starts) need read access
gcloud secrets add-iam-policy-binding supi-api-key \
  --member="serviceAccount:supi-deployer@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"
gcloud secrets add-iam-policy-binding supi-api-key \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"
```

Re-run "Deploy to Cloud Run" afterward — the next revision picks up `API_KEY` from the secret. To
see the key value again later (you won't be prompted for it again): `gcloud secrets versions
access latest --secret=supi-api-key`. For multiple tenants instead of one shared key, switch
`store.py`'s `API_KEYS` (JSON, still via a secret) instead of the legacy single `API_KEY` — same
`--set-secrets` mechanism, different env var name.

### 4. Calling the service

The Cloud Run layer is `--allow-unauthenticated` — no Google identity token needed, no `gcloud`
required on the caller's side. The app's own `X-API-Key` check (`tenancy.py`) is the actual gate:
constant-time key comparison, per-tenant rate limiting, and credit metering (`credits.py`). Any
system that can send an HTTPS request with a header can call it:

```bash
curl -X POST "$SERVICE_URL/tts" \
  -H "X-API-Key: <tenant-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"text": "..."}'
```

Because the endpoint is reachable by anyone who has (or guesses) the URL, the API key is the whole
perimeter — treat it like any other production credential (Secret Manager, not committed to the
repo; see step 3 above for how it's provisioned). If you outgrow a shared key, switch `API_KEYS`
(JSON, multi-tenant) for `API_KEY` (single tenant) in `deploy-cloud-run.yml`'s `secrets:` input.

### 5. Deploying the admin console (optional, not automated)

The same image also serves `console_app:console_app`. To deploy it as a second Cloud Run service:

```bash
gcloud run deploy supi-console \
  --image=<region>-docker.pkg.dev/<project-id>/supi-cloud-run/supi-cloud-run:<tag> \
  --region=<region> \
  --set-env-vars="APP_MODULE=console_app:console_app,ADMIN_API_KEY=<pick-a-strong-key>" \
  --min-instances=0 --max-instances=1 --concurrency=1 \
  --no-allow-unauthenticated
```

If it needs to call the public API's `/admin/sweep` endpoint, also set `TTS_INTERNAL_URL` to the
public service's Cloud Run URL (with appropriate auth — see step 4).

## Local testing

```bash
pip install -r requirements.txt
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu
python preload.py   # one-time: downloads + caches model weights locally
PORT=8080 python app.py
```

Or build and run the container exactly as Cloud Run will:

```bash
docker build -t supi-cloud-run .
docker run -p 8080:8080 -e PORT=8080 supi-cloud-run
```
