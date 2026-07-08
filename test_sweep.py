"""
test_sweep.py — covers the operator voice-sweep / A-B testing path:

  * Phase 1 (public API, [app.py]'s POST /admin/sweep): admin-key auth, the cell cap, one cell per
    combo, per-cell error isolation, and that the voice prompt is cloned ONCE for the whole grid.
  * Phase 2 (console proxy, [admin.py]'s POST /admin/sweep): the same-origin proxy forwards the body
    to the model service and returns its JSON verbatim.

Runs without a GPU or the real model: the heavy ML stack is mocked so `app`/`console_app` import, and
the two TTS-core calls the sweep makes (generate_audio, get_or_create_voice_prompt) are patched so the
tests assert on call counts and shape rather than real synthesis.
"""

import io
import os
import sys
import urllib.error
import urllib.request
import unittest
from unittest.mock import MagicMock, patch

# --- Environment must be set before importing the modules that read it at import time. -----------
os.environ["REQUIRE_AUTH"] = "false"                                  # /tts etc. not under test here
os.environ["ENABLE_SWEEP"] = "true"                                   # mount POST /admin/sweep
os.environ["ADMIN_API_KEY"] = "test-admin-key-long-enough-0123456789"  # guards the sweep route
os.environ["TTS_INTERNAL_URL"] = "http://127.0.0.1:8000"

# Mock the heavy ML stack so `app` (-> tts_core) imports on a CPU-only / py3.14 box.
for _name in ("torch", "torchaudio", "soundfile", "librosa"):
    sys.modules.setdefault(_name, MagicMock())
sys.modules.setdefault("omnivoice", MagicMock())

from fastapi.testclient import TestClient  # noqa: E402
import tts_core  # noqa: E402
import app  # noqa: E402
import admin  # noqa: E402
import console_app  # noqa: E402

ADMIN_HEADERS = {"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
_FAKE_CELL = ("UklGRg==", 24000, "24kHz_pcm16")  # (audio_base64, sample_rate, format_label)


class SweepEndpointTests(unittest.TestCase):
    """Phase 1: POST /admin/sweep on the public API."""

    def setUp(self):
        self.client = TestClient(app.app)
        app.model = MagicMock()  # truthy: model "loaded"

    def test_requires_admin_key(self):
        """Without the admin key the route is rejected (401), before any generation happens."""
        # A unique client IP keeps this single failure out of the shared throttle for other tests.
        r = self.client.post("/admin/sweep", json={"text": "नमस्ते"},
                             headers={"X-Forwarded-For": "203.0.113.42"})
        self.assertEqual(r.status_code, 401)

    def test_model_unavailable_returns_503(self):
        app.model = None
        r = self.client.post("/admin/sweep", json={"text": "नमस्ते"}, headers=ADMIN_HEADERS)
        self.assertEqual(r.status_code, 503)

    def test_cap_enforced(self):
        """A grid larger than MAX_SWEEP_CELLS is rejected with a 400 that tells the operator to narrow."""
        payload = {
            "text": "नमस्ते",
            "num_step": [8, 16, 24, 32, 40],          # 5 ...
            "guidance_scale": [1.0, 2.0, 3.0, 4.0, 5.0],  # ... x 5 = 25 > 24
        }
        with patch.object(tts_core, "generate_audio") as gen:
            r = self.client.post("/admin/sweep", json=payload, headers=ADMIN_HEADERS)
        self.assertEqual(r.status_code, 400)
        self.assertIn("Narrow an axis", r.json()["detail"])
        gen.assert_not_called()  # capped before any synthesis

    def test_one_cell_per_combo(self):
        """The Cartesian product yields exactly one cell (and one generate call) per combination."""
        payload = {
            "text": "नमस्ते",
            "num_step": [32, 64],            # 2 ...
            "guidance_scale": [1.5, 2.5],    # ... x 2 = 4 cells
        }
        with patch.object(tts_core, "generate_audio", return_value=_FAKE_CELL) as gen:
            r = self.client.post("/admin/sweep", json=payload, headers=ADMIN_HEADERS)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["count"], 4)
        self.assertEqual(len(body["cells"]), 4)
        self.assertEqual(gen.call_count, 4)
        # cell ids are 0..3 and each carries its distinct params + audio.
        self.assertEqual([c["cell_id"] for c in body["cells"]], [0, 1, 2, 3])
        self.assertTrue(all(c["audio_base64"] == "UklGRg==" for c in body["cells"]))
        self.assertEqual(body["base"], {"text": "नमस्ते", "voice": None, "seed": None})

    def test_per_cell_error_isolation(self):
        """A failure in one cell is captured, not fatal — the other cells still return audio."""
        def flaky(*args, **kwargs):
            if flaky.calls == 1:   # blow up on the second cell only
                flaky.calls += 1
                raise RuntimeError("boom on cell 1")
            flaky.calls += 1
            return _FAKE_CELL
        flaky.calls = 0

        payload = {"text": "नमस्ते", "num_step": [16, 32, 64]}  # 3 cells
        with patch.object(tts_core, "generate_audio", side_effect=flaky):
            r = self.client.post("/admin/sweep", json=payload, headers=ADMIN_HEADERS)
        self.assertEqual(r.status_code, 200)
        cells = r.json()["cells"]
        self.assertEqual(len(cells), 3)
        self.assertNotIn("error", cells[0])
        self.assertIn("error", cells[1])
        self.assertIn("boom on cell 1", cells[1]["error"])
        self.assertNotIn("error", cells[2])

    def test_voice_prompt_built_once(self):
        """The voice prompt is resolved a single time and reused across every cell (no re-clone)."""
        payload = {"text": "नमस्ते", "num_step": [16, 32, 64], "guidance_scale": [1.5, 2.5]}  # 6 cells
        with patch.object(tts_core, "get_or_create_voice_prompt", return_value="PROMPT") as prompt, \
                patch.object(tts_core, "generate_audio", return_value=_FAKE_CELL) as gen:
            r = self.client.post("/admin/sweep", json=payload, headers=ADMIN_HEADERS)
        self.assertEqual(r.status_code, 200)
        prompt.assert_called_once()
        self.assertEqual(gen.call_count, 6)
        # Every generate call received the one shared prompt object.
        self.assertTrue(all(c.kwargs.get("voice_clone_prompt") == "PROMPT" for c in gen.call_args_list))

    def test_out_of_range_axis_value_rejected(self):
        """An axis value outside the per-knob bounds is a 422 (same limits as the single-shot /tts)."""
        payload = {"text": "नमस्ते", "num_step": [9999]}  # > NUM_STEP_MAX
        r = self.client.post("/admin/sweep", json=payload, headers=ADMIN_HEADERS)
        self.assertEqual(r.status_code, 422)

    def test_url_keyed_voice_profile_id_accepted(self):
        """A profile cloned via ref_audio_url is keyed by that (long) URL; the field must accept it.

        Regression: a 256-char cap rejected every URL-keyed voice with a 422 when picked in the console.
        """
        url_key = "https://storage.example.com/refs/sample.wav?sig=" + "a" * 300  # > old 256 cap
        self.assertGreater(len(url_key), 256)
        payload = {"text": "नमस्ते", "voice_profile_id": url_key, "num_step": [16]}
        with patch.object(tts_core, "get_or_create_voice_prompt", return_value="PROMPT") as prompt, \
                patch.object(tts_core, "generate_audio", return_value=_FAKE_CELL):
            r = self.client.post("/admin/sweep", json=payload, headers=ADMIN_HEADERS)
        self.assertEqual(r.status_code, 200)
        # The full URL key is forwarded verbatim as the cache key to clone/reuse.
        self.assertEqual(prompt.call_args.args[1], url_key)
        self.assertEqual(r.json()["base"]["voice"], url_key)


class _FakeResp:
    """Minimal stand-in for the object urllib.request.urlopen returns (a context manager)."""
    def __init__(self, body):
        self._body = body
    def read(self):
        return self._body
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


class SweepProxyTests(unittest.TestCase):
    """Phase 2: the console's same-origin proxy to the model service."""

    def setUp(self):
        self.console = TestClient(console_app.console_app)

    def test_proxy_requires_admin_key(self):
        r = self.console.post("/admin/sweep", json={"text": "नमस्ते"},
                              headers={"X-Forwarded-For": "203.0.113.43"})
        self.assertEqual(r.status_code, 401)

    def test_proxy_forwards_upstream_json(self):
        upstream = b'{"base":{"text":"x"},"cells":[],"count":0}'
        with patch.object(urllib.request, "urlopen", return_value=_FakeResp(upstream)) as up:
            r = self.console.post("/admin/sweep", json={"text": "x"}, headers=ADMIN_HEADERS)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"base": {"text": "x"}, "cells": [], "count": 0})
        # The upstream call carried the admin key so the model service accepts it.
        # (urllib.request.Request normalises header keys via str.capitalize(): "X-Admin-Key" -> "X-admin-key".)
        sent_req = up.call_args.args[0]
        self.assertEqual(sent_req.get_header(admin.ADMIN_KEY_NAME.capitalize()), os.environ["ADMIN_API_KEY"])

    def test_proxy_surfaces_upstream_error(self):
        """An HTTP error from the model service is forwarded with its status and detail."""
        err = urllib.error.HTTPError(
            url="http://127.0.0.1:8000/admin/sweep", code=400, msg="Bad Request",
            hdrs=None, fp=io.BytesIO(b'{"detail":"too many cells"}'))
        with patch.object(urllib.request, "urlopen", side_effect=err):
            r = self.console.post("/admin/sweep", json={"text": "x"}, headers=ADMIN_HEADERS)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["detail"], "too many cells")


if __name__ == "__main__":
    unittest.main(verbosity=2)
