# Migration guide: RunPod/GPU → Cloud Run

Audience: existing devs who currently call the Supi v1 TTS service running on the RunPod GPU pod
(`omniServerless`), migrating to the CPU/Cloud Run service in this repo (`supi-cloud-run`).

## TL;DR

You're already calling the FastAPI HTTP interface directly (not RunPod's job-queue API), so this
is mostly **a base-URL swap**, not a client rewrite. Same request body, same `X-API-Key` header,
same response JSON. The real differences are operational: CPU inference is slower than GPU,
cold starts are a real and frequent cost now, and voice-profile persistence needs a decision.

| | RunPod (old) | Cloud Run (new) |
|---|---|---|
| Base URL | `https://<pod>-8000.proxy.runpod.net` | `https://supi-tts-887510744886.asia-south1.run.app` |
| Request body | `TTSRequest` JSON, direct POST | Same — unchanged |
| Auth | `X-API-Key` header | Same header, **new key values** (§ Auth) |
| Response | `{"status","audio_base64","sample_rate","format",...}` | Same — unchanged |
| Inference | GPU | CPU (slower, tunable — § Why it's slower) |
| Idle behavior | Worker pool (RunPod-managed) | Scales to zero after 5 min idle |
| Voice profile persistence | Optional RunPod network volume | Optional GCS volume mount — **not configured yet** |
| `output_format` default | `telephony` (8kHz µ-law) | Same — unchanged |

## Why this migration

RunPod GPU credits are running low, and actual traffic is business-hours only (~10am–6pm).
Paying for an always-on GPU pod that sits idle 16+ hours a day doesn't make sense for that
pattern. Cloud Run's request-based billing plus scale-to-zero fits it much better — the service
only costs money while it's actually generating audio.

## Endpoint change

- Old: `https://<pod>-8000.proxy.runpod.net/tts`
- New: `https://supi-tts-887510744886.asia-south1.run.app/tts`

Request body (`TTSRequest`), the `X-API-Key` header, and the response JSON shape are all
**unchanged** — same field names in both repos:

`action`, `text`, `ref_audio_url`, `ref_text`, `voice_profile_id`, `template`, `data`,
`num_step`/`numstep`, `speed`, `quality`, `guidance_scale`, `temperature`,
`position_temperature`, `class_temperature`, `instruct`, `seed`, `output_format`.

Response for `action: "generate"`:
```json
{"status": "success", "audio_base64": "...", "sample_rate": 8000, "format": "8kHz_ulaw",
 "credits_remaining": 12.5, "warning": null}
```
Response for `action: "bulk_generate"`:
```json
{"status": "success", "results": [{"...item fields", "text_generated": "...", "audio_base64": "..."}], "format": "8kHz_ulaw"}
```

No client-side response parsing changes needed.

One platform-level difference worth knowing: Cloud Run has its own IAM auth layer, separate from
the app's `X-API-Key` check. This deployment has it disabled (`--allow-unauthenticated`) so
behavior matches today — `X-API-Key` is the only gate, same as the RunPod pod.

## Auth — what actually needs to change

The *mechanism* is identical: `X-API-Key` header, SHA-256 digest lookup against configured
tenants (`tenancy.py`). What's **not** automatically carried over is the *key values*. This
Cloud Run deployment currently has one single legacy `API_KEY` provisioned via Google Secret
Manager — not necessarily the full multi-tenant `API_KEYS` map that may be configured in the
RunPod pod's environment today.

**Action item**: pull the RunPod deployment's `API_KEYS` env var (JSON, or confirm it's actually
single-tenant), and provision the equivalent secret for Cloud Run so existing tenant keys keep
working unchanged after cutover. The mechanism for adding a multi-tenant key set is the same
`--set-secrets` pattern already used for the single key — see `README.md` "Provisioning a tenant
API key."

## Why it's slower — and the levers to fix it

Three real, distinct causes:

1. **CPU vs GPU inference.** Inherently slower per request — an expected tradeoff of dropping
   the GPU pod. Tunable via `DEFAULT_NUM_STEP` (default `32`, flow-matching step count, cost is
   linear in this) and `CPU_INT8` (dynamic int8 quantization of Linear layers, roughly halves
   matmul cost with a small quality tradeoff). Both are env vars — no code changes to try them,
   and `POST /admin/sweep` exists specifically for A/B-testing the tradeoff by ear before locking
   in a value.

2. **Cold starts.** `--min-instances=0` plus an in-app watchdog (`IDLE_TIMEOUT_SECONDS=300`)
   means the container fully exits 5 minutes after the last request. The next request pays a
   full cold start — model load plus warmup runs — measured at **~70-90 seconds** in this
   session's own end-to-end test. RunPod's worker pool likely stayed warmer between requests
   than Cloud Run's scale-to-zero does by design. This is the direct, expected cost of the
   scale-to-zero architecture that was deliberately chosen to control spend during idle hours —
   raising `IDLE_TIMEOUT_SECONDS` trades some of that savings back for fewer cold starts.

3. **No persistent voice-profile volume mounted yet.** Compounds cold-start cost specifically
   for voice cloning — see next section.

## Voice profile persistence — what changed, and a decision to make

| | RunPod (old) | Cloud Run (new) |
|---|---|---|
| L2 disk cache | Local container disk, ephemeral across worker restarts | Local container disk, ephemeral across cold starts — happens **far more often** under scale-to-zero |
| Durable volume | Optional `VOICE_PROFILE_PERSIST_DIR` on a RunPod network volume | Optional [GCS Volume Mount](https://cloud.google.com/run/docs/configuring/services/cloud-storage-volume-mounts) at the same env var — **not configured in this deploy** |
| "Persistent" flag (`voices.py`) | Copies flagged voices to the network volume, warm-loaded at startup | Same code path, but nothing survives a cold start without a mounted volume |

**Action item**: confirm whether the RunPod deployment actually had `VOICE_PROFILE_PERSIST_DIR`
pointed at a network volume in production.

- If yes: this is a real regression today. Every cold start currently re-clones voices from
  `ref_audio_url` from scratch instead of hitting a warm cache. Provision an equivalent GCS
  volume mount on Cloud Run before or shortly after cutover to reach parity.
- If RunPod was also running fully ephemeral (no network volume configured): no regression,
  behavior is equivalent, just slower per the cold-start frequency difference above.

A cache miss is always *safe* either way — `ref_audio_url` is the source of truth, so a miss just
means the clone recomputes (slower, not broken). This only affects latency, not correctness.

## Voice cloning — what's the same

Mechanism is unchanged: send `ref_audio_url` (+ optional `ref_text`, `voice_profile_id`). Lookup
order is L1 in-memory LRU cache (20 entries) → L2 disk cache → SSRF-guarded download of
`ref_audio_url` + clone-prompt generation on a full miss. Nothing about the request shape or
cache-key logic changed — only the *frequency* of cache misses, per the persistence section
above.

## Bulk generation — what's the same, what to watch

Request/response shape is unchanged: `template` (a Python `.format()` string) + `data` (list of
dicts, up to 100 items), batched `BULK_BATCH_SIZE` items at a time, one `audio_base64` result per
row.

New watch-item: batches now run on CPU instead of GPU — large `data` arrays will take
proportionally much longer. Cloud Run's deploy timeout on this service is 300 seconds.
Sanity-check your largest real bulk payload's CPU runtime against that ceiling; chunk large bulk
calls client-side if needed.

## Output format / 8kHz standard

No change, and no code change was needed here: `output_format` already defaults to `telephony`
(8kHz µ-law WAV) in both the RunPod and Cloud Run versions when the caller omits it. `hd`,
`hd_flac`, `hd_opus`, `hd_mp3` (all 24kHz) remain available as explicit opt-in overrides for
callers that need higher fidelity than telephony audio.

## Admin console, if you use it

`admin.py`/`console_app.py` (tenant + API key management, voice-profile admin CRUD,
`/admin/sweep`) previously lived on the same pod (port 8001). On Cloud Run it's a **separate**
optional service (`console_app:console_app`) sharing the same image, not automated by this
repo's CI — deploy it manually per `README.md` step 5 if these endpoints are in active use.

## Cutover checklist

1. Provision the full tenant key set in Secret Manager (see "Auth" above).
2. Resolve the voice-profile persistence question and provision a GCS volume mount if the RunPod
   deployment had one (see "Voice profile persistence" above).
3. Point a staging/canary slice of traffic at the new Cloud Run URL. Monitor latency (especially
   the cold-start tail) and error rate before a full cutover.
4. Update client config: swap the base URL, bump client-side request timeout to comfortably
   cover a cold start (90s+ to be safe). Everything else — request body, auth header, response
   parsing — stays the same.
5. Full cutover. Keep the RunPod pod available for a rollback window if credits allow; otherwise
   decommission per the credit situation that motivated this migration.
6. Post-cutover: tune `DEFAULT_NUM_STEP` / `CPU_INT8` via the existing `POST /admin/sweep` A/B
   endpoint — see `README.md`'s "CPU performance tuning" section. Don't ship a quality/speed
   tradeoff without listening to it first.
