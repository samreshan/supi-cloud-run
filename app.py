"""
app.py — FastAPI service for the Supi v1 Text-to-Speech API (public HTTP surface).

Supi v1 is the commercial product; the underlying synthesis engine lives in [tts_core.py]. This
module owns the HTTP concerns: model lifecycle, multi-tenant authentication ([tenancy.py]),
per-tenant credit metering ([credits.py]), rate limiting, request validation, CORS, and translating
core errors to HTTP responses.
"""

import os
import io
import time
import wave
import signal
import asyncio
import base64
import logging
import itertools
from pathlib import Path
from typing import List, Dict, Any, Optional, Annotated
from contextlib import asynccontextmanager

import torch
from fastapi import FastAPI, HTTPException, status, Security, Request, Depends
from fastapi.security import APIKeyHeader
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

import admin
import tts_core
import credits
import tenancy
import voices
import security
from store import AdminNotSupported, TenantNotFound
from tts_core import TTSInputError, TTSServerError

# Re-exported so existing tests / external imports keep working.
from tts_core import (  # noqa: F401
    normalize_text_numbers, normalize_text, filter_instruct, parse_emotion_segments,
    process_to_ulaw, process_audio, download_audio, get_or_create_voice_prompt,
    contains_devanagari, VALID_INSTRUCTS, EMOTION_MAP, DEVICE, DTYPE,
)

logger = logging.getLogger("supi.app")

# ==============================================================================
# Model lifecycle (load at import; fail soft so the process/tests can still start)
# ==============================================================================
try:
    model = tts_core.load_model()
except Exception as e:
    logger.error("CRITICAL: Failed to initialise model globally: %s", e)
    model = None

# ==============================================================================
# Idle-shutdown watchdog (Cloud Run) — Cloud Run's own scale-to-zero (min-instances=0) already
# means we pay nothing while idle, but it doesn't expose an exact configurable "stay warm for N
# minutes" knob. This watchdog makes that deterministic: after IDLE_TIMEOUT_SECONDS (default 300 =
# 5 min) with no incoming HTTP request, the process sends itself SIGTERM so the container exits
# cleanly and Cloud Run tears the instance down. The next request triggers a fresh cold start.
# Set IDLE_TIMEOUT_SECONDS=0 to disable (e.g. if deploying with --min-instances > 0).
# ==============================================================================
IDLE_TIMEOUT_SECONDS = int(os.getenv("IDLE_TIMEOUT_SECONDS", "300"))
_last_activity = time.monotonic()


def _touch_activity() -> None:
    global _last_activity
    _last_activity = time.monotonic()


async def _idle_watchdog() -> None:
    if IDLE_TIMEOUT_SECONDS <= 0:
        return
    poll_seconds = min(30, max(5, IDLE_TIMEOUT_SECONDS // 4))
    while True:
        await asyncio.sleep(poll_seconds)
        idle_for = time.monotonic() - _last_activity
        if idle_for >= IDLE_TIMEOUT_SECONDS:
            logger.info(
                "Idle for %.0fs (>= IDLE_TIMEOUT_SECONDS=%d); shutting down so Cloud Run "
                "scales this instance to zero.", idle_for, IDLE_TIMEOUT_SECONDS,
            )
            os.kill(os.getpid(), signal.SIGTERM)
            return


@asynccontextmanager
async def lifespan(app: FastAPI):
    global model
    if model is not None:
        logger.info("Supi v1 API: model loaded on %s (%s); warming up...", DEVICE, DTYPE)
        tts_core.warmup(model)
        tts_core.warm_load_persistent_profiles()  # pre-load operator-pinned voices from disk
        logger.info("Supi v1 API: warm-boot complete, ready to serve.")
    else:
        logger.error("Supi v1 API: model failed to load — service is unhealthy.")
    watchdog_task = asyncio.create_task(_idle_watchdog())
    yield
    watchdog_task.cancel()
    if model is not None:
        logger.info("Supi v1 API: shutting down, releasing model.")
        del model
        model = None
        if torch.cuda.is_available():
            torch.cuda.empty_cache()


app = FastAPI(
    title="Supi v1 — Text-to-Speech API",
    description=(
        "Supi v1: natural multilingual speech synthesis (telephony + high-fidelity output) "
        "for commercial, multi-tenant use. Authenticate with a per-tenant X-API-Key. "
        "See GET /api-docs for the full Markdown manual, or /redoc for an alternate reference."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

# ==============================================================================
# CORS (Part E6) — closed by default; configure CORS_ALLOW_ORIGINS for browser clients.
# ==============================================================================
_cors_origins = [o.strip() for o in os.getenv("CORS_ALLOW_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
    allow_credentials=False,
)

# ==============================================================================
# Security headers (Part E8) — hardening headers on every response. A deliberately relaxed CSP keeps
# the interactive docs (/docs, /redoc) loading their assets from the jsDelivr CDN while still pinning
# everything else to same-origin and forbidding framing.
# ==============================================================================
_PUBLIC_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "img-src 'self' data: https://fastapi.tiangolo.com; "
    "font-src 'self' https://cdn.jsdelivr.net; "
    "worker-src 'self' blob:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; object-src 'none'"
)
security.install_security_headers(app, csp=_PUBLIC_CSP)


@app.middleware("http")
async def _idle_watchdog_activity(request: Request, call_next):
    """Reset the idle-shutdown clock on every request (see _idle_watchdog above)."""
    _touch_activity()
    return await call_next(request)


# NOTE: the tenant-management admin API is intentionally NOT mounted in this service. It is deployed
# as a separate Cloud Run service (console_app:console_app) so it can have its own IAM/ingress lockdown
# independent of the public TTS endpoint. See README.md.

# ==============================================================================
# Authentication (Part E1) — multi-tenant, fail-closed by default.
# ==============================================================================
# Each X-API-Key maps to a tenant (see tenancy.py). Configure tenants via API_KEYS (JSON) or the
# legacy single API_KEY. With REQUIRE_AUTH=true (default) the server refuses to serve protected
# routes until at least one key is configured, so it can never be accidentally exposed wide open.
# Set REQUIRE_AUTH=false to explicitly run without auth (local dev only).
API_KEY_NAME = "X-API-Key"
REQUIRE_AUTH = os.getenv("REQUIRE_AUTH", "true").lower() in ("1", "true", "yes")
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)


async def get_principal(api_key: Optional[str] = Security(api_key_header)) -> Optional[tenancy.Tenant]:
    """Resolve the X-API-Key to a Tenant. Returns None only when auth is explicitly disabled."""
    if not tenancy.auth_configured():
        if REQUIRE_AUTH:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Server authentication is not configured (no API keys set).",
            )
        return None  # auth explicitly disabled for local dev
    tenant = tenancy.authenticate(api_key)
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API Key.",
        )
    return tenant


# ==============================================================================
# Rate limiting (Part E2) — keyed by API key, falling back to client IP.
# Optional dependency: if slowapi is unavailable the decorator becomes a no-op.
# ==============================================================================
RATE_LIMIT = os.getenv("RATE_LIMIT", "60/minute")
try:
    from slowapi import Limiter, _rate_limit_exceeded_handler
    from slowapi.util import get_remote_address
    from slowapi.errors import RateLimitExceeded

    def _rate_limit_key(request: Request) -> str:
        # Bucket by tenant so each customer gets an isolated rate-limit allowance; multiple keys
        # for the same tenant share one bucket. Unauthenticated traffic falls back to client IP.
        raw_key = request.headers.get(API_KEY_NAME)
        tenant_id = tenancy.tenant_id_for_key(raw_key)
        if tenant_id:
            return f"tenant:{tenant_id}"
        return get_remote_address(request)

    limiter = Limiter(key_func=_rate_limit_key, default_limits=[RATE_LIMIT])
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    rate_limit = limiter.limit(RATE_LIMIT)
    logger.info("Rate limiting enabled (%s).", RATE_LIMIT)
except Exception as e:  # slowapi not installed
    logger.warning("slowapi unavailable (%s); rate limiting disabled.", e)

    def rate_limit(func):
        return func


# ==============================================================================
# Voice sweep / A-B testing (operator-only) — see POST /admin/sweep below.
# ==============================================================================
# A grid-search harness for tuning generation params by ear: one fixed text is synthesised across the
# Cartesian product of several parameter axes so an operator can compare cells and copy the winner.
# It is opt-in (ENABLE_SWEEP) so production pods never expose it by default, reuses the admin key for
# auth, and is unmetered (an operator path that charges no credits). Cells are generated as HD WAV so
# the browser can A/B them and so each clip's duration (hence RTF) is readable from the WAV header.
ENABLE_SWEEP = os.getenv("ENABLE_SWEEP", "false").lower() in ("1", "true", "yes")
MAX_SWEEP_CELLS = int(os.getenv("MAX_SWEEP_CELLS", "24"))
MAX_SWEEP_TEXT_CHARS = int(os.getenv("MAX_SWEEP_TEXT_CHARS", "600"))
_SWEEP_OUTPUT_FORMAT = "hd"  # 24kHz WAV PCM16: lossless for A/B listening + WAV-header-readable duration.


# ==============================================================================
# Request model with validation (Part E3)
# ==============================================================================
# A voice_profile_id is a cache key that may be a full reference-audio URL (profiles cloned via
# ref_audio_url without an explicit id are keyed by that URL — see tts_core.get_or_create_voice_prompt),
# so the cap must accommodate URL lengths rather than a short handle.
VOICE_PROFILE_ID_MAX_LEN = int(os.getenv("VOICE_PROFILE_ID_MAX_LEN", "2048"))


class TTSRequest(BaseModel):
    action: str = Field(..., description="'generate' or 'bulk_generate'")
    text: Optional[str] = Field(None, max_length=tts_core.MAX_TEXT_CHARS,
                                description="Text to synthesize (required for 'generate').")
    ref_audio_url: Optional[str] = Field(None, description="HTTPS URL to reference audio for cloning.")
    ref_text: Optional[str] = Field(None, max_length=tts_core.MAX_TEXT_CHARS,
                                    description="Optional transcript of the reference audio.")
    voice_profile_id: Optional[str] = Field(None, max_length=VOICE_PROFILE_ID_MAX_LEN,
                                            description="Voice profile cache key (may be a reference URL).")
    template: Optional[str] = Field(None, max_length=tts_core.MAX_TEXT_CHARS,
                                    description="Template string for bulk generation.")
    data: Optional[List[Dict[str, Any]]] = Field(
        None, max_length=tts_core.MAX_BULK_ITEMS,
        description=f"Bulk variables (max {tts_core.MAX_BULK_ITEMS} items).")
    num_step: Optional[int] = Field(None, ge=tts_core.NUM_STEP_MIN, le=tts_core.NUM_STEP_MAX,
                                    description="Inference/flow steps (1-128).")
    numstep: Optional[int] = Field(None, ge=tts_core.NUM_STEP_MIN, le=tts_core.NUM_STEP_MAX,
                                   description="Alias for num_step.")
    speed: Optional[float] = Field(None, ge=tts_core.SPEED_MIN, le=tts_core.SPEED_MAX,
                                   description="Speaking-rate multiplier (0.25-4.0).")
    quality: Optional[str] = Field(None, description="Preset: speed/standard/high/ultra/max.")
    guidance_scale: Optional[float] = Field(None, ge=tts_core.GUIDANCE_MIN, le=tts_core.GUIDANCE_MAX,
                                            description="Classifier-Free Guidance scale.")
    temperature: Optional[float] = Field(None, ge=0.0, le=5.0, description="Unified temperature control.")
    position_temperature: Optional[float] = Field(None, ge=0.0, le=100.0)
    class_temperature: Optional[float] = Field(None, ge=0.0, le=5.0)
    instruct: Optional[str] = Field(None, max_length=256,
                                    description="Style/emotion tag (e.g. 'sad', 'british accent').")
    seed: Optional[int] = Field(None, ge=0, le=2**31 - 1, description="Seed for reproducible output.")
    output_format: Optional[str] = Field(
        tts_core.DEFAULT_OUTPUT_FORMAT,
        description="Output: telephony (8kHz µ-law) | hd | hd_flac | hd_opus | hd_mp3.")


class CreditRequest(BaseModel):
    """A tenant's request for a credit top-up, pending operator approval."""
    amount: float = Field(..., gt=0, le=1_000_000_000,
                          description="Credits requested.")
    note: Optional[str] = Field("", max_length=512,
                                description="Optional message to the operator (reason, urgency, etc.).")


# Sweep axis item types — each list value is bounded by the same limits as the single-shot /tts knobs,
# so a cell can never request a parameter the normal API would reject (422 on any out-of-range value).
_SweepStep = Annotated[int, Field(ge=tts_core.NUM_STEP_MIN, le=tts_core.NUM_STEP_MAX)]
_SweepGuidance = Annotated[float, Field(ge=tts_core.GUIDANCE_MIN, le=tts_core.GUIDANCE_MAX)]
_SweepClassTemp = Annotated[float, Field(ge=0.0, le=5.0)]
_SweepPosTemp = Annotated[float, Field(ge=0.0, le=100.0)]
_SweepSpeed = Annotated[float, Field(ge=tts_core.SPEED_MIN, le=tts_core.SPEED_MAX)]


class SweepRequest(BaseModel):
    """One fixed text rendered across a grid of parameter axes (operator A/B testing)."""
    text: str = Field(..., min_length=1, max_length=MAX_SWEEP_TEXT_CHARS,
                      description="Fixed text synthesised in every cell (kept short for fast iteration).")
    voice_profile_id: Optional[str] = Field(None, max_length=VOICE_PROFILE_ID_MAX_LEN,
                                            description="Voice to clone once and reuse across all cells.")
    seed: Optional[int] = Field(None, ge=0, le=2**31 - 1,
                                description="Fixed seed so cells differ only by their swept params.")
    instruct: Optional[str] = Field(None, max_length=256, description="Optional style/emotion tag.")
    num_step: List[_SweepStep] = Field(default_factory=lambda: [32], min_length=1)
    guidance_scale: List[_SweepGuidance] = Field(default_factory=lambda: [2.0], min_length=1)
    class_temperature: List[_SweepClassTemp] = Field(default_factory=lambda: [0.25], min_length=1)
    position_temperature: List[_SweepPosTemp] = Field(default_factory=lambda: [5.0], min_length=1)
    speed: List[_SweepSpeed] = Field(default_factory=lambda: [1.0], min_length=1)


# ==============================================================================
# Error translation
# ==============================================================================
def _raise_http(e: Exception):
    if isinstance(e, TTSInputError):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))
    if isinstance(e, TTSServerError):
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
    raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


def _wav_duration_ms(audio_b64: str) -> Optional[float]:
    """Audio length (ms) read from a base64 WAV header; None if it can't be parsed (e.g. mocked b64)."""
    try:
        with wave.open(io.BytesIO(base64.b64decode(audio_b64)), "rb") as w:
            rate = w.getframerate()
            return (w.getnframes() / rate * 1000.0) if rate else None
    except Exception:
        return None


def _attach_credit_feedback(response: Dict[str, Any], remaining: float) -> None:
    """Report the post-charge balance and, when low/exhausted, a heads-up the client can show."""
    response["credits_remaining"] = remaining
    warning = credits.balance_warning(remaining)
    if warning:
        response["warning"] = warning


def _insufficient_credits_http(e: "credits.InsufficientCreditsError") -> HTTPException:
    """Translate an out-of-credits error into a structured, actionable HTTP 402 body."""
    return HTTPException(
        status_code=status.HTTP_402_PAYMENT_REQUIRED,
        detail={
            "error": "insufficient_credits",
            "message": e.message,
            "credits_required": e.required,
            "credits_remaining": e.remaining,
            "unit": e.unit,
            "hint": "Submit POST /credits/request to ask the operator for more credits, then retry.",
        },
    )


# ==============================================================================
# Static docs content (served verbatim at GET /api-docs so clients can `curl` the manual)
# ==============================================================================
_DOCS_PATH = Path(__file__).with_name("api_documentation.md")


def _load_docs() -> str:
    try:
        return _DOCS_PATH.read_text(encoding="utf-8")
    except OSError:
        return "# Supi v1 — Text-to-Speech API\n\nDocumentation file unavailable. See GET /docs for the interactive reference."


# ==============================================================================
# Routes
# ==============================================================================
@app.get("/", status_code=status.HTTP_200_OK)
async def index():
    """Service index — points clients at every documentation surface (no auth required)."""
    return {
        "service": "Supi v1 — Text-to-Speech API",
        "version": app.version,
        "status": "healthy" if model is not None else "unhealthy",
        "docs": {
            "interactive_swagger": "/docs",
            "redoc": "/redoc",
            "openapi_schema": "/openapi.json",
            "markdown_manual": "/api-docs",
        },
        "endpoints": {
            "health": "GET /health",
            "generate": "POST /tts",
            "credits": "GET /credits",
            "request_credits": "POST /credits/request",
            "voices": "GET /voices",
        },
        "admin_console": "deployed as a separate Cloud Run service (console_app:console_app); not exposed here",
    }


@app.get("/api-docs", response_class=PlainTextResponse, status_code=status.HTTP_200_OK)
async def api_docs():
    """Return the full Markdown manual verbatim so it can be fetched with a single `curl`."""
    return PlainTextResponse(content=_load_docs(), media_type="text/markdown; charset=utf-8")


@app.get("/health", status_code=status.HTTP_200_OK)
async def health_check():
    """Liveness probe (no internal details disclosed — Part E5)."""
    return {"status": "healthy" if model is not None else "unhealthy"}


@app.get("/credits", status_code=status.HTTP_200_OK)
async def get_credits(tenant: Optional[tenancy.Tenant] = Security(get_principal)):
    """Report the calling tenant's remaining credit balance and usage.

    Authenticated with the same `X-API-Key` as `/tts`. When credit metering is disabled
    (the default) this reports unlimited usage.
    """
    tenant_id = tenant.tenant_id if tenant else None
    grant = tenant.credits if tenant else None
    metered = tenant.metered if tenant else True
    return credits.get_balance(tenant_id, grant, metered)


@app.post("/credits/request", status_code=status.HTTP_201_CREATED)
@rate_limit
async def request_credits(request: Request, payload: CreditRequest,
                          tenant: Optional[tenancy.Tenant] = Security(get_principal)):
    """Ask the operator for a credit top-up. The request stays pending until an operator
    approves or rejects it from the admin console.

    Authenticated with the same `X-API-Key` as `/tts`. Requires the server to be running with a
    persistent store (TENANT_DB_PATH); otherwise returns 503, since pending requests must survive.
    """
    tenant_id = tenant.tenant_id if tenant else None
    try:
        req = credits.request_credits(tenant_id, payload.amount, payload.note or "")
    except AdminNotSupported as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))
    except TenantNotFound as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    return {
        "status": "pending",
        "message": "Credit request submitted; an operator will review it.",
        "request": req,
    }


@app.get("/credits/requests", status_code=status.HTTP_200_OK)
async def list_credit_requests(tenant: Optional[tenancy.Tenant] = Security(get_principal)):
    """List the calling tenant's own credit requests and their status (pending/approved/rejected)."""
    tenant_id = tenant.tenant_id if tenant else None
    try:
        return credits.list_my_credit_requests(tenant_id)
    except AdminNotSupported as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))


@app.get("/voices", status_code=status.HTTP_200_OK)
async def list_voices(tenant: Optional[tenancy.Tenant] = Security(get_principal)):
    """List the voice profiles the calling tenant may use, to pick a `voice_profile_id` for `/tts`.

    Returns, for this tenant: built-in **default** voices, any profile **published** to all tenants,
    profiles the tenant has **cloned itself**, and profiles an operator has **assigned** to it. Each
    item's `voice_profile_id` is what you pass to `POST /tts`; `relation` says why it is available
    (default/public/owner/shared) and `ready` is true when the voice is cloned and usable right now.

    Authenticated with the same `X-API-Key` as `/tts`. Voice management (publishing/assigning) is an
    operator action performed on the separate admin console.
    """
    tenant_id = tenant.tenant_id if tenant else None
    profiles = voices.list_for_tenant(tenant_id)
    return {"voices": profiles, "count": len(profiles)}


@app.post("/tts", status_code=status.HTTP_200_OK)
@rate_limit
async def tts_generation(request: Request, payload: TTSRequest,
                         tenant: Optional[tenancy.Tenant] = Security(get_principal)):
    """Generate TTS audio. Supports 'generate' (single) and 'bulk_generate' (templated batch)."""
    if model is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Model is not initialized on the server.")
    tenant_id = tenant.tenant_id if tenant else None
    grant = tenant.credits if tenant else None
    metered = tenant.metered if tenant else True  # unmetered tenants generate without being charged
    if payload.action not in ("generate", "bulk_generate"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"Unsupported action '{payload.action}'.")
    if (payload.output_format or tts_core.DEFAULT_OUTPUT_FORMAT) not in tts_core.OUTPUT_FORMATS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown output_format. Valid: {', '.join(sorted(tts_core.OUTPUT_FORMATS))}.")

    # Voice-profile access control (Part E7). Only meaningful when authenticated/multi-tenant: a
    # tenant may use a voice it owns, a default/public voice, or one assigned to it, and may only
    # (re)clone over a voice it owns. With auth disabled (no tenant) behaviour is unchanged.
    effective_ref_url = payload.ref_audio_url
    if tenant is not None and payload.voice_profile_id:
        existing = voices.get(payload.voice_profile_id)
        if existing is not None:
            if not voices.can_access(tenant_id, payload.voice_profile_id):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Voice profile '{payload.voice_profile_id}' is not available to your account.")
            if effective_ref_url and existing.get("owner_tenant_id") != tenant_id:
                # The tenant may use this shared/public voice but not overwrite it; ignore the ref.
                effective_ref_url = None
        elif not effective_ref_url:
            # An unknown profile id with no reference audio to create it from.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Voice profile '{payload.voice_profile_id}' was not found.")

    try:
        voice_clone_prompt = tts_core.get_or_create_voice_prompt(
            model, payload.voice_profile_id, effective_ref_url, payload.ref_text,
            owner_tenant_id=tenant_id,
        )
        num_step = tts_core.resolve_num_step(payload.num_step, payload.numstep, payload.quality)
        position_temperature, class_temperature = tts_core.resolve_temperatures(
            payload.temperature, payload.position_temperature, payload.class_temperature,
        )

        if payload.action == "generate":
            if not payload.text:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                    detail="Missing required parameter 'text' for action 'generate'.")
            credits.ensure_affordable(tenant_id, len(payload.text), grant, metered)
            audio_b64, sample_rate, fmt_label = tts_core.generate_audio(
                model, text=payload.text, voice_clone_prompt=voice_clone_prompt,
                num_step=num_step, speed=payload.speed, guidance_scale=payload.guidance_scale,
                position_temperature=position_temperature, class_temperature=class_temperature,
                instruct=payload.instruct, seed=payload.seed,
                output_format=payload.output_format or tts_core.DEFAULT_OUTPUT_FORMAT,
            )
            response = {
                "status": "success",
                "audio_base64": audio_b64,
                "sample_rate": sample_rate,
                "format": fmt_label,
            }
            remaining = credits.charge(tenant_id, len(payload.text), grant, metered)
            if remaining >= 0:
                _attach_credit_feedback(response, remaining)
            return response

        # bulk_generate
        if not payload.template:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail="Missing required parameter 'template' for action 'bulk_generate'.")
        if not payload.data:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail="Missing or empty parameter 'data' for action 'bulk_generate'.")
        credits.ensure_affordable(tenant_id, len(payload.template) * len(payload.data), grant, metered)
        output_format = payload.output_format or tts_core.DEFAULT_OUTPUT_FORMAT
        results = tts_core.generate_bulk(
            model, template=payload.template, data=payload.data,
            voice_clone_prompt=voice_clone_prompt, num_step=num_step, speed=payload.speed,
            guidance_scale=payload.guidance_scale, position_temperature=position_temperature,
            class_temperature=class_temperature, instruct=payload.instruct, seed=payload.seed,
            output_format=output_format,
        )
        response = {
            "status": "success",
            "results": results,
            "format": tts_core.OUTPUT_FORMATS.get(output_format, {}).get("label", output_format),
        }
        billable_chars = sum(len(r.get("text_generated", "")) for r in results)
        remaining = credits.charge(tenant_id, billable_chars, grant, metered)
        if remaining >= 0:
            _attach_credit_feedback(response, remaining)
        return response
    except HTTPException:
        raise
    except credits.InsufficientCreditsError as e:
        raise _insufficient_credits_http(e)
    except (TTSInputError, TTSServerError) as e:
        _raise_http(e)


# ==============================================================================
# Voice sweep / A-B testing (operator-only, opt-in via ENABLE_SWEEP, unmetered).
# ==============================================================================
async def admin_sweep(payload: SweepRequest):
    """Render one fixed text across a grid of parameter axes, returning audio per cell to compare.

    Guarded by the admin key (same as the tenant-management API) and only mounted when ENABLE_SWEEP
    is set, so production pods do not expose it. The voice prompt is cloned ONCE and reused for every
    cell, so cells differ only by their swept params; generation runs sequentially on the single GPU
    and per-cell failures are captured (never fatal) so one bad combo can't sink the whole sweep.
    """
    if model is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Model is not initialized on the server.")

    combos = list(itertools.product(
        payload.num_step, payload.guidance_scale, payload.class_temperature,
        payload.position_temperature, payload.speed))
    if len(combos) > MAX_SWEEP_CELLS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(f"Sweep expands to {len(combos)} cells, over the limit of {MAX_SWEEP_CELLS}. "
                    f"Narrow an axis (fewer values) and try again."))

    # Build (or fetch from cache) the voice prompt once — cloning is NOT redone per cell.
    try:
        voice_clone_prompt = tts_core.get_or_create_voice_prompt(
            model, payload.voice_profile_id, None, None, owner_tenant_id=None)
    except (TTSInputError, TTSServerError) as e:
        _raise_http(e)

    cells: List[Dict[str, Any]] = []
    for cell_id, (num_step, guidance, ctemp, ptemp, speed) in enumerate(combos):
        params = {
            "num_step": num_step, "guidance_scale": guidance, "class_temperature": ctemp,
            "position_temperature": ptemp, "speed": speed,
        }
        try:
            t0 = time.perf_counter()
            audio_b64, sample_rate, fmt_label = tts_core.generate_audio(
                model, text=payload.text, voice_clone_prompt=voice_clone_prompt,
                num_step=num_step, speed=speed, guidance_scale=guidance,
                position_temperature=ptemp, class_temperature=ctemp,
                instruct=payload.instruct, seed=payload.seed,
                output_format=_SWEEP_OUTPUT_FORMAT)
            gen_ms = (time.perf_counter() - t0) * 1000.0
            audio_ms = _wav_duration_ms(audio_b64)
            rtf = (gen_ms / audio_ms) if audio_ms else None
            cells.append({
                "cell_id": cell_id, "params": params,
                "audio_base64": audio_b64, "sample_rate": sample_rate, "format": fmt_label,
                "gen_ms": round(gen_ms, 1),
                "audio_ms": round(audio_ms, 1) if audio_ms is not None else None,
                "rtf": round(rtf, 3) if rtf is not None else None,
            })
        except Exception as e:  # one bad combo must not sink the rest of the grid
            logger.warning("Sweep cell %d failed (%s): %s", cell_id, params, e)
            cells.append({"cell_id": cell_id, "params": params, "error": str(e)})

    return {
        "base": {"text": payload.text, "voice": payload.voice_profile_id, "seed": payload.seed},
        "cells": cells,
        "count": len(cells),
    }


if ENABLE_SWEEP:
    # Mount only when enabled so the route simply does not exist (404) on production pods. Reuses the
    # admin-key auth + lockout from admin.py; unmetered because it is an operator path, not a tenant's.
    app.post("/admin/sweep", status_code=status.HTTP_200_OK,
             dependencies=[Depends(admin.require_admin)])(admin_sweep)
    logger.info("Voice sweep enabled: POST /admin/sweep (operator-only, max %d cells).", MAX_SWEEP_CELLS)


if __name__ == "__main__":
    import uvicorn
    # Cloud Run injects PORT (default 8080); falls back to 8080 for local parity.
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8080")), reload=False)
