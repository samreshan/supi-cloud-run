# supi-cloud-run

CPU-only, scale-to-zero Cloud Run fork of the [omniServerless](../omniServerless) OmniVoice TTS
service. That repo was **not modified** — this is an independent copy, adapted for a business-hours
traffic pattern (roughly 10am–6pm) where paying for an always-on GPU pod doesn't make sense.

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
- **GHCR image build** — GitHub Actions builds the Docker image and pushes it to
  `ghcr.io/<owner>/supi-cloud-run` on every push to `main`.

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
pushes `ghcr.io/<owner>/supi-cloud-run:latest` + `:<commit-sha>`.

### 2. GHCR package visibility

By default a package's visibility follows the repo's (private repo -> private package). Cloud Run
can pull a **public** GHCR image with zero extra config; pulling a **private** one needs either an
Artifact Registry remote-repository proxy or an image-pull secret — more setup. This repo's image
has no secrets baked in (all credentials are runtime env vars, not build-time), only code and model
weights, so making the package public is a reasonable choice for a personal/small-team project — but
it's your call. To do it manually: GitHub -> your profile/org -> Packages -> `supi-cloud-run` ->
Package settings -> Change visibility.

### 3. Cloud Run deploy setup (GCP side — do this once)

`.github/workflows/deploy-cloud-run.yml` deploys via Workload Identity Federation (no long-lived
JSON key). You'll need a GCP project with Cloud Run + IAM Credentials APIs enabled, then:

```bash
PROJECT_ID=<your-gcp-project-id>
REGION=<e.g. us-central1>
POOL=github-pool
PROVIDER=github-provider
SA=supi-deployer

gcloud config set project "$PROJECT_ID"
gcloud services enable run.googleapis.com iamcredentials.googleapis.com

# Workload Identity Pool + Provider trusting this specific GitHub repo
gcloud iam workload-identity-pools create "$POOL" --location=global
gcloud iam workload-identity-pools providers create-oidc "$PROVIDER" \
  --location=global --workload-identity-pool="$POOL" \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
  --attribute-condition="assertion.repository=='<your-account-or-org>/supi-cloud-run'"

# Deploy service account, scoped to Cloud Run admin only
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

# Print the provider resource name for the GitHub secret below
gcloud iam workload-identity-pools providers describe "$PROVIDER" \
  --location=global --workload-identity-pool="$POOL" --format="value(name)"
```

**Cloud Run cannot pull an image from `ghcr.io` directly** — it only accepts Artifact Registry,
`gcr.io`, or Docker Hub sources. The fix is an Artifact Registry **remote repository**, which acts
as a caching proxy in front of GHCR; `deploy-cloud-run.yml` already targets the resulting path. This
requires the GHCR package to be **public** (see step 2 above) — an unauthenticated remote repository
can't reach a private upstream without extra Secret Manager / PAT setup not covered here. One more
one-time command:

```bash
gcloud artifacts repositories create ghcr \
  --repository-format=docker \
  --location="$REGION" \
  --mode=remote-repository \
  --remote-repo-config-desc="Proxy for ghcr.io" \
  --remote-docker-repo=https://ghcr.io
```

Then in the GitHub repo, **Settings -> Secrets and variables -> Actions**:

Repo **variables** (not secret — plain config):
- `GCP_PROJECT_ID` = `<your-gcp-project-id>`
- `GCP_REGION` = e.g. `us-central1`
- `GCP_AR_GHCR_REPO` = `ghcr` (optional, matches the `gcloud artifacts repositories create` name above — this is the default)
- `CLOUD_RUN_SERVICE_NAME` = `supi-tts` (optional, this is the default)
- `CLOUD_RUN_CPU` / `CLOUD_RUN_MEMORY` / `CLOUD_RUN_MAX_INSTANCES` (optional, tune to taste)

Repo **secrets**:
- `GCP_WORKLOAD_IDENTITY_PROVIDER` = the `name` printed by the last command above
- `GCP_DEPLOY_SERVICE_ACCOUNT` = `supi-deployer@<project-id>.iam.gserviceaccount.com`

Re-run the "Deploy to Cloud Run" workflow (or push again) once these are set.

### 4. Calling an IAM-authenticated service

Per your choice, the deployed service requires a Google identity token — it is **not** open to the
public internet, on top of the app's own `X-API-Key` check. Grant a caller access:

```bash
gcloud run services add-iam-policy-binding supi-tts \
  --region=<region> \
  --member="serviceAccount:<caller-sa>@<project>.iam.gserviceaccount.com" \
  --role="roles/run.invoker"
```

A caller then fetches an identity token scoped to the service URL and sends it as a Bearer token
alongside the existing `X-API-Key` header:

```bash
TOKEN=$(gcloud auth print-identity-token --audiences="$SERVICE_URL")
curl -X POST "$SERVICE_URL/tts" \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-API-Key: <tenant-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"text": "..."}'
```

If your caller can't easily mint Google identity tokens (e.g. a third-party telephony webhook), the
alternative is redeploying with `--allow-unauthenticated` — the app's own `X-API-Key` auth still
gates actual generation either way. That's a deliberate tradeoff to revisit if it becomes a blocker.

### 5. Deploying the admin console (optional, not automated)

The same image also serves `console_app:console_app`. To deploy it as a second Cloud Run service:

```bash
gcloud run deploy supi-console \
  --image=ghcr.io/<owner>/supi-cloud-run:<tag> \
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
