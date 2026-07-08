"""
security.py — shared HTTP hardening for Supi (public API + admin console).

Two reusable pieces, kept dependency-free so both FastAPI apps can opt in:

  * install_security_headers(app, ...) — adds standard hardening response headers (anti-clickjacking,
    no MIME sniffing, a tight referrer policy, optional CSP / HSTS / no-store) to every response.
  * BruteForceThrottle — a small in-process, per-client lockout used to blunt admin-key guessing.

The throttle is intentionally in-process (no Redis): the admin console runs as a single worker on its
own port, so a process-local counter is the right scope. Behind the RunPod HTTPS proxy the real client
address arrives in X-Forwarded-For, so client_ip() reads that first hop before falling back to the
socket peer.
"""

import os
import time
import logging
import threading
from typing import Dict, Optional, Tuple

from starlette.requests import Request

logger = logging.getLogger("supi.security")

# Always-on hardening headers (safe for both JSON APIs and HTML).
_BASE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",                 # legacy anti-clickjacking (CSP frame-ancestors is stronger)
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "X-Permitted-Cross-Domain-Policies": "none",
}

# HSTS is only honoured by browsers over HTTPS (ignored on plain-HTTP dev), so it is safe to send
# always; disable with HSTS_ENABLED=false if terminating TLS somewhere that should not opt in.
_HSTS_ENABLED = os.getenv("HSTS_ENABLED", "true").strip().lower() in ("1", "true", "yes")
_HSTS_VALUE = "max-age=63072000; includeSubDomains"


def install_security_headers(app, *, csp: Optional[str] = None, no_store: bool = False) -> None:
    """Attach a middleware that stamps hardening headers on every response from `app`.

    csp:      Content-Security-Policy value (None to omit — e.g. when a relaxed policy would break
              third-party docs UIs).
    no_store: when True, also send `Cache-Control: no-store` (use for authenticated operator pages).
    """
    extra = dict(_BASE_HEADERS)
    if csp:
        extra["Content-Security-Policy"] = csp
    if _HSTS_ENABLED:
        extra["Strict-Transport-Security"] = _HSTS_VALUE
    if no_store:
        extra["Cache-Control"] = "no-store"

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        response = await call_next(request)
        for name, value in extra.items():
            response.headers.setdefault(name, value)
        return response


def client_ip(request: Request) -> str:
    """Best-effort client identity for throttling: first X-Forwarded-For hop, else socket peer."""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        first = fwd.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


class BruteForceThrottle:
    """Per-client failed-attempt limiter: lock out after `max_failures` within `window_seconds`.

    Thread-safe and self-pruning. `seconds_until_unlocked()` returns the remaining lockout (0 = open),
    `record_failure()` counts a bad attempt (and may start a lockout), `record_success()` clears state.
    """

    def __init__(self, max_failures: int = 5, window_seconds: int = 300,
                 lockout_seconds: int = 300) -> None:
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self.lockout_seconds = lockout_seconds
        self._lock = threading.Lock()
        # client_id -> (failure_count, first_failure_ts, locked_until_ts)
        self._state: Dict[str, Tuple[int, float, float]] = {}

    def seconds_until_unlocked(self, client_id: str) -> int:
        now = time.time()
        with self._lock:
            entry = self._state.get(client_id)
            if not entry:
                return 0
            _, _, locked_until = entry
            return max(0, int(round(locked_until - now))) if locked_until > now else 0

    def record_failure(self, client_id: str) -> int:
        """Register a failed attempt; return seconds of lockout now in effect (0 if not yet locked)."""
        now = time.time()
        with self._lock:
            count, first_ts, locked_until = self._state.get(client_id, (0, now, 0.0))
            if locked_until > now:
                return int(round(locked_until - now))
            # Reset the rolling window if the previous one has elapsed.
            if now - first_ts > self.window_seconds:
                count, first_ts = 0, now
            count += 1
            if count >= self.max_failures:
                locked_until = now + self.lockout_seconds
                self._state[client_id] = (count, first_ts, locked_until)
                logger.warning("Admin auth locked out %s for %ss after %d failures.",
                               client_id, self.lockout_seconds, count)
                return self.lockout_seconds
            self._state[client_id] = (count, first_ts, locked_until)
            return 0

    def record_success(self, client_id: str) -> None:
        with self._lock:
            self._state.pop(client_id, None)
