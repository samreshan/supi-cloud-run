"""
admin.py — tenant management API for Supi v1 (the backend behind console.supi.cc).

These endpoints let an operator create tenants, mint/revoke API keys, and top up credits at runtime
with no pod restart. They are guarded by a separate `ADMIN_API_KEY` (header `X-Admin-Key`) — distinct
from tenant API keys — and require a persistent store (`TENANT_DB_PATH`); on the ephemeral MemoryStore
they return 503 so management state is never silently lost.

API keys are returned in plaintext exactly once, at creation. Only their SHA-256 hash is stored, so
keys cannot be read back later — if one is lost, revoke it and mint a new one.
"""

import os
import hmac
import json
import logging
import urllib.error
import urllib.request
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, status, Depends, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

import security
from store import (
    get_store, TenantExists, TenantNotFound, AdminNotSupported, DEFAULT_KEY_PREFIX,
    CreditRequestNotFound, CreditRequestNotPending,
)
import voices
from voices import VoiceProfileError, VoiceProfileNotFound

logger = logging.getLogger("supi.admin")

ADMIN_API_KEY = os.getenv("ADMIN_API_KEY")
ADMIN_KEY_NAME = "X-Admin-Key"

# Voice-sweep proxy: the console (this port) forwards POST /admin/sweep to the TTS model service, which
# owns the GPU on its own internal port. Both run in one container (see start.sh), so the default target
# is loopback. The generous timeout covers a full grid (sequential, single-GPU) of generations.
TTS_INTERNAL_URL = os.getenv("TTS_INTERNAL_URL", "http://127.0.0.1:8000")
SWEEP_PROXY_TIMEOUT = float(os.getenv("SWEEP_PROXY_TIMEOUT", "300"))

# Refuse to treat a weak operator key as safe: warn loudly at startup if it is shorter than this.
# (A minted key from `store.generate_api_key` is ~43 url-safe chars, well above this floor.)
ADMIN_MIN_KEY_LENGTH = int(os.getenv("ADMIN_MIN_KEY_LENGTH", "24"))
if ADMIN_API_KEY and len(ADMIN_API_KEY) < ADMIN_MIN_KEY_LENGTH:
    logger.warning(
        "ADMIN_API_KEY is only %d characters — shorter than the recommended %d. Use a long, random "
        "secret (e.g. `openssl rand -base64 32`) to resist guessing.",
        len(ADMIN_API_KEY), ADMIN_MIN_KEY_LENGTH,
    )

# In-process lockout to blunt admin-key brute forcing (admin console runs as a single worker).
_admin_throttle = security.BruteForceThrottle(
    max_failures=int(os.getenv("ADMIN_MAX_AUTH_FAILURES", "5")),
    window_seconds=int(os.getenv("ADMIN_AUTH_WINDOW_SECONDS", "300")),
    lockout_seconds=int(os.getenv("ADMIN_AUTH_LOCKOUT_SECONDS", "300")),
)


# ==============================================================================
# Admin authentication — separate from tenant auth, fail-closed, brute-force throttled.
# ==============================================================================
def require_admin(request: Request,
                  x_admin_key: Optional[str] = Header(None, alias=ADMIN_KEY_NAME)) -> None:
    if not ADMIN_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin API is not configured (ADMIN_API_KEY unset).",
        )
    client = security.client_ip(request)
    locked = _admin_throttle.seconds_until_unlocked(client)
    if locked:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many failed admin attempts. Try again in {locked}s.",
            headers={"Retry-After": str(locked)},
        )
    if not x_admin_key or not hmac.compare_digest(x_admin_key, ADMIN_API_KEY):
        retry = _admin_throttle.record_failure(client)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin key.",
            headers={"Retry-After": str(retry)} if retry else None,
        )
    _admin_throttle.record_success(client)


def _store():
    """Return the active store, or 503 if it is not a persistent (admin-capable) backend."""
    st = get_store()
    if not getattr(st, "supports_admin", False):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=("Tenant management requires a persistent store. Set TENANT_DB_PATH "
                    "(e.g. /runpod-volume/supi.db) and restart to enable the admin API."),
        )
    return st


# All routes require a valid admin key.
router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


# ==============================================================================
# Request bodies
# ==============================================================================
class CreateTenant(BaseModel):
    tenant_id: str = Field(..., min_length=1, max_length=128, description="Stable unique id, e.g. 'acme'.")
    name: str = Field("", max_length=256, description="Human-readable display name.")
    credits: float = Field(0, ge=0, description="Starting credit balance.")


class AddCredits(BaseModel):
    amount: float = Field(..., gt=0, description="Credits to add to the tenant's balance.")


class SetMetered(BaseModel):
    metered: bool = Field(..., description="True = charge credits per request; "
                                           "False = unlimited plan (never charged).")


class ResolveCreditRequest(BaseModel):
    note: str = Field("", max_length=512,
                      description="Optional note recorded with the decision (e.g. reason for rejection).")


class CreateKey(BaseModel):
    prefix: str = Field(DEFAULT_KEY_PREFIX, max_length=32,
                        description="Key prefix, e.g. 'sk_live_' or 'sk_test_'.")


# Voice profile ids may be arbitrary URLs (e.g. an audio link with '/' and ':'), so they are passed in
# the request body / query string — never in the URL path, where slashes would break route matching.
class VoiceRef(BaseModel):
    profile_id: str = Field(..., min_length=1, description="Voice profile id (may be a full URL).")


class SetVisibility(VoiceRef):
    visibility: str = Field(..., description="'public' (all tenants) or 'private' (owner + assigned).")


class SetDefault(VoiceRef):
    is_default: bool = Field(..., description="Mark/unmark as a built-in default voice shown to all.")


class SetPersistent(VoiceRef):
    persistent: bool = Field(..., description="Keep on the persistent drive + warm-load on restart.")


class GrantTenant(VoiceRef):
    tenant_id: str = Field(..., min_length=1, max_length=128,
                           description="Tenant to assign this voice to.")


class UpdateVoice(VoiceRef):
    name: Optional[str] = Field(None, max_length=256, description="Display name.")
    description: Optional[str] = Field(None, max_length=1024, description="Description / notes.")


# ==============================================================================
# Routes
# ==============================================================================
@router.post("/tenants", status_code=status.HTTP_201_CREATED)
async def create_tenant(body: CreateTenant):
    """Create a tenant with an optional starting credit balance."""
    try:
        return _store().create_tenant(body.tenant_id, body.name, body.credits)
    except TenantExists as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except AdminNotSupported as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))


@router.get("/tenants")
async def list_tenants():
    """List all tenants with balances and active-key counts."""
    return {"tenants": _store().list_tenants()}


@router.get("/tenants/{tenant_id}")
async def get_tenant(tenant_id: str):
    """Fetch one tenant, including its keys (ids/prefixes only — never the secret)."""
    try:
        return _store().get_tenant(tenant_id)
    except TenantNotFound as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))


@router.post("/tenants/{tenant_id}/credits")
async def add_credits(tenant_id: str, body: AddCredits):
    """Top up a tenant's credit balance."""
    try:
        return _store().add_credits(tenant_id, body.amount)
    except TenantNotFound as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))


@router.post("/tenants/{tenant_id}/metered")
async def set_metered(tenant_id: str, body: SetMetered):
    """Switch a tenant between metered billing and an unlimited (unmetered) plan.

    Unmetering lets a tenant generate TTS without ever spending credits; its balance is preserved
    untouched, so metering can be turned back on later with the same balance intact.
    """
    try:
        return _store().set_metered(tenant_id, body.metered)
    except TenantNotFound as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except AdminNotSupported as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))


# ------------------------------------------------------------------------------
# Credit requests — tenants ask for a top-up (POST /credits/request on the public API);
# the operator reviews them here and approves (grants the credits) or rejects.
# ------------------------------------------------------------------------------
@router.get("/credit-requests")
async def list_credit_requests(status: Optional[str] = None):
    """List credit requests, newest first. Filter with ?status=pending|approved|rejected."""
    if status is not None and status not in ("pending", "approved", "rejected"):
        raise HTTPException(status_code=400,
                            detail="status must be one of: pending, approved, rejected.")
    try:
        requests = _store().list_credit_requests(status)
    except AdminNotSupported as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"requests": requests}


@router.post("/credit-requests/{request_id}/approve")
async def approve_credit_request(request_id: str, body: ResolveCreditRequest = ResolveCreditRequest()):
    """Approve a pending request — grants the requested credits to the tenant atomically."""
    try:
        return _store().resolve_credit_request(request_id, approve=True, note=body.note)
    except CreditRequestNotFound as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except CreditRequestNotPending as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))


@router.post("/credit-requests/{request_id}/reject")
async def reject_credit_request(request_id: str, body: ResolveCreditRequest = ResolveCreditRequest()):
    """Reject a pending request without granting any credits."""
    try:
        return _store().resolve_credit_request(request_id, approve=False, note=body.note)
    except CreditRequestNotFound as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except CreditRequestNotPending as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))


@router.post("/tenants/{tenant_id}/keys", status_code=status.HTTP_201_CREATED)
async def create_key(tenant_id: str, body: CreateKey = CreateKey()):
    """Mint a new API key for a tenant. The plaintext key is returned ONCE — store it now."""
    try:
        result = _store().create_key(tenant_id, body.prefix)
    except TenantNotFound as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    result["warning"] = "Store this api_key now; it cannot be retrieved again."
    return result


@router.get("/tenants/{tenant_id}/keys")
async def list_keys(tenant_id: str):
    """List a tenant's keys (ids, display prefixes, revoked flag) — no secrets."""
    return {"tenant_id": tenant_id, "keys": _store().list_keys(tenant_id)}


@router.delete("/keys/{key_id}")
async def revoke_key(key_id: str):
    """Revoke (disable) a single API key by its key_id."""
    try:
        _store().revoke_key(key_id)
    except TenantNotFound as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    return {"status": "revoked", "key_id": key_id}


@router.delete("/tenants/{tenant_id}")
async def delete_tenant(tenant_id: str):
    """Delete a tenant and all of its keys (offboarding)."""
    try:
        _store().delete_tenant(tenant_id)
    except TenantNotFound as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    return {"status": "deleted", "tenant_id": tenant_id}


# ==============================================================================
# Voice profile management ([voices.py]) — see which voices have been cloned/cached and control who
# can use them: publish to all tenants, assign to specific tenants, or mark as a default voice.
#
# Unlike tenant management, this works on any tenant-store backend: the voice registry is its own
# durable SQLite index next to the cached .pt files, independent of TENANT_DB_PATH.
# ==============================================================================
def _voice_not_found(e: VoiceProfileNotFound):
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))


def _voice_bad_request(e: VoiceProfileError):
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.get("/voices")
async def list_voices():
    """List every registered voice profile with owner, visibility, defaults, grants, and readiness."""
    return {"voices": voices.list_all()}


@router.get("/voices/disk")
async def list_voice_files():
    """List the raw cached .pt files on disk, flagging any 'orphans' the registry has no metadata for."""
    return {"files": voices.list_disk()}


@router.get("/voices/lookup")
async def get_voice(profile_id: str):
    """Fetch one voice profile's full metadata (profile_id as a query param, so URLs are safe)."""
    rec = voices.get(profile_id)
    if rec is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"Voice profile '{profile_id}' not found.")
    rec["ready"] = voices.profile_ready(profile_id)
    return rec


@router.post("/voices/visibility")
async def set_voice_visibility(body: SetVisibility):
    """Publish a voice to all tenants ('public') or restrict it ('private')."""
    try:
        return voices.get_registry().set_visibility(body.profile_id, body.visibility)
    except VoiceProfileNotFound as e:
        _voice_not_found(e)
    except VoiceProfileError as e:
        _voice_bad_request(e)


@router.post("/voices/default")
async def set_voice_default(body: SetDefault):
    """Mark/unmark a voice as a built-in default shown to every tenant."""
    try:
        return voices.get_registry().set_default(body.profile_id, body.is_default)
    except VoiceProfileNotFound as e:
        _voice_not_found(e)


@router.post("/voices/persist")
async def set_voice_persistent(body: SetPersistent):
    """Choose whether to keep this voice on the persistent drive and auto-load it into cache on restart."""
    try:
        return voices.get_registry().set_persistent(body.profile_id, body.persistent)
    except VoiceProfileNotFound as e:
        _voice_not_found(e)


@router.post("/voices/grants", status_code=status.HTTP_201_CREATED)
async def grant_voice(body: GrantTenant):
    """Assign a (private) voice to a specific tenant so only they can use it."""
    try:
        return voices.get_registry().grant(body.profile_id, body.tenant_id)
    except VoiceProfileNotFound as e:
        _voice_not_found(e)


@router.delete("/voices/grants")
async def revoke_voice_grant(profile_id: str, tenant_id: str):
    """Remove a tenant's assignment to a voice (ids as query params, so URLs are safe)."""
    try:
        return voices.get_registry().revoke(profile_id, tenant_id)
    except VoiceProfileNotFound as e:
        _voice_not_found(e)


@router.patch("/voices")
async def update_voice(body: UpdateVoice):
    """Rename a voice or edit its description."""
    try:
        return voices.get_registry().update(
            body.profile_id, name=body.name, description=body.description)
    except VoiceProfileNotFound as e:
        _voice_not_found(e)


@router.delete("/voices")
async def delete_voice(profile_id: str, delete_file: bool = True):
    """Delete a voice's metadata (and, by default, its cached .pt tensor). profile_id is a query param."""
    try:
        voices.get_registry().delete(profile_id, delete_file=delete_file)
    except VoiceProfileNotFound as e:
        _voice_not_found(e)
    return {"status": "deleted", "profile_id": profile_id, "file_deleted": delete_file}


# ==============================================================================
# Voice sweep / A-B testing — same-origin proxy to the TTS model service ([app.py]'s POST /admin/sweep).
#
# The console UI calls THIS route same-origin (no CORS/CSP changes), and we forward the body to the
# model service on its internal port, attaching the admin key. The model stays isolated on port 8000;
# the operator's browser only ever talks to the console origin. The upstream is gated by ENABLE_SWEEP,
# so when sweeps are disabled this transparently forwards the upstream 404.
# ==============================================================================
def _forward_sweep(raw_body: bytes) -> bytes:
    """Blocking POST of the raw sweep body to the model service; returns its JSON bytes. Run off-loop."""
    url = f"{TTS_INTERNAL_URL.rstrip('/')}/admin/sweep"
    req = urllib.request.Request(
        url, data=raw_body, method="POST",
        headers={"Content-Type": "application/json", ADMIN_KEY_NAME: ADMIN_API_KEY or ""})
    try:
        with urllib.request.urlopen(req, timeout=SWEEP_PROXY_TIMEOUT) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            detail = json.loads(body).get("detail", body.decode("utf-8", "replace"))
        except Exception:
            detail = body.decode("utf-8", "replace") or e.reason
        raise HTTPException(status_code=e.code, detail=detail)
    except urllib.error.URLError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Voice-sweep model service ({url}) is unreachable: {e.reason}")


@router.post("/sweep")
async def proxy_sweep(request: Request):
    """Forward a sweep request to the model service and return its JSON verbatim (operator-only)."""
    raw_body = await request.body()
    result = await run_in_threadpool(_forward_sweep, raw_body)
    return Response(content=result, media_type="application/json")
