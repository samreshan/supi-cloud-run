"""
credits.py — per-tenant usage / credit metering policy for Supi v1.

This module owns the *cost model* (per-character pricing, minimums, the on/off switch); the actual
balances live in the active [store.py] backend, keyed by tenant id, so customers are billed and
isolated independently — one tenant can never spend another's credits.

Disabled by default (fail-open): when metering is off the API behaves exactly as before and
`/credits` reports unlimited usage. Enable with `CREDITS_ENABLED=true`. Starting balances come from
(in precedence order) a per-tenant grant > the store's seed (`API_CREDITS` / admin top-ups) >
`CREDITS_DEFAULT`.

Cost model: credits are charged per character of synthesized text (`CREDITS_PER_CHAR`, default 1),
with a per-request minimum (`CREDITS_MIN_CHARGE`).
"""

import os
import logging
from typing import Optional, Dict

from store import get_store

logger = logging.getLogger("supi.credits")


class InsufficientCreditsError(Exception):
    """Raised when a request would cost more credits than the tenant has. Maps to HTTP 402.

    Carries the numbers the caller needs to build a clear, actionable 402 body: how much the
    request needed (`required`), how much is left (`remaining`), and the billing `unit`.
    """

    def __init__(self, message: str, required: Optional[float] = None,
                 remaining: Optional[float] = None, unit: str = "characters") -> None:
        super().__init__(message)
        self.message = message
        self.required = required
        self.remaining = remaining
        self.unit = unit


def _env_flag(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes")


ENABLED = _env_flag("CREDITS_ENABLED", False)
CREDITS_PER_CHAR = float(os.getenv("CREDITS_PER_CHAR", "1"))
CREDITS_MIN_CHARGE = float(os.getenv("CREDITS_MIN_CHARGE", "1"))
# Optional heads-up: warn on a successful response once a metered tenant's balance dips to/below
# this many credits (0 disables the early warning; exhaustion is always reported).
LOW_BALANCE_THRESHOLD = float(os.getenv("CREDITS_LOW_BALANCE_THRESHOLD", "0"))
UNIT = "characters"


def enabled() -> bool:
    return ENABLED


def estimate_cost(chars: int) -> float:
    """Credits a piece of text of `chars` characters will cost (used for pre-flight checks)."""
    return max(CREDITS_MIN_CHARGE, round(chars * CREDITS_PER_CHAR, 4))


def ensure_affordable(tenant_id: Optional[str], estimated_chars: int,
                      grant: Optional[float] = None, metered: bool = True) -> None:
    """Raise InsufficientCreditsError if `tenant_id` cannot cover an `estimated_chars` request.

    No-op when metering is disabled globally or when this tenant is on an unlimited plan
    (`metered=False`). Does not deduct — call `charge()` after a successful generation to record
    actual usage.
    """
    if not ENABLED or not metered:
        return
    tenant = tenant_id or "anonymous"
    cost = estimate_cost(estimated_chars)
    remaining = get_store().account_snapshot(tenant, grant)["remaining"]
    if cost > remaining:
        raise InsufficientCreditsError(
            f"Insufficient credits: this request needs ~{cost:g} credit(s) "
            f"but only {remaining:g} remain. Top up to continue.",
            required=cost, remaining=remaining, unit=UNIT,
        )


def charge(tenant_id: Optional[str], chars: int, grant: Optional[float] = None,
           metered: bool = True) -> float:
    """Deduct credits for `chars` characters actually synthesized; return the new balance.

    No-op (returns -1) when metering is disabled globally or the tenant is on an unlimited plan
    (`metered=False`). Balance is floored at 0.
    """
    if not ENABLED or not metered:
        return -1.0
    tenant = tenant_id or "anonymous"
    return get_store().deduct(tenant, estimate_cost(chars), grant)


def balance_warning(remaining: float) -> Optional[str]:
    """A heads-up to attach to a successful response, or None.

    Fires when a metered tenant has just exhausted its balance (`remaining <= 0`) or — when
    CREDITS_LOW_BALANCE_THRESHOLD is configured — dipped to/below that threshold. `remaining` is the
    value returned by `charge()`; callers should only pass real balances (>= 0), not the -1 sentinel.
    """
    if remaining <= 0:
        return ("You have used all of your credits. Submit POST /credits/request to top up "
                "and continue generating.")
    if LOW_BALANCE_THRESHOLD > 0 and remaining <= LOW_BALANCE_THRESHOLD:
        return (f"Low balance: {remaining:g} credit(s) remaining. Top up via POST /credits/request "
                "before they run out.")
    return None


def request_credits(tenant_id: Optional[str], amount: float, note: str = "") -> Dict[str, object]:
    """Record a tenant's request for a credit top-up (pending operator approval).

    Persists regardless of whether metering is enabled — the operator decides when to approve it.
    Requires a persistent store; the store raises AdminNotSupported on the ephemeral MemoryStore.
    """
    return get_store().create_credit_request(tenant_id or "anonymous", float(amount), note)


def list_my_credit_requests(tenant_id: Optional[str]) -> Dict[str, object]:
    """Return the calling tenant's own credit requests and their status."""
    tenant = tenant_id or "anonymous"
    return {"tenant_id": tenant, "requests": get_store().list_credit_requests_for_tenant(tenant)}


def get_balance(tenant_id: Optional[str], grant: Optional[float] = None,
                metered: bool = True) -> Dict[str, object]:
    """Return a JSON-serializable balance snapshot for the `/credits` endpoint."""
    if not ENABLED:
        return {
            "credits_enabled": False,
            "plan": "unlimited",
            "metered": False,
            "credits_remaining": None,
            "message": "Credit metering is disabled; usage is unlimited.",
        }
    tenant = tenant_id or "anonymous"
    if not metered:
        # Metering is on globally, but this tenant is explicitly on an unlimited plan.
        return {
            "credits_enabled": True,
            "plan": "unlimited",
            "metered": False,
            "tenant_id": tenant,
            "credits_remaining": None,
            "message": "This account is on an unlimited plan; usage is not charged.",
        }
    snap = get_store().account_snapshot(tenant, grant)
    return {
        "credits_enabled": True,
        "plan": "metered",
        "metered": True,
        "tenant_id": tenant,
        "unit": UNIT,
        "cost_per_char": CREDITS_PER_CHAR,
        "min_charge": CREDITS_MIN_CHARGE,
        "credits_granted": snap["granted"],
        "credits_used": snap["used"],
        "credits_remaining": snap["remaining"],
    }
