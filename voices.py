"""
voices.py — voice-profile registry & access policy for Supi v1.

The TTS engine ([tts_core.py]) caches every cloned voice as a `<sha256(profile_id)>.pt` tensor file
in VOICE_PROFILES_DIR (the L2 disk cache). Those files carry no ownership, naming, or sharing
metadata on their own — this module adds that layer so profiles can be *managed* (named, published,
or shared with specific tenants) and *listed* (each tenant only ever sees the voices it may use).

Storage: a small SQLite index kept alongside the .pt files (VOICE_PROFILE_DB_PATH, default
`<VOICE_PROFILES_DIR>/profiles.db`). It is deliberately independent of the tenant store ([store.py]):
voice metadata is durable exactly when the profile cache is durable (same volume), and the operator
console reaches this registry on the admin port *without* importing the heavy TTS stack.

Access model — a tenant T may use a profile P when ANY of:
  * P.is_default            — a built-in / recommended voice, shown to everyone
  * P.visibility == public  — the operator published it to all tenants
  * P.owner_tenant_id == T  — T cloned it (its own private voice)
  * a grant (P, T) exists   — the operator assigned it to T specifically

Tenants create profiles implicitly by cloning through `POST /tts` with a `voice_profile_id`; the new
profile is registered *private* and owned by the calling tenant. Operators then publish it or assign
it to other tenants via the admin API ([admin.py]). See [app.py] for `GET /voices` (tenant listing)
and the access check enforced on `POST /tts`.
"""

import os
import json
import time
import shutil
import sqlite3
import hashlib
import logging
import threading
from typing import Optional, List, Dict, Any

logger = logging.getLogger("supi.voices")

# ==============================================================================
# Profile cache location & filename derivation (single source of truth; tts_core imports these).
# ==============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VOICE_PROFILES_DIR = os.getenv("VOICE_PROFILES_DIR", os.path.join(BASE_DIR, "voice_profiles"))
os.makedirs(VOICE_PROFILES_DIR, exist_ok=True)

# Optional durable location — e.g. a mounted volume like /runpod-volume/voice_profiles. When set and
# distinct from the working cache dir, profiles an operator marks "persistent" are copied here so they
# survive a restart, and the registry DB defaults to living here too. When unset, the working dir is
# itself assumed durable and "persistent" is purely a warm-load/pin flag (no second copy).
VOICE_PROFILE_PERSIST_DIR = os.getenv("VOICE_PROFILE_PERSIST_DIR", "").strip() or VOICE_PROFILES_DIR
if VOICE_PROFILE_PERSIST_DIR != VOICE_PROFILES_DIR:
    os.makedirs(VOICE_PROFILE_PERSIST_DIR, exist_ok=True)

VISIBILITY_PRIVATE = "private"
VISIBILITY_PUBLIC = "public"
VALID_VISIBILITY = {VISIBILITY_PRIVATE, VISIBILITY_PUBLIC}


def two_tier() -> bool:
    """True when a separate persistent volume dir is configured (distinct from the working cache)."""
    return VOICE_PROFILE_PERSIST_DIR != VOICE_PROFILES_DIR


def _hashed_name(cache_key: str) -> str:
    return hashlib.sha256(cache_key.encode("utf-8")).hexdigest() + ".pt"


def get_profile_filepath(cache_key: str) -> str:
    """Working-cache path for a profile's .pt (where fresh clones are written)."""
    return os.path.join(VOICE_PROFILES_DIR, _hashed_name(cache_key))


def get_persist_filepath(cache_key: str) -> str:
    """Durable path for a profile's .pt on the persistent volume."""
    return os.path.join(VOICE_PROFILE_PERSIST_DIR, _hashed_name(cache_key))


def find_cached_file(cache_key: str) -> Optional[str]:
    """Path of an existing cached .pt for this key — working dir first, then the persist dir — or None."""
    working = get_profile_filepath(cache_key)
    if os.path.exists(working):
        return working
    if two_tier():
        persist = get_persist_filepath(cache_key)
        if os.path.exists(persist):
            return persist
    return None


def profile_ready(cache_key: str) -> bool:
    """True when a cloned tensor for this profile exists on disk (in either dir) — usable for synthesis."""
    return find_cached_file(cache_key) is not None


# ==============================================================================
# Errors (translated to HTTP by the admin API)
# ==============================================================================
class VoiceProfileError(Exception):
    """Base class for voice-profile errors (maps to 400)."""


class VoiceProfileNotFound(VoiceProfileError):
    """Raised when a profile id is not in the registry (maps to 404)."""


# ==============================================================================
# Registry (durable SQLite index next to the .pt files)
# ==============================================================================
_SCHEMA = """
CREATE TABLE IF NOT EXISTS voice_profiles (
    profile_id      TEXT PRIMARY KEY,
    name            TEXT NOT NULL DEFAULT '',
    description     TEXT NOT NULL DEFAULT '',
    owner_tenant_id TEXT,
    visibility      TEXT NOT NULL DEFAULT 'private',
    is_default      INTEGER NOT NULL DEFAULT 0,
    persistent      INTEGER NOT NULL DEFAULT 0,
    source          TEXT NOT NULL DEFAULT '',
    ref_audio_url   TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS voice_profile_grants (
    profile_id TEXT NOT NULL,
    tenant_id  TEXT NOT NULL,
    PRIMARY KEY (profile_id, tenant_id),
    FOREIGN KEY (profile_id) REFERENCES voice_profiles(profile_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_vp_grants_tenant ON voice_profile_grants(tenant_id);
"""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _preview_url(rec: Dict[str, Any]) -> Optional[str]:
    """Best playable URL for previewing a voice: its stored reference audio, or — for url-keyed
    profiles whose id is itself the reference clip — the profile id. None when nothing is playable."""
    ref = (rec.get("ref_audio_url") or "").strip()
    if ref.startswith(("http://", "https://")):
        return ref
    pid = rec.get("profile_id") or ""
    if pid.startswith(("http://", "https://")):
        return pid
    return None


class VoiceProfileRegistry:
    """Metadata + access policy over the on-disk voice-profile cache."""

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
        self._seed_defaults_from_env()
        logger.info("Voice profile registry ready at %s.", path)

    def _migrate(self) -> None:
        """Additive schema migrations for registries created before a column existed."""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(voice_profiles)").fetchall()}
        if "persistent" not in cols:
            self._conn.execute(
                "ALTER TABLE voice_profiles ADD COLUMN persistent INTEGER NOT NULL DEFAULT 0")
        if "ref_audio_url" not in cols:
            self._conn.execute(
                "ALTER TABLE voice_profiles ADD COLUMN ref_audio_url TEXT NOT NULL DEFAULT ''")

    # --- seeding (optional built-in defaults) ---
    def _seed_defaults_from_env(self) -> None:
        """Register built-in default voices from DEFAULT_VOICE_PROFILES (JSON list), if set.

        Each entry: {"profile_id": "...", "name": "...", "description": "..."}. They are upserted as
        public defaults; their .pt file must still be cloned once before they can synthesize (the
        listing's `ready` flag reflects this).
        """
        raw = os.getenv("DEFAULT_VOICE_PROFILES")
        if not raw:
            return
        try:
            entries = json.loads(raw)
        except Exception as e:  # noqa: BLE001
            logger.error("Failed to parse DEFAULT_VOICE_PROFILES (%s); ignoring.", e)
            return
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict) or not entry.get("profile_id"):
                continue
            pid = str(entry["profile_id"])
            with self._lock:
                self._conn.execute(
                    "INSERT INTO voice_profiles (profile_id, name, description, owner_tenant_id, "
                    "visibility, is_default, source, ref_audio_url, created_at) "
                    "VALUES (?, ?, ?, NULL, ?, 1, 'default', ?, ?) "
                    "ON CONFLICT(profile_id) DO UPDATE SET "
                    "name=excluded.name, description=excluded.description, is_default=1, "
                    "visibility=excluded.visibility, ref_audio_url=excluded.ref_audio_url",
                    (pid, str(entry.get("name", pid)), str(entry.get("description", "")),
                     VISIBILITY_PUBLIC, str(entry.get("ref_audio_url", "")), _now()),
                )
                self._conn.commit()

    # --- serialization helpers (pure; safe to call outside the lock) ---
    @staticmethod
    def _to_dict(row: sqlite3.Row, grants: List[str]) -> Dict[str, Any]:
        return {
            "profile_id": row["profile_id"],
            "name": row["name"],
            "description": row["description"],
            "owner_tenant_id": row["owner_tenant_id"],
            "visibility": row["visibility"],
            "is_default": bool(row["is_default"]),
            "persistent": bool(row["persistent"]),
            "source": row["source"],
            "ref_audio_url": row["ref_audio_url"],
            "created_at": row["created_at"],
            "grants": list(grants),
        }

    @staticmethod
    def _accessible(rec: Dict[str, Any], tenant_id: Optional[str]) -> bool:
        if rec["is_default"] or rec["visibility"] == VISIBILITY_PUBLIC:
            return True
        if tenant_id and rec["owner_tenant_id"] == tenant_id:
            return True
        if tenant_id and tenant_id in rec["grants"]:
            return True
        return False

    @staticmethod
    def _relation(rec: Dict[str, Any], tenant_id: Optional[str]) -> str:
        """Why the tenant can see this voice (most-specific first)."""
        if tenant_id and rec["owner_tenant_id"] == tenant_id:
            return "owner"
        if rec["is_default"]:
            return "default"
        if rec["visibility"] == VISIBILITY_PUBLIC:
            return "public"
        if tenant_id and tenant_id in rec["grants"]:
            return "shared"
        return "none"

    @classmethod
    def _tenant_view(cls, rec: Dict[str, Any], tenant_id: Optional[str]) -> Dict[str, Any]:
        """Tenant-facing projection — never leaks other tenants' ids or the grant list."""
        return {
            "voice_profile_id": rec["profile_id"],
            "name": rec["name"],
            "description": rec["description"],
            "default": rec["is_default"],
            "visibility": rec["visibility"],
            "relation": cls._relation(rec, tenant_id),
            "ready": profile_ready(rec["profile_id"]),
            "created_at": rec["created_at"],
        }

    @staticmethod
    def _admin_view(rec: Dict[str, Any]) -> Dict[str, Any]:
        view = dict(rec)
        view["ready"] = profile_ready(rec["profile_id"])
        view["preview_url"] = _preview_url(rec)
        return view

    # --- registration (called by tts_core when a named profile is cloned) ---
    def register(self, profile_id: str, owner_tenant_id: Optional[str] = None,
                 name: str = "", source: str = "clone", description: str = "",
                 visibility: str = VISIBILITY_PRIVATE, ref_audio_url: str = "") -> Dict[str, Any]:
        """Insert a profile if absent; idempotent (re-cloning never changes ownership/visibility).

        `ref_audio_url` is the clip the voice was cloned from — kept so operators can preview the voice
        in the admin console. An already-registered profile with no preview URL on file is back-filled
        with one if a later (re-)clone supplies it; an operator-set name is never overwritten.
        """
        with self._lock:
            existing = self._conn.execute(
                "SELECT ref_audio_url FROM voice_profiles WHERE profile_id = ?",
                (profile_id,)).fetchone()
            if not existing:
                self._conn.execute(
                    "INSERT INTO voice_profiles (profile_id, name, description, owner_tenant_id, "
                    "visibility, is_default, source, ref_audio_url, created_at) "
                    "VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)",
                    (profile_id, name or profile_id, description, owner_tenant_id, visibility,
                     source, ref_audio_url, _now()),
                )
                self._conn.commit()
                logger.info("Registered voice profile '%s' (owner=%s).", profile_id, owner_tenant_id)
            elif ref_audio_url and not (existing["ref_audio_url"] or "").strip():
                self._conn.execute(
                    "UPDATE voice_profiles SET ref_audio_url = ? WHERE profile_id = ?",
                    (ref_audio_url, profile_id))
                self._conn.commit()
        return self.get(profile_id)

    # --- reads ---
    def get(self, profile_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM voice_profiles WHERE profile_id = ?", (profile_id,)).fetchone()
            if not row:
                return None
            grants = [r["tenant_id"] for r in self._conn.execute(
                "SELECT tenant_id FROM voice_profile_grants WHERE profile_id = ?",
                (profile_id,)).fetchall()]
        return self._to_dict(row, grants)

    def can_access(self, tenant_id: Optional[str], profile_id: str) -> bool:
        rec = self.get(profile_id)
        return rec is not None and self._accessible(rec, tenant_id)

    def list_all(self) -> List[Dict[str, Any]]:
        """Every registered profile (admin view, includes owner + grants + readiness)."""
        recs = self._fetch_all()
        return [self._admin_view(r) for r in recs]

    def list_for_tenant(self, tenant_id: Optional[str]) -> List[Dict[str, Any]]:
        """Profiles the tenant may use: defaults + public + owned + granted (tenant view)."""
        recs = self._fetch_all()
        return [self._tenant_view(r, tenant_id) for r in recs if self._accessible(r, tenant_id)]

    def _fetch_all(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM voice_profiles ORDER BY is_default DESC, name COLLATE NOCASE"
            ).fetchall()
            grants_map: Dict[str, List[str]] = {}
            for r in self._conn.execute(
                    "SELECT profile_id, tenant_id FROM voice_profile_grants").fetchall():
                grants_map.setdefault(r["profile_id"], []).append(r["tenant_id"])
        return [self._to_dict(row, grants_map.get(row["profile_id"], [])) for row in rows]

    def list_disk(self) -> List[Dict[str, Any]]:
        """List the raw .pt cache files on disk, flagging which ones the registry knows about.

        Lets an operator spot 'orphan' cached voices (hash-named files with no metadata) — e.g.
        profiles cloned before the registry existed, or url-keyed ephemeral caches.
        """
        recs = self._fetch_all()
        by_hash = {hashlib.sha256(r["profile_id"].encode("utf-8")).hexdigest(): r for r in recs}
        out: List[Dict[str, Any]] = []
        try:
            files = sorted(os.listdir(VOICE_PROFILES_DIR))
        except OSError:
            files = []
        for fn in files:
            if not fn.endswith(".pt"):
                continue
            digest = fn[:-3]
            path = os.path.join(VOICE_PROFILES_DIR, fn)
            try:
                st = os.stat(path)
            except OSError:
                continue
            rec = by_hash.get(digest)
            out.append({
                "file": fn,
                "size_bytes": st.st_size,
                "modified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(st.st_mtime)),
                "registered": rec is not None,
                "profile_id": rec["profile_id"] if rec else None,
                "name": rec["name"] if rec else None,
            })
        return out

    # --- management (admin) ---
    def set_visibility(self, profile_id: str, visibility: str) -> Dict[str, Any]:
        if visibility not in VALID_VISIBILITY:
            raise VoiceProfileError(
                f"Invalid visibility '{visibility}'. Use one of: {', '.join(sorted(VALID_VISIBILITY))}.")
        self._update_columns(profile_id, {"visibility": visibility})
        return self.get(profile_id)

    def set_default(self, profile_id: str, is_default: bool) -> Dict[str, Any]:
        self._update_columns(profile_id, {"is_default": 1 if is_default else 0})
        return self.get(profile_id)

    def set_persistent(self, profile_id: str, persistent: bool) -> Dict[str, Any]:
        """Choose whether to keep this voice on the persistent drive (and warm-load it on restart).

        With a separate persist dir configured, this also copies the cached tensor onto the durable
        volume (or removes that copy when turned off); otherwise it is just the flag. The warm-load /
        in-memory pin takes effect on the model process's next restart (see
        tts_core.warm_load_persistent_profiles).
        """
        self._update_columns(profile_id, {"persistent": 1 if persistent else 0})
        if two_tier():
            src, dst = get_profile_filepath(profile_id), get_persist_filepath(profile_id)
            try:
                if persistent and os.path.exists(src):
                    shutil.copyfile(src, dst)
                elif not persistent and os.path.exists(dst):
                    os.remove(dst)
            except OSError as e:
                logger.warning("Failed to sync persistent copy for '%s': %s", profile_id, e)
        return self.get(profile_id)

    def list_persistent(self) -> List[Dict[str, Any]]:
        """Profiles flagged persistent (used by the startup warm-loader)."""
        return [self._admin_view(r) for r in self._fetch_all() if r["persistent"]]

    def update(self, profile_id: str, name: Optional[str] = None,
               description: Optional[str] = None) -> Dict[str, Any]:
        cols: Dict[str, Any] = {}
        if name is not None:
            cols["name"] = name
        if description is not None:
            cols["description"] = description
        if cols:
            self._update_columns(profile_id, cols)
        return self._require(profile_id)

    def grant(self, profile_id: str, tenant_id: str) -> Dict[str, Any]:
        """Assign a profile to a specific tenant (visibility can stay private)."""
        with self._lock:
            if not self._conn.execute(
                    "SELECT 1 FROM voice_profiles WHERE profile_id = ?", (profile_id,)).fetchone():
                raise VoiceProfileNotFound(f"Voice profile '{profile_id}' not found.")
            self._conn.execute(
                "INSERT OR IGNORE INTO voice_profile_grants (profile_id, tenant_id) VALUES (?, ?)",
                (profile_id, tenant_id))
            self._conn.commit()
        return self.get(profile_id)

    def revoke(self, profile_id: str, tenant_id: str) -> Dict[str, Any]:
        with self._lock:
            self._conn.execute(
                "DELETE FROM voice_profile_grants WHERE profile_id = ? AND tenant_id = ?",
                (profile_id, tenant_id))
            self._conn.commit()
        return self._require(profile_id)

    def delete(self, profile_id: str, delete_file: bool = True) -> None:
        """Remove a profile (and its grants); optionally delete the cached .pt tensor too."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM voice_profiles WHERE profile_id = ?", (profile_id,))
            self._conn.commit()
            if cur.rowcount == 0:
                raise VoiceProfileNotFound(f"Voice profile '{profile_id}' not found.")
        if delete_file:
            for path in {get_profile_filepath(profile_id), get_persist_filepath(profile_id)}:
                try:
                    os.remove(path)
                except OSError:
                    pass

    # --- internal write helper ---
    def _update_columns(self, profile_id: str, cols: Dict[str, Any]) -> None:
        assignments = ", ".join(f"{c} = ?" for c in cols)
        params = list(cols.values()) + [profile_id]
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE voice_profiles SET {assignments} WHERE profile_id = ?", params)
            self._conn.commit()
            if cur.rowcount == 0:
                raise VoiceProfileNotFound(f"Voice profile '{profile_id}' not found.")

    def _require(self, profile_id: str) -> Dict[str, Any]:
        rec = self.get(profile_id)
        if rec is None:
            raise VoiceProfileNotFound(f"Voice profile '{profile_id}' not found.")
        return rec


# ==============================================================================
# Singleton + module-level facade (mirrors tenancy.py / credits.py style)
# ==============================================================================
_registry: Optional[VoiceProfileRegistry] = None
_registry_lock = threading.Lock()


def get_registry() -> VoiceProfileRegistry:
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                # The registry index must be as durable as the profiles it describes: default it onto
                # the persist volume when one is configured, so the id→file mapping survives a restart
                # (warm-loading depends on it). Override explicitly with VOICE_PROFILE_DB_PATH.
                path = os.getenv("VOICE_PROFILE_DB_PATH") or os.path.join(
                    VOICE_PROFILE_PERSIST_DIR, "profiles.db")
                _registry = VoiceProfileRegistry(path)
    return _registry


def reset_registry_for_tests() -> None:
    """Drop the cached registry so the next get_registry() re-reads the environment (tests only)."""
    global _registry
    with _registry_lock:
        _registry = None


def register(profile_id: str, owner_tenant_id: Optional[str] = None, **kwargs) -> Dict[str, Any]:
    return get_registry().register(profile_id, owner_tenant_id, **kwargs)


def get(profile_id: str) -> Optional[Dict[str, Any]]:
    return get_registry().get(profile_id)


def can_access(tenant_id: Optional[str], profile_id: str) -> bool:
    return get_registry().can_access(tenant_id, profile_id)


def list_for_tenant(tenant_id: Optional[str]) -> List[Dict[str, Any]]:
    return get_registry().list_for_tenant(tenant_id)


def list_all() -> List[Dict[str, Any]]:
    return get_registry().list_all()


def list_disk() -> List[Dict[str, Any]]:
    return get_registry().list_disk()


def list_persistent() -> List[Dict[str, Any]]:
    return get_registry().list_persistent()


def set_persistent(profile_id: str, persistent: bool) -> Dict[str, Any]:
    return get_registry().set_persistent(profile_id, persistent)
