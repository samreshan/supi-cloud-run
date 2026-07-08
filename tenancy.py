"""
tenancy.py — multi-tenant authentication facade for the Supi v1 API.

Each API key maps to a Tenant (a paying customer). The actual key/tenant data lives in the active
[store.py] backend (env-seeded MemoryStore by default, or a durable SqliteStore when TENANT_DB_PATH
is set). Keys are matched in constant time via SHA-256 digests, so the registry never reveals which
keys exist through response timing.

Configure tenants either statically via `API_KEYS` (MemoryStore) or at runtime via the /admin API
(SqliteStore). See [store.py] for the storage details and [admin.py] for management endpoints.
"""

import logging
from typing import Optional

from store import Tenant, get_store  # re-exported as tenancy.Tenant for callers

logger = logging.getLogger("supi.tenancy")

__all__ = ["Tenant", "authenticate", "auth_configured", "any_keys_configured",
           "multi_tenant", "tenant_id_for_key"]


def authenticate(raw_key: Optional[str]) -> Optional[Tenant]:
    """Return the Tenant for `raw_key`, or None if the key is missing/unknown/revoked."""
    return get_store().authenticate(raw_key)


def auth_configured() -> bool:
    """True if an auth mechanism is configured (env keys, or a persistent store).

    Distinct from there being live keys *right now*: with a persistent store this stays True even
    when every key is revoked, so a bad key gets 401 (invalid) rather than 503 (not configured).
    """
    return get_store().auth_configured()


def any_keys_configured() -> bool:
    """True if at least one (non-revoked) API key currently exists."""
    return get_store().any_keys_configured()


def multi_tenant() -> bool:
    """True if more than one distinct tenant is configured."""
    return get_store().distinct_tenant_count() > 1


def tenant_id_for_key(raw_key: Optional[str]) -> Optional[str]:
    """Resolve a raw key to its tenant id (used for per-tenant rate-limit bucketing)."""
    tenant = authenticate(raw_key)
    return tenant.tenant_id if tenant else None
