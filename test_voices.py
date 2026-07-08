"""
test_voices.py — voice-profile registry, the tenant-facing GET /voices listing, the POST /tts access
checks, and the operator /admin/voices management surface.

Runs fully mocked (no model, no GPU): heavy libraries are stubbed before import, and a throwaway
VOICE_PROFILES_DIR holds the registry DB so tests never touch real cached voices.
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

import numpy as np  # noqa: F401  (kept for parity with the other suites' import preamble)

# --- mock heavy libraries that may be absent locally (must precede importing the app) ---
for _name in ("omnivoice", "torch", "torchaudio", "librosa", "soundfile", "runpod"):
    try:
        __import__(_name)
    except ImportError:
        sys.modules[_name] = MagicMock()
sys.modules["torch"].cuda.is_available.return_value = False

# --- configure a multi-tenant, admin-enabled server against temp profile dirs ---
# Use a separate persist dir so the two-tier (working cache + durable volume) path is exercised.
_TMPDIR = tempfile.mkdtemp(prefix="supi_voices_test_")
_PERSIST = tempfile.mkdtemp(prefix="supi_voices_persist_")
os.environ["VOICE_PROFILES_DIR"] = _TMPDIR
os.environ["VOICE_PROFILE_PERSIST_DIR"] = _PERSIST
os.environ["REQUIRE_AUTH"] = "true"
os.environ["ADMIN_API_KEY"] = "admin-secret"
os.environ["API_KEYS"] = (
    '{"key_acme": {"tenant_id": "acme", "name": "Acme"}, '
    '"key_globex": {"tenant_id": "globex", "name": "Globex"}}'
)

import app          # noqa: E402
import console_app   # noqa: E402
import tts_core      # noqa: E402
import voices        # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ACME = {"X-API-Key": "key_acme"}
GLOBEX = {"X-API-Key": "key_globex"}
ADMIN = {"X-Admin-Key": "admin-secret"}


class VoiceProfileTests(unittest.TestCase):
    def setUp(self):
        self.api = TestClient(app.app)
        self.con = TestClient(console_app.console_app)
        # Start each test from an empty registry and a clean L1 cache.
        reg = voices.get_registry()
        for rec in reg.list_all():
            reg.delete(rec["profile_id"], delete_file=True)
        tts_core.voice_profile_cache.clear()
        tts_core.voice_profile_cache.max_size = 20
        # acme owns a ready-to-use private voice.
        reg.register("acme-brand", owner_tenant_id="acme", name="Acme Brand")
        open(voices.get_profile_filepath("acme-brand"), "wb").close()

    # --- tenant-facing GET /voices ---
    def test_requires_auth(self):
        self.assertEqual(self.api.get("/voices").status_code, 401)

    def test_owner_sees_own_private_voice_others_do_not(self):
        acme = self.api.get("/voices", headers=ACME).json()
        self.assertEqual([v["voice_profile_id"] for v in acme["voices"]], ["acme-brand"])
        self.assertEqual(acme["voices"][0]["relation"], "owner")
        self.assertTrue(acme["voices"][0]["ready"])
        globex = self.api.get("/voices", headers=GLOBEX).json()
        self.assertEqual(globex["voices"], [])

    def test_publish_makes_voice_visible_to_all(self):
        self.con.post("/admin/voices/visibility", headers=ADMIN,
                      json={"profile_id": "acme-brand", "visibility": "public"})
        seen = self.api.get("/voices", headers=GLOBEX).json()["voices"]
        self.assertEqual([v["voice_profile_id"] for v in seen], ["acme-brand"])
        self.assertEqual(seen[0]["relation"], "public")

    def test_assign_to_specific_tenant(self):
        self.con.post("/admin/voices/grants", headers=ADMIN,
                      json={"profile_id": "acme-brand", "tenant_id": "globex"})
        seen = self.api.get("/voices", headers=GLOBEX).json()["voices"]
        self.assertEqual(seen[0]["relation"], "shared")
        # Revoking removes it again.
        self.con.delete("/admin/voices/grants?profile_id=acme-brand&tenant_id=globex", headers=ADMIN)
        self.assertEqual(self.api.get("/voices", headers=GLOBEX).json()["voices"], [])

    def test_url_shaped_profile_id_is_manageable(self):
        # Regression: voice ids are often full URLs (slashes + colon). They must route through the
        # admin surface without 404 — the id travels in the body/query, never in the URL path.
        url_id = "https://console.baarta.app/api/voice-profiles/290f03e0/combined.wav"
        reg = voices.get_registry()
        reg.register(url_id, owner_tenant_id="acme", name="Baarta")
        r = self.con.post("/admin/voices/grants", headers=ADMIN,
                          json={"profile_id": url_id, "tenant_id": "globex"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(self.api.get("/voices", headers=GLOBEX).json()["voices"][0]["relation"], "shared")
        self.assertEqual(self.con.post("/admin/voices/persist", headers=ADMIN,
                                       json={"profile_id": url_id, "persistent": True}).status_code, 200)
        self.assertEqual(self.con.delete(
            "/admin/voices/grants?profile_id=" + url_id + "&tenant_id=globex", headers=ADMIN).status_code, 200)
        self.assertEqual(self.api.get("/voices", headers=GLOBEX).json()["voices"], [])

    def test_default_voice_shown_to_everyone(self):
        self.con.post("/admin/voices/default", headers=ADMIN,
                      json={"profile_id": "acme-brand", "is_default": True})
        seen = self.api.get("/voices", headers=GLOBEX).json()["voices"]
        self.assertTrue(seen[0]["default"])
        self.assertEqual(seen[0]["relation"], "default")

    # --- POST /tts access control ---
    def test_tts_forbidden_for_inaccessible_profile(self):
        r = self.api.post("/tts", headers=GLOBEX,
                          json={"action": "generate", "text": "hi", "voice_profile_id": "acme-brand"})
        self.assertEqual(r.status_code, 403)
        self.assertIn("not available", r.json()["detail"])

    def test_tts_unknown_profile_without_ref_is_404(self):
        r = self.api.post("/tts", headers=ACME,
                          json={"action": "generate", "text": "hi", "voice_profile_id": "ghost"})
        self.assertEqual(r.status_code, 404)

    # --- admin surface ---
    def test_clone_registers_even_without_auth(self):
        # Serverless-handler / no-auth path: owner is None, but the cloned voice must still be
        # registered so it shows up in the admin console and can be persisted/warm-loaded.
        from unittest.mock import patch
        model = MagicMock()
        model.create_voice_clone_prompt.return_value = "PROMPT"
        with patch("tts_core.download_audio", return_value="/tmp/r.wav"), \
             patch("tts_core.preprocess_reference_audio"):
            tts_core.get_or_create_voice_prompt(
                model, "handler-voice", "https://ex.com/r.wav", None, owner_tenant_id=None)
        rec = voices.get("handler-voice")
        self.assertIsNotNone(rec)                      # registered despite no tenant
        self.assertIsNone(rec["owner_tenant_id"])
        self.assertEqual(rec["ref_audio_url"], "https://ex.com/r.wav")   # kept for preview
        self.assertIn("handler-voice", [v["profile_id"] for v in voices.list_all()])

    def test_admin_names_voice_and_tenant_receives_the_name(self):
        # Operators name a voice; that name (not the raw id) is what tenants get from GET /voices.
        self.con.patch("/admin/voices", headers=ADMIN,
                       json={"profile_id": "acme-brand", "name": "Warm Narrator"})
        self.con.post("/admin/voices/visibility", headers=ADMIN,
                      json={"profile_id": "acme-brand", "visibility": "public"})
        seen = self.api.get("/voices", headers=GLOBEX).json()["voices"][0]
        self.assertEqual(seen["name"], "Warm Narrator")
        self.assertEqual(seen["voice_profile_id"], "acme-brand")   # id still returned for selection

    def test_admin_listing_exposes_preview_url(self):
        # A url-keyed clone is previewable via its own audio url (no separate ref needed)...
        url_id = "https://cdn.example.com/voices/123/combined.wav"
        voices.get_registry().register(url_id, owner_tenant_id="acme")
        # ...and a named clone exposes the reference clip it was cloned from.
        voices.get_registry().register("seed-voice", owner_tenant_id="acme",
                                       ref_audio_url="https://cdn.example.com/ref.wav")
        listing = {v["profile_id"]: v
                   for v in self.con.get("/admin/voices", headers=ADMIN).json()["voices"]}
        self.assertEqual(listing[url_id]["preview_url"], url_id)
        self.assertEqual(listing["seed-voice"]["preview_url"], "https://cdn.example.com/ref.wav")
        self.assertIsNone(listing["acme-brand"]["preview_url"])     # plain id, no ref -> nothing to play

    def test_admin_requires_key(self):
        self.assertEqual(self.con.get("/admin/voices").status_code, 401)

    def test_admin_list_and_disk(self):
        listing = self.con.get("/admin/voices", headers=ADMIN).json()["voices"]
        self.assertEqual(listing[0]["owner_tenant_id"], "acme")
        self.assertTrue(listing[0]["ready"])
        disk = self.con.get("/admin/voices/disk", headers=ADMIN).json()["files"]
        self.assertTrue(any(f["profile_id"] == "acme-brand" and f["registered"] for f in disk))

    def test_admin_delete_removes_profile_and_file(self):
        path = voices.get_profile_filepath("acme-brand")
        self.assertTrue(os.path.exists(path))
        r = self.con.delete("/admin/voices?profile_id=acme-brand", headers=ADMIN)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(os.path.exists(path))
        self.assertIsNone(voices.get("acme-brand"))

    # --- persistence (durable volume + warm-load) ---
    def test_persist_copies_to_volume_and_unpersist_removes_it(self):
        persist_path = voices.get_persist_filepath("acme-brand")
        self.assertFalse(os.path.exists(persist_path))
        # Operator marks it persistent -> flag set + tensor copied onto the durable volume.
        r = self.con.post("/admin/voices/persist", headers=ADMIN,
                          json={"profile_id": "acme-brand", "persistent": True})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["persistent"])
        self.assertTrue(os.path.exists(persist_path))
        # Turning it off removes the durable copy again.
        self.con.post("/admin/voices/persist", headers=ADMIN,
                      json={"profile_id": "acme-brand", "persistent": False})
        self.assertFalse(os.path.exists(persist_path))

    def test_warm_load_loads_and_pins_persistent_profiles(self):
        voices.get_registry().set_persistent("acme-brand", True)
        # Simulate a restart: working cache wiped, L1 empty — only the durable copy remains.
        os.remove(voices.get_profile_filepath("acme-brand"))
        tts_core.voice_profile_cache.clear()
        self.assertIsNone(tts_core.voice_profile_cache.get("acme-brand"))

        loaded = tts_core.warm_load_persistent_profiles()
        self.assertEqual(loaded, 1)
        self.assertIsNotNone(tts_core.voice_profile_cache.get("acme-brand"))
        self.assertIn("acme-brand", tts_core.voice_profile_cache.pinned)

    def test_pinned_entries_survive_lru_eviction(self):
        cache = tts_core.voice_profile_cache
        cache.clear()
        cache.max_size = 2
        cache.set("pinned-voice", object(), pin=True)
        for i in range(5):  # churn well past capacity with ad-hoc clones
            cache.set(f"adhoc-{i}", object())
        self.assertIsNotNone(cache.get("pinned-voice"))            # pinned stays warm
        self.assertIsNone(cache.get("adhoc-0"))                    # oldest non-pinned evicted
        self.assertLessEqual(len([k for k in cache.cache if k not in cache.pinned]), 2)


if __name__ == "__main__":
    unittest.main()
