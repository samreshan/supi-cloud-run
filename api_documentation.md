# Supi v1 — Text-to-Speech API Documentation

**Supi v1** is a commercial, multilingual Text-to-Speech (TTS) API. It exposes a persistent,
warm-booted FastAPI service that turns text into natural, lifelike speech — built for telephony
(IVR/VoIP) as well as high-fidelity web and app playback.

By default Supi synthesizes input text, resamples it to **8kHz**, and compresses it using the ITU-T
G.711 **U-LAW** codec inside a **WAV** container — ready to stream into Asterisk, FreePBX, Vicidial,
or other VoIP telephony platforms.

For natural, high-fidelity playback (web/app use), request a high-fidelity `output_format` (`hd`,
`hd_flac`, `hd_opus`, `hd_mp3`) to receive the model's native **24kHz** audio without the telephony
downsampling. See [§8 Output Formats](#8-output-formats) and [§9 Emotion & Style Tags](#9-emotion--style-tags).

> **Branding:** This product is **Supi v1**.

---

## Contents

1. [Connection & Base URL](#1-connection--base-url)
2. [Endpoints at a glance](#2-endpoints-at-a-glance)
3. [Multi-tenant model](#3-multi-tenant-model)
4. [How do I authenticate?](#4-how-do-i-authenticate)
5. [How do I generate audio?](#5-how-do-i-generate-audio)
6. [What do I get back?](#6-what-do-i-get-back)
7. [How much credit do I have left?](#7-how-much-credit-do-i-have-left)
8. [Output Formats](#8-output-formats)
9. [Emotion & Style Tags](#9-emotion--style-tags)
10. [Reproducibility](#10-reproducibility)
11. [What errors can occur?](#11-what-errors-can-occur)
12. [Getting the docs](#12-getting-the-docs)
13. [Security & operations](#13-security--operations)
14. [Managing tenants & API keys (admin API)](#14-managing-tenants--api-keys-admin-api)
15. [Voice profiles](#15-voice-profiles)

---

## 1. Connection & Base URL
**Today** the service runs on RunPod, so use the endpoint:
* **RunPod Proxy URL:** `https://vrvyxdo6ehqssf-8000.proxy.runpod.net`


```bash
export BASE_URL="https://<YOUR_POD_ID>-8000.proxy.runpod.net"
export API_KEY="<YOUR_API_KEY>"
```

---

## 2. Endpoints at a glance

| Method & Path | Auth | Purpose |
| :--- | :--- | :--- |
| `GET /` | No | Service index + links to every documentation surface. |
| `GET /health` | No | Liveness probe. |
| `GET /api-docs` | No | This manual, served verbatim as Markdown (curl-friendly). |
| `GET /docs` | No | Interactive **Swagger UI** (try requests in the browser). |
| `GET /redoc` | No | Alternate **ReDoc** reference. |
| `GET /openapi.json` | No | Machine-readable OpenAPI schema. |
| `POST /tts` | **Yes** | Generate audio (`generate` / `bulk_generate`). |
| `GET /credits` | **Yes** | Calling tenant's remaining credit balance & usage. |
| `POST /credits/request` | **Yes** | Request a credit top-up (pending operator approval). See [§7](#7-how-much-credit-do-i-have-left). |
| `GET /credits/requests` | **Yes** | List your own credit requests and their status. |
| `GET /voices` | **Yes** | Voice profiles available to the calling tenant (defaults, public, owned, assigned). See [§15](#15-voice-profiles). |

---

## 3. Multi-tenant model

Supi is multi-tenant: every customer ("tenant") is issued one or more API keys. Authentication,
billing, and rate limiting are all scoped **per tenant**:

* **Identity:** each `X-API-Key` resolves to a tenant. A tenant may hold **multiple keys** (for key
  rotation or per-environment keys); they share one credit balance and one rate-limit bucket.
* **Isolation:** one tenant can never see or spend another tenant's credits, and one tenant's
  traffic can never exhaust another's rate-limit allowance.
* **Billing:** credits ([§7](#7-how-much-credit-do-i-have-left)) are tracked per tenant.
* **Provisioning (operators):** two modes — a static `API_KEYS` env var for small/fixed setups, or
  the **runtime admin API** (`/admin/*`, backed by a persistent store) to create tenants, mint/revoke
  keys, and top up credits with no restart. See [§14](#14-managing-tenants--api-keys-admin-api). The
  admin API is the backend the customer console (`console.supi.cc`) is built on.

> Keys look like `sk_live_…` (production) — treat them as secrets. If a key leaks, rotate it: issue
> a new key for the tenant and remove the old one. Because a tenant can hold several keys, rotation
> is zero-downtime.

---

## 4. How do I authenticate?

Every protected request must include your secret key in the **`X-API-Key`** header.

* **Required by default (fail-closed):** Configure tenant keys server-side via `API_KEYS` (multi
  tenant) or the legacy single `API_KEY`. With `REQUIRE_AUTH=true` (the default), protected routes
  return **503** until at least one key is configured — the service can never be accidentally
  exposed wide open.
* **Local dev without auth:** Set `REQUIRE_AUTH=false` to explicitly disable authentication
  (development only).
* **Headers required on every authenticated request:**
  ```http
  X-API-Key: <YOUR_API_KEY>
  Content-Type: application/json
  ```

Keys are verified in **constant time** (SHA-256 digest comparison), so a wrong key cannot be
recovered by measuring response timing.

### Quick check

A correct key returns `200`; a missing/wrong key returns `401`.

```bash
# Should succeed (200) and print your balance:
curl -s "$BASE_URL/credits" -H "X-API-Key: $API_KEY"

# Should fail (401):
curl -s -o /dev/null -w "%{http_code}\n" "$BASE_URL/credits" -H "X-API-Key: wrong-key"
```

### Rate limiting & request limits

Rate limits are applied **per tenant** (multiple keys for one tenant share the bucket).

| Control | Default | Env var |
| :--- | :--- | :--- |
| Requests per tenant (per API key tenant, else IP) | `60/minute` → HTTP **429** | `RATE_LIMIT` |
| Max `text` / `template` length | `8000` chars → HTTP **422** | `MAX_TEXT_CHARS` |
| Max `bulk_generate` items | `100` → HTTP **422** | `MAX_BULK_ITEMS` |
| `num_step` range | `1`–`128` | — |
| `speed` range | `0.25`–`4.0` | — |
| Max reference-audio download size | `25 MB` | `REF_AUDIO_MAX_BYTES` |
| Reference-audio URL | `https` + public IP only | `ALLOW_INSECURE_REF_URL` |

`ref_audio_url` is fetched with SSRF protection: it must be `https` (by default) and must not
resolve to a private/loopback/link-local address.

---

## 5. How do I generate audio?

Send a `POST /tts` request with a JSON body. The `action` field selects the mode:

* `"generate"` — synthesize a single piece of `text`.
* `"bulk_generate"` — interpolate `data` rows into a `template` and synthesize each in one batched call.

### `POST /tts` request parameters

| Parameter | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `action` | `string` | **Yes** | `"generate"` or `"bulk_generate"`. |
| `text` | `string` | Conditional | Text to synthesize. Required when `action` is `"generate"`. |
| `template` | `string` | Conditional | Interpolation template. Required when `action` is `"bulk_generate"`. |
| `data` | `array[object]` | Conditional | Key/value rows to interpolate into `template`. Required when `action` is `"bulk_generate"`. |
| `ref_audio_url` | `string` | No | HTTPS URL to a reference `.wav` for zero-shot voice cloning. |
| `ref_text` | `string` | No | Transcript of the reference audio (improves cloning accuracy). |
| `voice_profile_id` | `string` | No | Reuse a saved voice instead of re-cloning. Pass an id from [`GET /voices`](#15-voice-profiles), or a new id **together with** `ref_audio_url` to clone-and-save it as your own. See [§15](#15-voice-profiles). |
| `output_format` | `string` | No | `telephony` (default), `hd`, `hd_flac`, `hd_opus`, `hd_mp3`. See [§8](#8-output-formats). |
| `instruct` | `string` | No | Style/emotion tag, e.g. `sad`, `excited`, `british accent`, `whisper`. See [§9](#9-emotion--style-tags). |
| `quality` | `string` | No | Preset: `speed` (16 steps), `standard` (24), `high` (32, default), `ultra` (64), `max` (128). |
| `num_step` | `int` | No | Explicit inference/flow steps (1–128). Overrides `quality`. |
| `speed` | `float` | No | Speaking-rate multiplier (0.25–4.0; default 1.0). |
| `guidance_scale` | `float` | No | Classifier-Free Guidance scale (0.0–10.0; controls expressiveness). |
| `temperature` | `float` | No | Unified sampling temperature (sets class + scales position temperature). |
| `seed` | `int` | No | Fixes randomness for reproducible output. See [§10](#10-reproducibility). |

### Demo A — Single generation (standard voice)

```bash
curl -s -X POST "$BASE_URL/tts" \
     -H "Content-Type: application/json" \
     -H "X-API-Key: $API_KEY" \
     -d '{
       "action": "generate",
       "text": "नमस्ते, यो सुपी परीक्षण हो।"
     }'
```

Decode and play it without writing a file (macOS/Linux, needs `ffplay`):

```bash
curl -s -X POST "$BASE_URL/tts" \
     -H "Content-Type: application/json" \
     -H "X-API-Key: $API_KEY" \
     -d '{ "action": "generate", "text": "नमस्ते, यो सुपी परीक्षण हो।" }' \
     | python3 -c "import sys,json,base64; sys.stdout.buffer.write(base64.b64decode(json.load(sys.stdin)['audio_base64']))" \
     | ffplay -nodisp -autoexit -i -
``` 

Save it to a `.wav` file instead:

```bash
curl -s -X POST "$BASE_URL/tts" \
     -H "Content-Type: application/json" \
     -H "X-API-Key: $API_KEY" \
     -d '{ "action": "generate", "text": "Hello from Supi." }' \
     | python3 -c "import sys,json,base64; open('out.wav','wb').write(base64.b64decode(json.load(sys.stdin)['audio_base64']))"
```

### Demo B — High-fidelity (24kHz) generation

```bash
curl -s -X POST "$BASE_URL/tts" \
     -H "Content-Type: application/json" \
     -H "X-API-Key: $API_KEY" \
     -d '{
       "action": "generate",
       "text": "This is the high fidelity voice.",
       "output_format": "hd",
       "quality": "ultra",
       "seed": 42
     }'
```

### Demo C — Voice cloning (zero-shot)

Provide a URL to a reference sample; the cloned timbre is applied to your `text`.

```bash
curl -s -X POST "$BASE_URL/tts" \
     -H "Content-Type: application/json" \
     -H "X-API-Key: $API_KEY" \
     -d '{
       "action": "generate",
       "text": "नमस्कार, तपाईको खातामा रकम जम्मा भएको छ।",
       "ref_audio_url": "https://pub-your-bucket.r2.dev/reference_voice.wav",
       "ref_text": "यो मेरो सन्दर्भ आवाज रेकर्ड हो।"
     }'
```

### Demo D — Emotion / style tag

```bash
curl -s -X POST "$BASE_URL/tts" \
     -H "Content-Type: application/json" \
     -H "X-API-Key: $API_KEY" \
     -d '{
       "action": "generate",
       "text": "I am [excited] so glad you came, but [sad] I have to go now."
     }'
```

### Demo E — Bulk campaign generation

Interpolate variables into a template to generate audio for many recipients in one request.

```bash
curl -s -X POST "$BASE_URL/tts" \
     -H "Content-Type: application/json" \
     -H "X-API-Key: $API_KEY" \
     -d '{
       "action": "bulk_generate",
       "template": "नमस्ते {name}, तपाईको {item} डेलिभरीको लागि तयार छ।",
       "data": [
         { "name": "अमूल्य", "item": "किताब" },
         { "name": "बिपिन", "item": "ल्यापटप" }
       ]
     }'
```

Play each result sequentially (`ffplay` cannot read concatenated WAVs in one stream):

```bash
curl -s -X POST "$BASE_URL/tts" \
     -H "Content-Type: application/json" \
     -H "X-API-Key: $API_KEY" \
     -d '{
       "action": "bulk_generate",
       "template": "नमस्ते {name}, तपाईको {item} डेलिभरीको लागि तयार छ।",
       "data": [
         { "name": "अमूल्य", "item": "किताब" },
         { "name": "बिपिन", "item": "ल्यापटप" }
       ]
     }' \
     | python3 -c "
import sys, json, base64, subprocess
for r in json.load(sys.stdin).get('results', []):
    print(f'Playing: \"{r[\"text_generated\"]}\"')
    subprocess.run(['ffplay','-nodisp','-autoexit','-i','-'],
                   input=base64.b64decode(r['audio_base64']),
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
"
```

---

## 6. What do I get back?

All responses are JSON. Audio is returned **Base64-encoded** in the `audio_base64` field — decode it
to get the raw bytes of the container named by `format` (e.g. a WAV file).

### Single generation (`action: "generate"`)

```json
{
  "status": "success",
  "audio_base64": "UklGRiS8AABXQVZFZm10IBIA... [Truncated Base64]",
  "sample_rate": 8000,
  "format": "8kHz_ulaw"
}
```

| Field | Type | Meaning |
| :--- | :--- | :--- |
| `status` | `string` | `"success"` on a completed generation. |
| `audio_base64` | `string` | Base64 of the encoded audio file (decode to get the bytes). |
| `sample_rate` | `int` | Sample rate of the returned audio (e.g. `8000` or `24000`). |
| `format` | `string` | Codec/container label, e.g. `8kHz_ulaw`, `24kHz_pcm16`, `24kHz_mp3`. |
| `credits_remaining` | `number` | **Only present when you are on a metered plan** — your tenant's balance after this call. Absent for unmetered/unlimited tenants and when metering is off. See [§7](#7-how-much-credit-do-i-have-left). |
| `warning` | `string` | **Only present when low on or out of credits** — a human-readable heads-up (e.g. "You have used all of your credits…"). The audio in this same response is still valid; the warning is about the *next* call. |

### Bulk generation (`action: "bulk_generate"`)

```json
{
  "status": "success",
  "results": [
    {
      "payload_data": { "name": "अमूल्य", "item": "किताब" },
      "text_generated": "नमस्ते अमूल्य, तपाईको किताब डेलिभरीको लागि तयार छ।",
      "audio_base64": "UklGRiS8AABXQVZFZm10IBIA... [Truncated Base64]"
    },
    {
      "payload_data": { "name": "बिपिन", "item": "ल्यापटप" },
      "text_generated": "नमस्ते बिपिन, तपाईको ल्यापटप डेलिभरीको लागि तयार छ।",
      "audio_base64": "UklGRiS8AABXQVZFZm10IBIA... [Truncated Base64]"
    }
  ],
  "format": "8kHz_ulaw"
}
```

Each entry of `results` contains the original `payload_data` row, the fully interpolated
`text_generated`, and that item's `audio_base64`. A top-level `credits_remaining` is included when
metering is enabled.

> **Tip:** The `sample_rate` and `format` fields always report exactly what you received — never
> assume; read them back.

---

## 7. How much credit do I have left?

Call **`GET /credits`** with your API key. Credits are tracked **per tenant**.

```bash
curl -s "$BASE_URL/credits" -H "X-API-Key: $API_KEY"
```

**When you are on a metered plan** you get your live balance:

```json
{
  "credits_enabled": true,
  "plan": "metered",
  "metered": true,
  "tenant_id": "acme",
  "unit": "characters",
  "cost_per_char": 1.0,
  "min_charge": 1.0,
  "credits_granted": 50000.0,
  "credits_used": 1240.0,
  "credits_remaining": 48760.0
}
```

**Metered vs. unmetered is set per tenant.** Even when metering is enabled on the server, an operator
can put an individual tenant on an **unlimited (unmetered) plan** — that tenant generates without ever
being charged, and its credit balance is left untouched (so switching back to metered resumes exactly
where it left off). An unmetered tenant sees:

```json
{
  "credits_enabled": true,
  "plan": "unlimited",
  "metered": false,
  "tenant_id": "acme",
  "credits_remaining": null,
  "message": "This account is on an unlimited plan; usage is not charged."
}
```

**When metering is disabled server-wide** (the default) every tenant is unlimited:

```json
{
  "credits_enabled": false,
  "plan": "unlimited",
  "metered": false,
  "credits_remaining": null,
  "message": "Credit metering is disabled; usage is unlimited."
}
```

### How charging works

* **Per tenant, per plan:** balances are isolated per tenant; one tenant never spends another's
  credits. Unmetered tenants are never charged regardless of these rules.
* **Unit:** credits are charged per **character** of synthesized text — `len(text)` for `generate`,
  and the sum of every interpolated `text_generated` for `bulk_generate`.
* **Cost:** `cost_per_char` per character, with a per-request floor of `min_charge`.
* **Live balance + warnings in responses:** on a metered plan, each successful `POST /tts` response
  includes `credits_remaining`, and adds a `warning` string when you have just run out (or dipped to/
  below `CREDITS_LOW_BALANCE_THRESHOLD`, if the operator set one) — so you get a heads-up before the
  next call fails.
* **Running out:** if a request would cost more than you have, `/tts` returns **402 Payment Required**
  and **no audio is generated or charged**. The body is a structured object you can act on
  programmatically — see [§11](#11-what-errors-can-occur).

### Requesting more credits

Out of credits, or planning a larger campaign? Submit a **credit request** and your operator can
approve it from the admin console — no need to email anyone out of band.

```bash
# Ask for a top-up (amount is in credits; note is an optional message to the operator)
curl -s -X POST "$BASE_URL/credits/request" \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"amount": 50000, "note": "Scaling up for the June campaign"}'

# Check the status of your requests (pending / approved / rejected)
curl -s "$BASE_URL/credits/requests" -H "X-API-Key: $API_KEY"
```

A successful submission returns `201` with `"status": "pending"`. When the operator **approves** it,
the credits are added to your balance automatically (visible via `GET /credits`); if **rejected**,
nothing changes and an optional reason is recorded on the request. Credit requests require the server
to run with a persistent store — otherwise these endpoints return **503**.

### Server configuration (operators)

Metering is **off by default** (fail-open). Enable and provision it with environment variables:

| Env var | Default | Meaning |
| :--- | :--- | :--- |
| `CREDITS_ENABLED` | `false` | Master switch. When `false`, usage is unlimited. |
| per-tenant `credits` in `API_KEYS` | — | Starting balance for that tenant (highest precedence). |
| `API_CREDITS` | — | JSON map of `tenant_id → starting balance`, e.g. `{"acme": 50000}`. |
| `CREDITS_DEFAULT` | — | Balance granted to any otherwise-unprovisioned tenant on first use. |
| `CREDITS_PER_CHAR` | `1` | Credits charged per character. |
| `CREDITS_MIN_CHARGE` | `1` | Minimum credits charged per request. |
| `CREDITS_LOW_BALANCE_THRESHOLD` | `0` | When > 0, successful `/tts` responses include a low-balance `warning` once `credits_remaining` falls to/below this value. (`0` = only warn on full exhaustion.) |

> **Per-tenant plans:** whether an individual tenant is metered or unmetered is stored on the tenant
> (persistent store only) and toggled at runtime from the admin console or
> `POST /admin/tenants/{id}/metered` — see [§14](#14-managing-tenants--api-keys-admin-api). With the
> static `MemoryStore`, set `"metered": false` on a key's object in `API_KEYS` to start it unmetered.

> **Note:** Balances are held in-process and reset on restart — suitable for a single replica or
> trials. For durable, multi-replica billing, back the store with Redis/Postgres (see the
> integration surface in `credits.py`). This is the natural integration point for `console.supi.cc`.

---

## 8. Output Formats

Pass `output_format` to choose fidelity. The response `sample_rate` and `format` fields always
report exactly what you received.

| `output_format` | Sample rate | Codec | Use case |
| :--- | :--- | :--- | :--- |
| `telephony` *(default)* | 8 kHz | G.711 µ-law (WAV) | IVR / VoIP / call systems |
| `hd` | 24 kHz | PCM16 (WAV) | Natural, realistic playback |
| `hd_flac` | 24 kHz | FLAC (lossless) | Archival / lossless |
| `hd_opus` | 24 kHz | Opus (OGG) | Small high-quality web payloads |
| `hd_mp3` | 24 kHz | MP3 (64 kbps) | Broad app/browser compatibility |

> **Why this matters:** 8kHz µ-law is phone-line fidelity by design and can never sound
> "hyper-realistic." For natural-sounding voice, use an `hd*` format — it returns the model's full
> 24kHz output with no telephony downsampling.

---

## 9. Emotion & Style Tags

Inline tags like `[sad]` or `[excited]` (or the top-level `instruct` parameter) steer delivery.
Tags split the text into segments; each segment is rendered with its emotion and the segments are
joined with natural pauses.

```json
{ "action": "generate", "text": "I am [excited] so glad you came, but [sad] I have to go now." }
```

**Supported emotion tags** (mapped onto Supi's pitch/rate/temperature controls):
`sad`, `happy`, `excited`, `cheerful`, `angry`, `calm`, `serious`, `fearful`, `whisper`,
`shouting`, `fast`, `slow`. You can combine them: `[sad, slow]`.

You may also use Supi's native style words directly as tags or via `instruct`:
`british accent`, `indian accent`, `high pitch`, `low pitch`, `child`, `elderly`, `male`,
`female`, `whisper`, etc.

> **Limitation:** Emotions are approximated via prosody (pitch/rate/temperature). True non-verbal
> events such as `[laughs]`, `[sighs]`, or `[gasps]` are **not** produced. Those would require an
> emotional reference-audio library or a different model.

---

## 10. Reproducibility

Provide a `seed` to make generation deterministic: the same `text` + parameters + `seed` yields
byte-identical audio. Useful for "locking in" an approved take or for caching.

---

## 11. What errors can occur?

Errors return the appropriate HTTP status with a JSON body of the form `{"detail": "<message>"}`.
Successful audio generation always returns `200`. **Exception:** the `402` out-of-credits error puts a
structured **object** in `detail` (not a string) so you can react programmatically — see below.

| Status | Name | When it happens | How to fix |
| :--- | :--- | :--- | :--- |
| `400` | Bad Request | Unsupported `action`; missing `text` (generate); missing `template`/`data` (bulk); unknown `output_format`. | Check the request body against [§5](#5-how-do-i-generate-audio). |
| `401` | Unauthorized | Missing or invalid `X-API-Key`. | Send the correct key in the `X-API-Key` header. |
| `402` | Payment Required | The request would exceed your tenant's remaining credits (metering enabled). | Reduce text length or top up. See [§7](#7-how-much-credit-do-i-have-left). |
| `422` | Unprocessable Entity | Validation failed: `text`/`template` over `MAX_TEXT_CHARS`, too many bulk items, `num_step`/`speed`/`guidance_scale` out of range, bad/unsafe `ref_audio_url` (SSRF block). | Keep values within the [limits in §4](#rate-limiting--request-limits). |
| `429` | Too Many Requests | Tenant rate limit exceeded (default `60/minute`). | Back off and retry; raise `RATE_LIMIT` server-side if needed. |
| `500` | Internal Server Error | Model/codec/internal failure during synthesis. | Retry; if it persists, check server logs. |
| `503` | Service Unavailable | Model not yet loaded/failed to load, **or** auth required but no API keys configured on the server. | Wait for warm-boot / configure `API_KEYS`. Check `GET /health`. |

### Example error responses

Missing text (`400`):
```json
{ "detail": "Missing required parameter 'text' for action 'generate'." }
```

Invalid key (`401`):
```json
{ "detail": "Invalid or missing API Key." }
```

Out of credits (`402`) — `detail` is a structured object:
```json
{
  "detail": {
    "error": "insufficient_credits",
    "message": "Insufficient credits: this request needs ~250 credit(s) but only 80 remain. Top up to continue.",
    "credits_required": 250,
    "credits_remaining": 80,
    "unit": "characters",
    "hint": "Submit POST /credits/request to ask the operator for more credits, then retry."
  }
}
```

Rate limited (`429`):
```json
{ "error": "Rate limit exceeded: 60 per 1 minute" }
```

### Checking errors with curl

Print just the HTTP status code to confirm behavior:

```bash
# 401 — wrong key
curl -s -o /dev/null -w "%{http_code}\n" -X POST "$BASE_URL/tts" \
     -H "Content-Type: application/json" -H "X-API-Key: wrong-key" \
     -d '{"action":"generate","text":"hi"}'

# 400 — missing text
curl -s -o /dev/null -w "%{http_code}\n" -X POST "$BASE_URL/tts" \
     -H "Content-Type: application/json" -H "X-API-Key: $API_KEY" \
     -d '{"action":"generate"}'
```

---

## 12. Getting the docs

This manual is available at runtime — no repo checkout needed:

| What | URL | Best for |
| :--- | :--- | :--- |
| Service index (links to everything) | `GET /` | Discovery |
| This Markdown manual (verbatim) | `GET /api-docs` | `curl`, scripts, offline reading |
| Interactive **Swagger UI** | `GET /docs` | Trying requests in the browser |
| **ReDoc** reference | `GET /redoc` | Clean, readable reference |
| OpenAPI schema (JSON) | `GET /openapi.json` | Codegen / tooling |

```bash
# Fetch this manual as Markdown
curl -s "$BASE_URL/api-docs"

# Discover the service and its doc links
curl -s "$BASE_URL/" | python3 -m json.tool

# Liveness probe
curl -s "$BASE_URL/health"
```

---

## 13. Security & operations

These settings are configured server-side (RunPod environment variables today; the customer console
at `console.supi.cc` later). They are listed here so integrators understand the security posture.

### Tenant & key provisioning — two modes

Supi supports two backends, chosen by whether `TENANT_DB_PATH` is set:

**1. Static (default) — `MemoryStore`.** Tenants/keys come from env vars; good for a few fixed
customers. Changes require a pod restart.

* **`API_KEYS`** — JSON object mapping each API key to a tenant. Two value shapes:
  ```jsonc
  {
    "sk_live_acme":   { "tenant_id": "acme", "name": "Acme Corp", "credits": 50000 },
    "sk_live_globex": "globex"
  }
  ```
  * Object form attaches a display `name` and an optional starting `credits` grant.
  * String form is shorthand for `{ "tenant_id": "<string>" }`.
  * Multiple keys may share a `tenant_id` (key rotation / per-environment keys).
* **`API_KEY`** *(legacy)* — a single key, registered as one tenant (`API_TENANT`, default `default`).
  Used only when `API_KEYS` is unset.

**2. Persistent — `SqliteStore` (recommended for production).** Set **`TENANT_DB_PATH`** (e.g.
`/runpod-volume/supi.db`, on a RunPod volume so it survives restarts) to store tenants, keys, and
balances in SQLite and unlock the **runtime admin API** ([§14](#14-managing-tenants--api-keys-admin-api)).
In this mode `API_KEYS` is ignored — manage everything via `/admin/*`. **API keys are stored only as
SHA-256 hashes**, so they can never be read back. Set **`ADMIN_API_KEY`** to enable the admin API.

* **`REQUIRE_AUTH`** — `true` (default) refuses to serve protected routes until an auth mechanism is
  configured. (With a persistent store, auth is considered configured even before the first key
  exists, so unknown keys get `401`, not `503`.)

### Hardening checklist

* **Keys are secrets.** Issue `sk_live_…`-style keys, transmit only over HTTPS, never embed them in
  client-side code. Rotate by adding a new key for the tenant and removing the old one.
* **Constant-time auth.** Keys are matched via SHA-256 digests, so timing cannot reveal valid keys.
* **Per-tenant isolation.** Credits and rate-limit buckets are scoped per tenant.
* **Admin is off the public port.** Tenant management runs on a separate port (`ADMIN_PORT`, default
  8001) guarded by `ADMIN_API_KEY` — the public API (8000) exposes no `/admin/*`. Reach the admin
  port over the RunPod HTTPS proxy and restrict who can access it. The console starts only when
  `ADMIN_API_KEY` is set.
* **Admin brute-force lockout.** The admin key is compared in constant time, and after
  `ADMIN_MAX_AUTH_FAILURES` (default 5) bad attempts a client IP is locked out for
  `ADMIN_AUTH_LOCKOUT_SECONDS` (default 300s, returns `429` + `Retry-After`). Use a long random
  `ADMIN_API_KEY`; a key shorter than `ADMIN_MIN_KEY_LENGTH` (24) logs a startup warning.
* **Security response headers.** Both ports send hardening headers on every response —
  `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY` + a `frame-ancestors 'none'` CSP
  (anti-clickjacking), `Referrer-Policy`, and HSTS (over HTTPS). The admin console additionally uses a
  strict same-origin CSP and `Cache-Control: no-store`.
* **SSRF protection.** `ref_audio_url` must be `https` to a public IP (no loopback/private/link-local),
  capped at `REF_AUDIO_MAX_BYTES` (25 MB). Override only in trusted dev with `ALLOW_INSECURE_REF_URL`.
* **CORS is closed by default.** Set `CORS_ALLOW_ORIGINS` (comma-separated) to allow browser clients
  from `supi.cc` / `console.supi.cc`.
* **No PII in logs.** Request text is not logged.
* **Health checks** (`GET /health`) disclose no internal details (device/dtype).

### Key environment variables

| Env var | Default | Purpose |
| :--- | :--- | :--- |
| `TENANT_DB_PATH` | *(unset → MemoryStore)* | Path to SQLite DB; enables persistent store + admin API. |
| `ADMIN_API_KEY` | — | Secret for the admin console + `/admin/*` (header `X-Admin-Key`). Console only starts when set. |
| `ADMIN_PORT` | `8001` | Port the operator-only admin console/API listens on (separate from the 8000 API). |
| `ADMIN_MIN_KEY_LENGTH` | `24` | Below this length, `ADMIN_API_KEY` triggers a startup "weak key" warning. |
| `ADMIN_MAX_AUTH_FAILURES` | `5` | Failed admin-key attempts (per client IP) before lockout. |
| `ADMIN_AUTH_WINDOW_SECONDS` | `300` | Rolling window over which failed admin attempts are counted. |
| `ADMIN_AUTH_LOCKOUT_SECONDS` | `300` | Lockout duration after too many failed admin attempts (`429`). |
| `HSTS_ENABLED` | `true` | Send `Strict-Transport-Security` (honoured by browsers only over HTTPS). |
| `API_KEYS` | — | Static multi-tenant key → tenant registry (JSON). Ignored when `TENANT_DB_PATH` set. |
| `API_KEY` / `API_TENANT` | — / `default` | Legacy single-tenant key. |
| `REQUIRE_AUTH` | `true` | Fail-closed auth gate. |
| `RATE_LIMIT` | `60/minute` | Per-tenant request rate limit. |
| `CORS_ALLOW_ORIGINS` | *(closed)* | Comma-separated allowed browser origins. |
| `MAX_TEXT_CHARS` | `8000` | Max characters per `text`/`template`. |
| `MAX_BULK_ITEMS` | `100` | Max rows per `bulk_generate`. |
| `REF_AUDIO_MAX_BYTES` | `26214400` | Max reference-audio download size. |
| `ALLOW_INSECURE_REF_URL` | `false` | Allow non-HTTPS/private `ref_audio_url` (dev only). |
| `VOICE_PROFILES_DIR` | `./voice_profiles` | Working voice cache (fresh clones land here; may be ephemeral). |
| `VOICE_PROFILE_PERSIST_DIR` | *(= `VOICE_PROFILES_DIR`)* | Durable volume (e.g. `/runpod-volume/voice_profiles`); persisted voices are copied here and warm-loaded on restart. |
| `VOICE_PROFILE_DB_PATH` | *(in the persist dir)* | Override path for the voice-profile registry DB. |
| `DEFAULT_VOICE_PROFILES` | — | JSON list of built-in default voices to seed (see [§15](#15-voice-profiles)). |
| `CREDITS_ENABLED` | `false` | Enable per-tenant credit metering. |
| `API_CREDITS` | — | `tenant_id → balance` seed map (JSON). |
| `CREDITS_DEFAULT` | — | Grant for unprovisioned tenants. |
| `CREDITS_PER_CHAR` | `1` | Credits per character. |
| `CREDITS_MIN_CHARGE` | `1` | Minimum credits per request. |
| `CREDITS_LOW_BALANCE_THRESHOLD` | `0` | Warn in `/tts` responses once a metered balance falls to/below this (`0` = warn only when exhausted). |

> **Per-tenant metering** (metered vs. unlimited) is a property of each tenant in the persistent
> store, toggled via the console or `POST /admin/tenants/{id}/metered` — not an env var.

---

## 14. Managing tenants & API keys (admin API)

This is how you **manage tenants** and **generate API keys for tenants**. Management runs as a
**separate admin service on its own port** (`ADMIN_PORT`, default **8001**) — deliberately *not* on
the public API (8000) — so you can lock it down independently. That port serves both:

* a **minimal web console** (a single black-and-white page) at `GET /`, and
* the **admin API** (`/admin/*`) the console calls.

### Prerequisites

The admin service requires the **persistent store** and its own secret, set server-side (RunPod →
Environment Variables), then restart once:

```bash
TENANT_DB_PATH=/workspace/supi.db     # SQLite on a PERSISTENT volume (not /app — that's ephemeral)
ADMIN_API_KEY=<a long random secret>  # protects the console + /admin/*; if unset, the console DOESN'T start
ADMIN_PORT=8001                       # optional; the port the console listens on (default 8001)
CREDITS_ENABLED=true                  # if you want metering
```

Generate a strong admin secret:
```bash
python3 -c "import secrets; print('admin_' + secrets.token_urlsafe(48))"
```

> **Security:** the console only starts when `ADMIN_API_KEY` is set. Expose its port over the RunPod
> **HTTPS** proxy (`https://<POD_ID>-8001.proxy.runpod.net`) so the admin key isn't sent in clear,
> and restrict who can reach it. On the default (MemoryStore) setup — no `TENANT_DB_PATH` — the admin
> API returns **503**, because there's nothing durable to write to.

### Web console (the simplest way)

1. Open `https://<YOUR_POD_ID>-8001.proxy.runpod.net/` in a browser.
2. Paste your `ADMIN_API_KEY` into the field and click **Connect** (it's kept only in the tab's
   `sessionStorage`, never sent anywhere but the `X-Admin-Key` header).
3. Create tenants, **Mint key** (copy the shown key — it appears only once), **+ credits**, view a
   tenant's keys, **Revoke**, or **Delete**. Everything updates live; no restart.
4. Each tenant row shows its **plan** (metered/unmetered) with a one-click **Meter / Unmeter** toggle:
   **Unmeter** puts that tenant on an unlimited plan (no credit charges, balance preserved); **Meter**
   switches it back. Unmetered tenants show `∞` for remaining.
5. Review **Credit requests** submitted by tenants (the section shows a pending count); click
   **Approve** to grant the requested credits or **Reject** with an optional reason.

### Admin API (curl / scripting)

All `/admin/*` calls authenticate with the **`X-Admin-Key`** header (separate from tenant keys); a
wrong/missing key returns **401**, and the service returns **503** if `ADMIN_API_KEY` is unset or the
store isn't persistent. Point your shell at the admin port:

```bash
export ADMIN_URL="https://<YOUR_POD_ID>-8001.proxy.runpod.net"
export ADMIN_KEY="<your ADMIN_API_KEY>"
```

### Admin endpoints

| Method & Path | Purpose |
| :--- | :--- |
| `POST /admin/tenants` | Create a tenant (with optional starting credits). |
| `GET /admin/tenants` | List all tenants (balances + active-key counts). |
| `GET /admin/tenants/{tenant_id}` | Get one tenant + its keys (ids/prefixes only). |
| `POST /admin/tenants/{tenant_id}/keys` | **Mint a new API key** (plaintext returned once). |
| `GET /admin/tenants/{tenant_id}/keys` | List a tenant's keys (no secrets). |
| `POST /admin/tenants/{tenant_id}/credits` | Top up a tenant's credit balance. |
| `POST /admin/tenants/{tenant_id}/metered` | Switch a tenant between metered billing and an unlimited (unmetered) plan — body `{ "metered": true\|false }`. |
| `GET /admin/credit-requests` | List tenant credit requests; filter with `?status=pending\|approved\|rejected`. |
| `POST /admin/credit-requests/{request_id}/approve` | Approve a request — grants the credits atomically. |
| `POST /admin/credit-requests/{request_id}/reject` | Reject a request (no credits granted). |
| `DELETE /admin/keys/{key_id}` | Revoke a single key. |
| `DELETE /admin/tenants/{tenant_id}` | Delete a tenant and all its keys. |
| `GET /admin/voices` | List every registered voice profile (owner, visibility, defaults, assignments, readiness). |
| `GET /admin/voices/disk` | List raw cached `.pt` files on disk, flagging registry "orphans". |
| `GET /admin/voices/lookup?profile_id=…` | Fetch one voice profile's full metadata. |
| `POST /admin/voices/visibility` | Publish (`public`) or restrict (`private`) a voice. |
| `POST /admin/voices/default` | Mark/unmark a voice as a built-in default shown to all. |
| `POST /admin/voices/persist` | Keep a voice on the persistent drive + warm-load it on restart. |
| `POST /admin/voices/grants` | Assign a private voice to a specific tenant. |
| `DELETE /admin/voices/grants?profile_id=…&tenant_id=…` | Remove a tenant's assignment. |
| `PATCH /admin/voices` | Rename a voice or edit its description. |
| `DELETE /admin/voices?profile_id=…` | Delete a voice's metadata (and its cached audio). |

> **`profile_id` travels in the JSON body (POST/PATCH) or query string (GET/DELETE), never in the URL
> path.** Voice ids are frequently full URLs (e.g. `https://host/voice-profiles/<uuid>/combined.wav`);
> putting them in the path would break routing on the slashes. Pass it as a field, e.g.
> `{ "profile_id": "<id>", "tenant_id": "acme" }`.
>
> Voice management works on **any** tenant-store backend — the voice registry is its own durable
> index next to the cached audio, independent of `TENANT_DB_PATH`. See [§15](#15-voice-profiles).

### Walkthrough — onboard a tenant and issue its key

**1. Create the tenant** (with a starting balance of 50,000 credits):

```bash
curl -s -X POST "$ADMIN_URL/admin/tenants" \
     -H "X-Admin-Key: $ADMIN_KEY" -H "Content-Type: application/json" \
     -d '{ "tenant_id": "acme", "name": "Acme Corp", "credits": 50000 }'
```

**2. Generate (mint) an API key for that tenant.** The plaintext key is returned **once** — copy it
now; only its hash is stored, so it cannot be retrieved later.

```bash
curl -s -X POST "$ADMIN_URL/admin/tenants/acme/keys" \
     -H "X-Admin-Key: $ADMIN_KEY" -H "Content-Type: application/json" -d '{}'
```
```json
{
  "api_key": "sk_live_Xa9...Qe2",
  "key_id": "key_1f3c8b2a9d4e5f60",
  "key_prefix": "sk_live_Xa9…",
  "tenant_id": "acme",
  "warning": "Store this api_key now; it cannot be retrieved again."
}
```

**3. Hand `api_key` to the customer.** They use it against the **public API** (port 8000) exactly as
in [§4](#4-how-do-i-authenticate):

```bash
curl -s "$BASE_URL/credits" -H "X-API-Key: sk_live_Xa9...Qe2"
```

### Day-2 operations

```bash
# List tenants
curl -s "$ADMIN_URL/admin/tenants" -H "X-Admin-Key: $ADMIN_KEY"

# Inspect one tenant (balance + keys, no secrets)
curl -s "$ADMIN_URL/admin/tenants/acme" -H "X-Admin-Key: $ADMIN_KEY"

# Top up 10,000 credits
curl -s -X POST "$ADMIN_URL/admin/tenants/acme/credits" \
     -H "X-Admin-Key: $ADMIN_KEY" -H "Content-Type: application/json" \
     -d '{ "amount": 10000 }'

# Put a tenant on an unlimited (unmetered) plan — generates without ever spending credits
curl -s -X POST "$ADMIN_URL/admin/tenants/acme/metered" \
     -H "X-Admin-Key: $ADMIN_KEY" -H "Content-Type: application/json" \
     -d '{ "metered": false }'
# ...and switch it back to metered billing later (balance was preserved):
curl -s -X POST "$ADMIN_URL/admin/tenants/acme/metered" \
     -H "X-Admin-Key: $ADMIN_KEY" -H "Content-Type: application/json" \
     -d '{ "metered": true }'

# Rotate a key: mint a new one (step 2 above), ship it, then revoke the old by its key_id
curl -s -X DELETE "$ADMIN_URL/admin/keys/key_1f3c8b2a9d4e5f60" -H "X-Admin-Key: $ADMIN_KEY"

# Offboard a tenant (deletes the tenant and all its keys)
curl -s -X DELETE "$ADMIN_URL/admin/tenants/acme" -H "X-Admin-Key: $ADMIN_KEY"
```

> **Zero-downtime key rotation:** a tenant can hold several keys at once (they share one balance and
> rate-limit bucket). Mint the replacement, deploy it, then revoke the old key — no interruption.

### Where this is headed

The console today is the minimal built-in page above, running on the admin port. `console.supi.cc`
will be a fuller web UI on top of these same `/admin/*` endpoints (and eventually proper admin
*accounts* — users/roles/audit — rather than one shared `ADMIN_API_KEY`). The storage layer
(`store.py`) is written so the SQLite backend can be swapped for Postgres without touching the API.

---

## 15. Voice profiles

A **voice profile** is a saved, reusable voice. Instead of sending `ref_audio_url` on every request,
you clone a voice once and then refer to it by a short `voice_profile_id`. Each tenant sees only the
voices it is allowed to use:

* **Default** — built-in voices Supi ships for everyone.
* **Public** — voices an operator has published to all tenants.
* **Owned** — voices **you** cloned (private to your account by default).
* **Assigned** — a private voice an operator has shared with your account specifically.

### List the voices you can use

```bash
curl -s "$BASE_URL/voices" -H "X-API-Key: $API_KEY"
```
```json
{
  "voices": [
    { "voice_profile_id": "narrator-en", "name": "English Narrator",
      "default": true,  "visibility": "public",  "relation": "default", "ready": true,
      "description": "Warm neutral narrator.", "created_at": "2026-06-01T10:00:00Z" },
    { "voice_profile_id": "acme-brand", "name": "Acme Brand Voice",
      "default": false, "visibility": "private", "relation": "owner",   "ready": true,
      "description": "", "created_at": "2026-06-18T09:12:00Z" }
  ],
  "count": 2
}
```

* `name` — the **display name** for the voice. Show this to your users instead of the raw
  `voice_profile_id` (which may be a long internal id or URL). Operators set friendly names from the
  admin console; until one is set, `name` falls back to the id. You still pass `voice_profile_id` —
  not `name` — to `POST /tts` to select the voice.
* `relation` — why it is available to you: `default`, `public`, `owner`, or `shared`.
* `ready` — `true` when the voice has been cloned and can synthesize **right now**. A listed voice
  with `ready: false` exists but still needs its reference audio (see below) before first use.

### Create your own voice (clone & save)

Send `ref_audio_url` **together with** a new `voice_profile_id`. Supi clones the voice, caches it,
and registers it as a **private** profile owned by your account — reuse it later by id alone:

```bash
# First call: clone and save under "acme-brand"
curl -s -X POST "$BASE_URL/tts" -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{ "action":"generate", "text":"Welcome to Acme.",
        "voice_profile_id":"acme-brand",
        "ref_audio_url":"https://example.com/acme_sample.wav" }'

# Later calls: reuse by id (no re-cloning, no ref_audio_url needed)
curl -s -X POST "$BASE_URL/tts" -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{ "action":"generate", "text":"Your order has shipped.", "voice_profile_id":"acme-brand" }'
```

### Choose a voice for generation

Pass any `voice_profile_id` from `GET /voices` to `POST /tts`:

```bash
curl -s -X POST "$BASE_URL/tts" -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{ "action":"generate", "text":"नमस्ते!", "voice_profile_id":"narrator-en" }'
```

**Access rules.** Requesting a voice you cannot use returns **403**; an unknown id with no
`ref_audio_url` to create it returns **404**. You may use, but **not overwrite**, public/assigned
voices — sending `ref_audio_url` with someone else's profile id is ignored and the saved voice is
used. Only the owner can re-clone over their own profile.

### Managing voices (operators)

Use the **admin console** (the separate admin port) or the `/admin/voices` API to manage who can use
each cached voice. From the console's **Voice profiles** panel you can:

* **▶ Preview** — play the voice's reference clip right in the console before deciding what to do with
  it. (Each admin listing row carries a `preview_url`: the clip the voice was cloned from, or — for
  voices keyed by an audio URL — that URL itself. It is `null` when no playable source is on file.)
* **Rename** — set the display **name** tenants receive from `GET /voices` instead of the raw id
  (`PATCH /admin/voices` with `{ "profile_id", "name" }`).
* **Assign…** — opens a picker listing every tenant; assign or remove the voice for each.
* Publish to all tenants, mark as a default, or delete.

(`profile_id` always goes in the body/query, so URL-shaped ids work.)

```bash
# See every cloned/cached voice
curl -s "$ADMIN_URL/admin/voices" -H "X-Admin-Key: $ADMIN_KEY"

# Publish a tenant's voice to everyone
curl -s -X POST "$ADMIN_URL/admin/voices/visibility" \
     -H "X-Admin-Key: $ADMIN_KEY" -H "Content-Type: application/json" \
     -d '{ "profile_id": "acme-brand", "visibility": "public" }'

# Or assign it to one specific tenant (keeps it private to others)
curl -s -X POST "$ADMIN_URL/admin/voices/grants" \
     -H "X-Admin-Key: $ADMIN_KEY" -H "Content-Type: application/json" \
     -d '{ "profile_id": "acme-brand", "tenant_id": "globex" }'

# Mark a voice as a built-in default shown to all tenants
curl -s -X POST "$ADMIN_URL/admin/voices/default" \
     -H "X-Admin-Key: $ADMIN_KEY" -H "Content-Type: application/json" \
     -d '{ "profile_id": "narrator-en", "is_default": true }'
```

Operators can pre-seed built-in defaults at startup with the `DEFAULT_VOICE_PROFILES` env var (a JSON
list of `{ "profile_id", "name", "description" }`); each still needs its reference audio cloned once
before it reports `ready`.

### Persisting voices across restarts (operators)

Cloned voices are cached so they aren't re-cloned on every request, but on RunPod the container disk
is **ephemeral** — anything not on a mounted volume is lost when the pod restarts. Operators choose
**which** voices are worth keeping permanently:

```bash
# Keep this voice on the persistent drive (and auto-load it into memory on the next restart)
curl -s -X POST "$ADMIN_URL/admin/voices/persist" \
     -H "X-Admin-Key: $ADMIN_KEY" -H "Content-Type: application/json" \
     -d '{ "profile_id": "acme-brand", "persistent": true }'
```

How it works:

1. **Point `VOICE_PROFILE_PERSIST_DIR` at a mounted volume** (e.g. `/runpod-volume/voice_profiles`).
   Marking a voice persistent copies its cached tensor there; the registry index lives there too, so
   the id→file mapping survives a restart. (If you instead mount the volume directly at
   `VOICE_PROFILES_DIR`, everything is already durable and "persistent" is purely the warm-load flag.)
2. **On restart**, the server scans the registry for persistent voices, loads each one's cached tensor
   straight into memory, and **pins** it (exempt from cache eviction) — so curated voices are ready on
   the very first request, with no re-clone and no cold disk read.

Non-persistent clones stay only in the working cache and are dropped on restart (they'll re-clone from
their reference audio on next use). Persistent voices are pinned in memory, so curate sensibly. The
warm-load/pin change applies at the model process's next restart; the durable copy is written
immediately.
