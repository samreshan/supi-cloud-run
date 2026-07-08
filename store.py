"""
store.py — pluggable persistence for Supi tenants, API keys, and credit balances.

This is the single source of truth that [tenancy.py] (auth) and [credits.py] (metering) delegate to.
Two backends are provided:

  * MemoryStore  — the default. Reproduces the original env-var behaviour: tenants/keys come from
                   API_KEYS (or legacy API_KEY) and balances from API_CREDITS / CREDITS_DEFAULT.
                   Ephemeral: resets on restart, no runtime CRUD.
  * SqliteStore  — durable, file-backed store enabling runtime tenant management (no restart) and
                   the /admin API. API keys are stored only as SHA-256 hashes (never plaintext).
                   Enabled by setting TENANT_DB_PATH (e.g. /runpod-volume/supi.db).

Selection happens once via get_store(): TENANT_DB_PATH set -> SqliteStore, else MemoryStore. The
Sqlite backend is the integration point a future console.supi.cc would build on (swap for Postgres
by reimplementing this same surface).
"""

import os
import json
import hmac
import time
import sqlite3
import secrets
import hashlib
import logging
import threading
from dataclasses import dataclass
from typing import Optional, List, Dict, Any

logger = logging.getLogger("supi.store")

ANONYMOUS_KEY = "anonymous"
DEFAULT_KEY_PREFIX = "sk_live_"


@dataclass(frozen=True)
class Tenant:
    """An authenticated API consumer."""
    tenant_id: str
    name: str = ""
    credits: Optional[float] = None  # memory-mode starting grant; None in DB mode (balance is in DB)
    metered: bool = True  # when False this tenant is on an unlimited plan — usage is never charged


class StoreError(Exception):
    """Base class for store errors."""


class TenantExists(StoreError):
    """Raised when creating a tenant that already exists (maps to 409)."""


class TenantNotFound(StoreError):
    """Raised when a tenant/key is not found (maps to 404)."""


class CreditRequestNotFound(StoreError):
    """Raised when a credit request id is not found (maps to 404)."""


class CreditRequestNotPending(StoreError):
    """Raised when resolving a credit request that is already approved/rejected (maps to 409)."""


class AdminNotSupported(StoreError):
    """Raised when admin CRUD is attempted on a non-persistent store (maps to 503)."""


# ==============================================================================
# Key helpers (shared by both backends)
# ==============================================================================
def generate_api_key(prefix: str = DEFAULT_KEY_PREFIX) -> str:
    """Mint a fresh, URL-safe 256-bit API key with the given prefix."""
    return f"{prefix}{secrets.token_urlsafe(32)}"


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def key_display_prefix(raw_key: str) -> str:
    """A short, non-secret label for a key, e.g. 'sk_live_abcd…' (for dashboards)."""
    return raw_key[:12] + "…"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ==============================================================================
# MemoryStore — env-seeded, ephemeral (default; preserves original behaviour)
# ==============================================================================
class MemoryStore:
    supports_admin = False
    backend = "memory"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_digest: Dict[str, Tenant] = {}
        self._balances: Dict[str, float] = {}
        self._granted: Dict[str, float] = {}
        self._used: Dict[str, float] = {}
        self._credits_default = os.getenv("CREDITS_DEFAULT")
        self._load_keys_from_env()
        self._load_balances_from_env()

    # --- seeding ---
    def _load_keys_from_env(self) -> None:
        raw = os.getenv("API_KEYS")
        if raw:
            try:
                parsed = json.loads(raw)
            except Exception as e:
                logger.error("Failed to parse API_KEYS (%s); no multi-tenant keys loaded.", e)
                parsed = {}
            for key, value in parsed.items():
                if isinstance(value, str):
                    tenant = Tenant(tenant_id=value)
                elif isinstance(value, dict):
                    tenant = Tenant(
                        tenant_id=str(value.get("tenant_id") or value.get("tenant") or key),
                        name=str(value.get("name", "")),
                        credits=(float(value["credits"]) if value.get("credits") is not None else None),
                        metered=bool(value.get("metered", True)),
                    )
                else:
                    continue
                self._by_digest[hash_key(key)] = tenant
            if self._by_digest:
                logger.info("Loaded %d multi-tenant API key(s) from API_KEYS.", len(self._by_digest))
                return
        legacy = os.getenv("API_KEY")
        if legacy:
            tenant_id = os.getenv("API_TENANT", "default")
            self._by_digest[hash_key(legacy)] = Tenant(tenant_id=tenant_id, name=tenant_id)
            logger.info("Loaded legacy single API_KEY as tenant '%s'.", tenant_id)

    def _load_balances_from_env(self) -> None:
        raw = os.getenv("API_CREDITS")
        if not raw:
            return
        try:
            for tenant_id, balance in json.loads(raw).items():
                self._balances[tenant_id] = float(balance)
                self._granted[tenant_id] = float(balance)
                self._used[tenant_id] = 0.0
        except Exception as e:
            logger.error("Failed to parse API_CREDITS (%s); ignoring.", e)

    def _ensure_account(self, tenant: str, grant: Optional[float]) -> None:
        if tenant in self._balances:
            return
        if grant is not None:
            initial = float(grant)
        elif self._credits_default is not None:
            initial = float(self._credits_default)
        else:
            initial = 0.0
        self._balances[tenant] = initial
        self._granted[tenant] = initial
        self._used[tenant] = 0.0

    # --- auth surface ---
    def authenticate(self, raw_key: Optional[str]) -> Optional[Tenant]:
        if not raw_key:
            return None
        return self._by_digest.get(hash_key(raw_key))

    def any_keys_configured(self) -> bool:
        return len(self._by_digest) > 0

    def auth_configured(self) -> bool:
        # Memory auth exists only if keys were supplied via env.
        return len(self._by_digest) > 0

    def distinct_tenant_count(self) -> int:
        return len({t.tenant_id for t in self._by_digest.values()})

    # --- credits surface ---
    def account_snapshot(self, tenant_id: str, grant: Optional[float]) -> Dict[str, float]:
        with self._lock:
            self._ensure_account(tenant_id, grant)
            return {
                "granted": self._granted.get(tenant_id, 0.0),
                "used": self._used.get(tenant_id, 0.0),
                "remaining": self._balances[tenant_id],
            }

    def deduct(self, tenant_id: str, cost: float, grant: Optional[float]) -> float:
        with self._lock:
            self._ensure_account(tenant_id, grant)
            actual = min(cost, self._balances[tenant_id])
            self._balances[tenant_id] = round(self._balances[tenant_id] - actual, 4)
            self._used[tenant_id] = round(self._used.get(tenant_id, 0.0) + actual, 4)
            return self._balances[tenant_id]

    # --- admin surface (unsupported here) ---
    def _no_admin(self, *_a, **_k):
        raise AdminNotSupported(
            "Tenant management requires a persistent store. Set TENANT_DB_PATH to enable the admin API."
        )

    create_tenant = list_tenants = get_tenant = add_credits = set_metered = _no_admin
    create_key = list_keys = revoke_key = delete_tenant = _no_admin
    create_credit_request = get_credit_request = resolve_credit_request = _no_admin
    list_credit_requests = list_credit_requests_for_tenant = _no_admin


# ==============================================================================
# SqliteStore — durable, runtime-managed, admin-enabled
# ==============================================================================
_SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    tenant_id         TEXT PRIMARY KEY,
    name              TEXT NOT NULL DEFAULT '',
    credits_remaining REAL NOT NULL DEFAULT 0,
    credits_granted   REAL NOT NULL DEFAULT 0,
    credits_used      REAL NOT NULL DEFAULT 0,
    metered           INTEGER NOT NULL DEFAULT 1,   -- 0 = unlimited plan (never charged)
    created_at        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS api_keys (
    key_id     TEXT PRIMARY KEY,
    key_hash   TEXT NOT NULL UNIQUE,
    key_prefix TEXT NOT NULL,
    tenant_id  TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked    INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash);
CREATE INDEX IF NOT EXISTS idx_api_keys_tenant ON api_keys(tenant_id);
CREATE TABLE IF NOT EXISTS credit_requests (
    request_id    TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    amount        REAL NOT NULL,
    note          TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | rejected
    created_at    TEXT NOT NULL,
    resolved_at   TEXT,
    resolved_note TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_credit_requests_tenant ON credit_requests(tenant_id);
CREATE INDEX IF NOT EXISTS idx_credit_requests_status ON credit_requests(status);
"""


class SqliteStore:
    supports_admin = True
    backend = "sqlite"

    def __init__(self, path: str) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.commit()
        logger.info("SqliteStore ready at %s.", path)

    def _migrate(self) -> None:
        """Add columns introduced after the original schema so existing DBs keep working.

        Called under self._lock. SQLite has no `ADD COLUMN IF NOT EXISTS`, so we probe first.
        """
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(tenants)").fetchall()}
        if "metered" not in cols:
            self._conn.execute("ALTER TABLE tenants ADD COLUMN metered INTEGER NOT NULL DEFAULT 1")
            logger.info("SqliteStore: migrated tenants table (added 'metered' column).")

    # --- auth surface ---
    def authenticate(self, raw_key: Optional[str]) -> Optional[Tenant]:
        if not raw_key:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT t.tenant_id, t.name, t.metered FROM api_keys k "
                "JOIN tenants t ON t.tenant_id = k.tenant_id "
                "WHERE k.key_hash = ? AND k.revoked = 0",
                (hash_key(raw_key),),
            ).fetchone()
        return Tenant(tenant_id=row["tenant_id"], name=row["name"],
                      metered=bool(row["metered"])) if row else None

    def any_keys_configured(self) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS c FROM api_keys WHERE revoked = 0").fetchone()
        return row["c"] > 0

    def auth_configured(self) -> bool:
        # A persistent store is itself the auth mechanism: a present-but-empty DB still means
        # "auth is configured" — unknown/revoked keys get 401, not the "not configured" 503.
        return True

    def distinct_tenant_count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS c FROM tenants").fetchone()
        return row["c"]

    # --- credits surface ---
    def account_snapshot(self, tenant_id: str, grant: Optional[float] = None) -> Dict[str, float]:
        with self._lock:
            row = self._conn.execute(
                "SELECT credits_granted, credits_used, credits_remaining FROM tenants WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchone()
        if not row:
            return {"granted": 0.0, "used": 0.0, "remaining": 0.0}
        return {"granted": row["credits_granted"], "used": row["credits_used"],
                "remaining": row["credits_remaining"]}

    def deduct(self, tenant_id: str, cost: float, grant: Optional[float] = None) -> float:
        with self._lock:
            row = self._conn.execute(
                "SELECT credits_remaining, credits_used FROM tenants WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchone()
            if not row:
                return 0.0
            actual = min(cost, row["credits_remaining"])
            new_remaining = round(row["credits_remaining"] - actual, 4)
            new_used = round(row["credits_used"] + actual, 4)
            self._conn.execute(
                "UPDATE tenants SET credits_remaining = ?, credits_used = ? WHERE tenant_id = ?",
                (new_remaining, new_used, tenant_id),
            )
            self._conn.commit()
            return new_remaining

    # --- admin surface ---
    def create_tenant(self, tenant_id: str, name: str = "", credits: float = 0.0) -> Dict[str, Any]:
        with self._lock:
            exists = self._conn.execute(
                "SELECT 1 FROM tenants WHERE tenant_id = ?", (tenant_id,)).fetchone()
            if exists:
                raise TenantExists(f"Tenant '{tenant_id}' already exists.")
            self._conn.execute(
                "INSERT INTO tenants (tenant_id, name, credits_remaining, credits_granted, "
                "credits_used, created_at) VALUES (?, ?, ?, ?, 0, ?)",
                (tenant_id, name, float(credits), float(credits), _now()),
            )
            self._conn.commit()
        return self.get_tenant(tenant_id)

    def add_credits(self, tenant_id: str, amount: float) -> Dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM tenants WHERE tenant_id = ?", (tenant_id,)).fetchone()
            if not row:
                raise TenantNotFound(f"Tenant '{tenant_id}' not found.")
            self._conn.execute(
                "UPDATE tenants SET credits_remaining = credits_remaining + ?, "
                "credits_granted = credits_granted + ? WHERE tenant_id = ?",
                (float(amount), float(amount), tenant_id),
            )
            self._conn.commit()
        return self.get_tenant(tenant_id)

    def set_metered(self, tenant_id: str, metered: bool) -> Dict[str, Any]:
        """Switch a tenant between the metered plan (charged per request) and an unlimited plan.

        When `metered` is False the tenant generates without ever spending credits; its balance is
        left untouched so it is restored exactly if metering is turned back on.
        """
        with self._lock:
            cur = self._conn.execute(
                "UPDATE tenants SET metered = ? WHERE tenant_id = ?",
                (1 if metered else 0, tenant_id),
            )
            self._conn.commit()
            if cur.rowcount == 0:
                raise TenantNotFound(f"Tenant '{tenant_id}' not found.")
        return self.get_tenant(tenant_id)

    def get_tenant(self, tenant_id: str) -> Dict[str, Any]:
        with self._lock:
            t = self._conn.execute(
                "SELECT tenant_id, name, credits_granted, credits_used, credits_remaining, metered, "
                "created_at FROM tenants WHERE tenant_id = ?", (tenant_id,)).fetchone()
            if not t:
                raise TenantNotFound(f"Tenant '{tenant_id}' not found.")
            keys = self._conn.execute(
                "SELECT key_id, key_prefix, created_at, revoked FROM api_keys "
                "WHERE tenant_id = ? ORDER BY created_at", (tenant_id,)).fetchall()
        return {
            "tenant_id": t["tenant_id"], "name": t["name"],
            "credits_granted": t["credits_granted"], "credits_used": t["credits_used"],
            "credits_remaining": t["credits_remaining"], "metered": bool(t["metered"]),
            "created_at": t["created_at"],
            "keys": [dict(k) for k in keys],
        }

    def list_tenants(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT t.tenant_id, t.name, t.credits_remaining, t.credits_granted, t.credits_used, "
                "t.metered, t.created_at, COUNT(CASE WHEN k.revoked = 0 THEN 1 END) AS active_keys "
                "FROM tenants t LEFT JOIN api_keys k ON k.tenant_id = t.tenant_id "
                "GROUP BY t.tenant_id ORDER BY t.created_at").fetchall()
        return [{**dict(r), "metered": bool(r["metered"])} for r in rows]

    def create_key(self, tenant_id: str, prefix: str = DEFAULT_KEY_PREFIX) -> Dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM tenants WHERE tenant_id = ?", (tenant_id,)).fetchone()
            if not row:
                raise TenantNotFound(f"Tenant '{tenant_id}' not found.")
            raw = generate_api_key(prefix)
            key_id = "key_" + secrets.token_hex(8)
            self._conn.execute(
                "INSERT INTO api_keys (key_id, key_hash, key_prefix, tenant_id, created_at, revoked) "
                "VALUES (?, ?, ?, ?, ?, 0)",
                (key_id, hash_key(raw), key_display_prefix(raw), tenant_id, _now()),
            )
            self._conn.commit()
        # Raw key is returned exactly once; only its hash is persisted.
        return {"api_key": raw, "key_id": key_id, "key_prefix": key_display_prefix(raw),
                "tenant_id": tenant_id}

    def list_keys(self, tenant_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT key_id, key_prefix, created_at, revoked FROM api_keys "
                "WHERE tenant_id = ? ORDER BY created_at", (tenant_id,)).fetchall()
        return [dict(r) for r in rows]

    def revoke_key(self, key_id: str) -> None:
        with self._lock:
            cur = self._conn.execute("UPDATE api_keys SET revoked = 1 WHERE key_id = ?", (key_id,))
            self._conn.commit()
            if cur.rowcount == 0:
                raise TenantNotFound(f"Key '{key_id}' not found.")

    def delete_tenant(self, tenant_id: str) -> None:
        with self._lock:
            cur = self._conn.execute("DELETE FROM tenants WHERE tenant_id = ?", (tenant_id,))
            self._conn.commit()
            if cur.rowcount == 0:
                raise TenantNotFound(f"Tenant '{tenant_id}' not found.")

    # --- credit requests: tenants ask for a top-up, an operator approves/rejects ---
    def create_credit_request(self, tenant_id: str, amount: float, note: str = "") -> Dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM tenants WHERE tenant_id = ?", (tenant_id,)).fetchone()
            if not row:
                raise TenantNotFound(f"Tenant '{tenant_id}' not found.")
            request_id = "creq_" + secrets.token_hex(8)
            self._conn.execute(
                "INSERT INTO credit_requests (request_id, tenant_id, amount, note, status, created_at) "
                "VALUES (?, ?, ?, ?, 'pending', ?)",
                (request_id, tenant_id, float(amount), note or "", _now()),
            )
            self._conn.commit()
        return self.get_credit_request(request_id)

    def get_credit_request(self, request_id: str) -> Dict[str, Any]:
        with self._lock:
            r = self._conn.execute(
                "SELECT cr.request_id, cr.tenant_id, t.name AS tenant_name, cr.amount, cr.note, "
                "cr.status, cr.created_at, cr.resolved_at, cr.resolved_note "
                "FROM credit_requests cr LEFT JOIN tenants t ON t.tenant_id = cr.tenant_id "
                "WHERE cr.request_id = ?", (request_id,)).fetchone()
        if not r:
            raise CreditRequestNotFound(f"Credit request '{request_id}' not found.")
        return dict(r)

    def list_credit_requests(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """All credit requests (admin view), newest first; optionally filtered by status."""
        query = (
            "SELECT cr.request_id, cr.tenant_id, t.name AS tenant_name, cr.amount, cr.note, "
            "cr.status, cr.created_at, cr.resolved_at, cr.resolved_note "
            "FROM credit_requests cr LEFT JOIN tenants t ON t.tenant_id = cr.tenant_id"
        )
        params: tuple = ()
        if status:
            query += " WHERE cr.status = ?"
            params = (status,)
        query += " ORDER BY cr.created_at DESC"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def list_credit_requests_for_tenant(self, tenant_id: str) -> List[Dict[str, Any]]:
        """A single tenant's own credit requests (newest first)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT request_id, tenant_id, amount, note, status, created_at, resolved_at, "
                "resolved_note FROM credit_requests WHERE tenant_id = ? ORDER BY created_at DESC",
                (tenant_id,)).fetchall()
        return [dict(r) for r in rows]

    def resolve_credit_request(self, request_id: str, approve: bool, note: str = "") -> Dict[str, Any]:
        """Approve (granting the credits) or reject a pending request — atomically.

        Only a 'pending' request can be resolved; resolving an already-decided one raises
        CreditRequestNotPending so credits are never granted twice.
        """
        with self._lock:
            req = self._conn.execute(
                "SELECT tenant_id, amount, status FROM credit_requests WHERE request_id = ?",
                (request_id,)).fetchone()
            if not req:
                raise CreditRequestNotFound(f"Credit request '{request_id}' not found.")
            if req["status"] != "pending":
                raise CreditRequestNotPending(
                    f"Credit request '{request_id}' is already {req['status']}.")
            if approve:
                # Grant the credits in the same transaction as marking the request approved.
                self._conn.execute(
                    "UPDATE tenants SET credits_remaining = credits_remaining + ?, "
                    "credits_granted = credits_granted + ? WHERE tenant_id = ?",
                    (req["amount"], req["amount"], req["tenant_id"]),
                )
            self._conn.execute(
                "UPDATE credit_requests SET status = ?, resolved_at = ?, resolved_note = ? "
                "WHERE request_id = ?",
                ("approved" if approve else "rejected", _now(), note or "", request_id),
            )
            self._conn.commit()
        return self.get_credit_request(request_id)


# ==============================================================================
# Singleton selection
# ==============================================================================
_store = None
_store_lock = threading.Lock()


def get_store():
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                path = os.getenv("TENANT_DB_PATH")
                _store = SqliteStore(path) if path else MemoryStore()
    return _store


def reset_store_for_tests() -> None:
    """Drop the cached store so the next get_store() re-reads the environment (tests only)."""
    global _store
    with _store_lock:
        _store = None
