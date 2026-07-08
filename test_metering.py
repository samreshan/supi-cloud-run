"""
test_metering.py — verifies the per-tenant metered/unmetered plan, the out-of-credits feedback,
and the security hardening (response headers + admin brute-force throttle).

Runs without a GPU or the real model: heavy libs are mocked so `app` imports. The store/credits
logic itself is pure Python and needs no mocking.
"""

import os
import sys
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock

# --- Environment must be set before importing the modules that read it at import time. -----------
_TMPDIR = tempfile.mkdtemp()
os.environ["TENANT_DB_PATH"] = os.path.join(_TMPDIR, "supi.db")
os.environ["CREDITS_ENABLED"] = "true"
os.environ["REQUIRE_AUTH"] = "true"
os.environ["ADMIN_API_KEY"] = "test-admin-key-long-enough-0123456789"
os.environ["CREDITS_LOW_BALANCE_THRESHOLD"] = "0"

# Mock the heavy ML stack so `app` (-> tts_core) imports on a CPU-only/py3.14 box.
for _name in ("torch", "torchaudio", "soundfile", "librosa"):
    sys.modules.setdefault(_name, MagicMock())
sys.modules.setdefault("omnivoice", MagicMock())

import store  # noqa: E402
import credits  # noqa: E402


def _fresh_store():
    store.reset_store_for_tests()
    return store.get_store()


class TestStoreMetering(unittest.TestCase):
    def setUp(self):
        self.st = _fresh_store()

    def test_new_tenant_is_metered_by_default(self):
        self.st.create_tenant("t_meter_default", "Default", 50)
        self.assertTrue(self.st.get_tenant("t_meter_default")["metered"])

    def test_set_unmetered_then_back(self):
        self.st.create_tenant("t_toggle", "Toggle", 10)
        self.assertFalse(self.st.set_metered("t_toggle", False)["metered"])
        self.assertFalse(self.st.get_tenant("t_toggle")["metered"])
        # Authenticated Tenant reflects the flag, so the request path can read it.
        key = self.st.create_key("t_toggle")["api_key"]
        self.assertFalse(self.st.authenticate(key).metered)
        # Balance is preserved across the toggle.
        self.assertEqual(self.st.get_tenant("t_toggle")["credits_remaining"], 10)
        self.assertTrue(self.st.set_metered("t_toggle", True)["metered"])
        self.assertTrue(self.st.authenticate(key).metered)

    def test_list_tenants_exposes_metered_bool(self):
        self.st.create_tenant("t_list", "List", 0)
        self.st.set_metered("t_list", False)
        row = next(r for r in self.st.list_tenants() if r["tenant_id"] == "t_list")
        self.assertIs(row["metered"], False)

    def test_set_metered_unknown_tenant(self):
        with self.assertRaises(store.TenantNotFound):
            self.st.set_metered("nope", False)


class TestMigration(unittest.TestCase):
    def test_old_db_without_metered_column_is_migrated(self):
        path = os.path.join(_TMPDIR, "legacy.db")
        conn = sqlite3.connect(path)
        conn.executescript(
            "CREATE TABLE tenants (tenant_id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', "
            "credits_remaining REAL NOT NULL DEFAULT 0, credits_granted REAL NOT NULL DEFAULT 0, "
            "credits_used REAL NOT NULL DEFAULT 0, created_at TEXT NOT NULL);"
            "INSERT INTO tenants VALUES ('legacy','Legacy',100,100,0,'2020-01-01T00:00:00Z');"
        )
        conn.commit()
        conn.close()
        st = store.SqliteStore(path)  # __init__ runs _migrate()
        t = st.get_tenant("legacy")
        self.assertTrue(t["metered"])  # back-filled to the metered default
        self.assertEqual(t["credits_remaining"], 100)


class TestCreditsGating(unittest.TestCase):
    def setUp(self):
        self.st = _fresh_store()
        self.assertTrue(credits.ENABLED, "CREDITS_ENABLED must be true for these tests")

    def test_metered_tenant_is_charged(self):
        self.st.create_tenant("c_metered", "Metered", 100)
        credits.ensure_affordable("c_metered", 10, metered=True)  # within balance, no raise
        remaining = credits.charge("c_metered", 10, metered=True)
        self.assertEqual(remaining, 90)

    def test_unmetered_tenant_is_never_charged(self):
        self.st.create_tenant("c_unmetered", "Unmetered", 0)  # zero balance
        # Even with no credits, an unmetered tenant is affordable and uncharged.
        credits.ensure_affordable("c_unmetered", 10_000, metered=False)
        self.assertEqual(credits.charge("c_unmetered", 10_000, metered=False), -1.0)
        self.assertEqual(self.st.get_tenant("c_unmetered")["credits_remaining"], 0)

    def test_insufficient_credits_is_structured(self):
        self.st.create_tenant("c_broke", "Broke", 5)
        with self.assertRaises(credits.InsufficientCreditsError) as ctx:
            credits.ensure_affordable("c_broke", 100, metered=True)
        err = ctx.exception
        self.assertEqual(err.required, 100)
        self.assertEqual(err.remaining, 5)
        self.assertEqual(err.unit, "characters")

    def test_balance_warning(self):
        self.assertIsNotNone(credits.balance_warning(0))     # exhausted -> warn
        self.assertIsNone(credits.balance_warning(1000))     # plenty, threshold disabled -> no warn

    def test_get_balance_unlimited_for_unmetered(self):
        self.st.create_tenant("c_plan", "Plan", 42)
        bal = credits.get_balance("c_plan", metered=False)
        self.assertEqual(bal["plan"], "unlimited")
        self.assertIs(bal["metered"], False)
        self.assertIsNone(bal["credits_remaining"])


class TestSecurityAndRoutes(unittest.TestCase):
    """Imports the real apps (heavy libs mocked) and exercises headers, throttle, and the new route."""

    @classmethod
    def setUpClass(cls):
        _fresh_store()
        from fastapi.testclient import TestClient
        import app
        import console_app
        cls.api = TestClient(app.app)
        cls.console = TestClient(console_app.console_app)
        cls.admin_headers = {"X-Admin-Key": os.environ["ADMIN_API_KEY"]}

    def test_public_security_headers(self):
        r = self.api.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(r.headers.get("X-Frame-Options"), "DENY")
        self.assertIn("frame-ancestors 'none'", r.headers.get("Content-Security-Policy", ""))

    def test_console_headers_no_store(self):
        r = self.console.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("Cache-Control"), "no-store")
        self.assertIn("frame-ancestors 'none'", r.headers.get("Content-Security-Policy", ""))

    def test_metered_toggle_endpoint(self):
        self.console.post("/admin/tenants", json={"tenant_id": "route_t", "credits": 5},
                          headers=self.admin_headers)
        r = self.console.post("/admin/tenants/route_t/metered", json={"metered": False},
                              headers=self.admin_headers)
        self.assertEqual(r.status_code, 200)
        self.assertIs(r.json()["metered"], False)

    def test_admin_brute_force_lockout(self):
        # Use a dedicated client IP (via X-Forwarded-For) so this lockout can't block the other
        # correct-key tests sharing this process/throttle.
        bad = {"X-Admin-Key": "wrong-key", "X-Forwarded-For": "203.0.113.7"}
        statuses = [self.console.get("/admin/tenants", headers=bad).status_code for _ in range(6)]
        self.assertIn(401, statuses)         # first failures are rejected as unauthorized
        self.assertEqual(statuses[-1], 429)  # then the client is locked out


if __name__ == "__main__":
    unittest.main(verbosity=2)
