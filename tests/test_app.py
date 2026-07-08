import sys
import os
from unittest.mock import MagicMock, patch

# Ensure the parent directory is in sys.path so we can import app
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Mock omnivoice before any imports to avoid loading the real model weights
mock_omnivoice = MagicMock()
mock_model = MagicMock()
mock_omnivoice.OmniVoice.from_pretrained.return_value = mock_model
sys.modules['omnivoice'] = mock_omnivoice

# Mock torch before imports to avoid needing GPU or full library
mock_torch = MagicMock()
mock_torch.cuda.is_available.return_value = False
sys.modules['torch'] = mock_torch

# Import app now that heavy modules are mocked
import app
import unittest
from fastapi.testclient import TestClient
import numpy as np

class TestApp(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app.app)
        mock_model.generate.reset_mock()
        mock_model.create_voice_clone_prompt.reset_mock()
        app.model = mock_model
        app.voice_profile_cache.clear()

    def test_health_check_healthy(self):
        """Test health check returns 200 when model is loaded."""
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "healthy")

    def test_health_check_unhealthy(self):
        """Test health check returns status unhealthy when model is None."""
        app.model = None
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "unhealthy")

    def test_generate_single_tts_success(self):
        """Test single TTS generation endpoint."""
        payload = {
            "action": "generate",
            "text": "नमस्ते"
        }
        
        # Mock the model output
        mock_model.generate.return_value = [np.zeros(24000)]
        
        with patch('app.process_to_ulaw') as mock_process:
            mock_process.return_value = "dGVzdF9hdWRpb19kYXRh"
            response = self.client.post("/tts", json=payload)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], "success")
            self.assertEqual(response.json()["audio_base64"], "dGVzdF9hdWRpb19kYXRh")

    def test_generate_single_tts_missing_text(self):
        """Test single TTS fails when text is missing."""
        payload = {
            "action": "generate"
        }
        response = self.client.post("/tts", json=payload)
        self.assertEqual(response.status_code, 400)
        self.assertIn("Missing required parameter 'text'", response.json()["detail"])

    def test_bulk_generate_success(self):
        """Test template-based bulk TTS generation."""
        payload = {
            "action": "bulk_generate",
            "template": "नमस्ते {name}",
            "data": [
                {"name": "राम"},
                {"name": "श्याम"}
            ]
        }
        
        mock_model.generate.return_value = [np.zeros(24000)]
        
        with patch('app.process_to_ulaw') as mock_process:
            mock_process.return_value = "dGVzdF9hdWRpb19kYXRh"
            response = self.client.post("/tts", json=payload)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], "success")
            self.assertEqual(len(response.json()["results"]), 2)
            self.assertEqual(response.json()["results"][0]["text_generated"], "नमस्ते राम")
            self.assertEqual(response.json()["results"][1]["text_generated"], "नमस्ते श्याम")

    def test_invalid_action(self):
        """Test validation error for unsupported action."""
        payload = {
            "action": "invalid_action",
            "text": "test"
        }
        response = self.client.post("/tts", json=payload)
        self.assertEqual(response.status_code, 400)
        self.assertIn("Unsupported action", response.json()["detail"])

    def test_download_audio_failure(self):
        """Test that failure to download voice cloning reference raises 400."""
        payload = {
            "action": "generate",
            "text": "test",
            "ref_audio_url": "http://invalid-url/ref.wav"
        }
        with patch('requests.get') as mock_get:
            mock_get.side_effect = Exception("Connection error")
            response = self.client.post("/tts", json=payload)
            self.assertEqual(response.status_code, 400)
            self.assertIn("Failed to download reference audio", response.json()["detail"])

    def test_voice_clone_cache_hit_and_miss(self):
        """Test that voice cloning downloads & creates prompt on miss, and reuses it on hit."""
        payload = {
            "action": "generate",
            "text": "नमस्ते",
            "ref_audio_url": "http://example.com/ref.wav",
            "voice_profile_id": "profile_1"
        }

        mock_model.generate.return_value = [np.zeros(24000)]
        mock_model.create_voice_clone_prompt.return_value = "fake_prompt_object"

        with patch('app.download_audio') as mock_download, \
             patch('app.process_to_ulaw') as mock_process:
            
            mock_download.return_value = "/fake/path/ref.wav"
            mock_process.return_value = "audio_data_base64"

            # First request: Cache Miss
            response1 = self.client.post("/tts", json=payload)
            self.assertEqual(response1.status_code, 200)
            mock_download.assert_called_once()
            mock_model.create_voice_clone_prompt.assert_called_once_with(
                ref_audio="/fake/path/ref.wav",
                ref_text=None
            )

            # Reset download & create prompt mocks
            mock_download.reset_mock()
            mock_model.create_voice_clone_prompt.reset_mock()

            # Second request: Cache Hit (should not download or create prompt again)
            response2 = self.client.post("/tts", json=payload)
            self.assertEqual(response2.status_code, 200)
            mock_download.assert_not_called()
            mock_model.create_voice_clone_prompt.assert_not_called()

    def test_voice_clone_cache_miss_without_url(self):
        """Test that requesting a voice_profile_id that is not cached raises 400 when ref_audio_url is missing."""
        payload = {
            "action": "generate",
            "text": "नमस्ते",
            "voice_profile_id": "non_existent_profile"
        }
        response = self.client.post("/tts", json=payload)
        self.assertEqual(response.status_code, 400)
        self.assertIn("Cache miss for voice profile", response.json()["detail"])

    def test_voice_clone_cache_lru_eviction(self):
        """Test that cache evicts the oldest items when max_size is exceeded."""
        app.voice_profile_cache.max_size = 2
        mock_model.generate.return_value = [np.zeros(24000)]
        mock_model.create_voice_clone_prompt.side_effect = lambda ref_audio, ref_text: f"prompt_{ref_audio}"

        with patch('app.download_audio') as mock_download, \
             patch('app.process_to_ulaw'):
            
            # Fill cache with 2 items
            for profile_id in ["p1", "p2"]:
                mock_download.return_value = f"/path/{profile_id}.wav"
                payload = {
                    "action": "generate",
                    "text": "नमस्ते",
                    "ref_audio_url": f"http://example.com/{profile_id}.wav",
                    "voice_profile_id": profile_id
                }
                self.client.post("/tts", json=payload)

            self.assertIsNotNone(app.voice_profile_cache.get("p1"))
            self.assertIsNotNone(app.voice_profile_cache.get("p2"))

            # Add third item to trigger eviction of p1 (the oldest)
            mock_download.return_value = "/path/p3.wav"
            payload = {
                "action": "generate",
                "text": "नमस्ते",
                "ref_audio_url": "http://example.com/p3.wav",
                "voice_profile_id": "p3"
            }
            self.client.post("/tts", json=payload)

            # p1 should be evicted, p2 and p3 remain
            self.assertIsNone(app.voice_profile_cache.get("p1"))
            self.assertIsNotNone(app.voice_profile_cache.get("p2"))
            self.assertIsNotNone(app.voice_profile_cache.get("p3"))

    def test_generate_single_tts_with_custom_num_step(self):
        """Test single TTS generation with custom num_step parameter."""
        payload = {
            "action": "generate",
            "text": "नमस्ते",
            "num_step": 24
        }
        
        mock_model.generate.return_value = [np.zeros(24000)]
        
        with patch('app.process_to_ulaw') as mock_process:
            mock_process.return_value = "dGVzdF9hdWRpb19kYXRh"
            response = self.client.post("/tts", json=payload)
            self.assertEqual(response.status_code, 200)
            mock_model.generate.assert_called_with(text="नमस्ते", num_step=24)

    def test_generate_single_tts_with_invalid_instruct(self):
        """Test single TTS generation with an unsupported instruction tag (e.g. 'sad') is filtered out."""
        payload = {
            "action": "generate",
            "text": "नमस्ते",
            "instruct": "sad"
        }
        mock_model.generate.return_value = [np.zeros(24000)]
        with patch('app.process_to_ulaw') as mock_process:
            mock_process.return_value = "dGVzdF9hdWRpb19kYXRh"
            response = self.client.post("/tts", json=payload)
            self.assertEqual(response.status_code, 200)
            # Check that model generate was called, but instruct key is absent or not passed since 'sad' is filtered out
            call_kwargs = mock_model.generate.call_args[1]
            self.assertNotIn("instruct", call_kwargs)

    def test_generate_single_tts_with_mixed_instruct(self):
        """Test single TTS generation with mixed supported/unsupported instructions."""
        payload = {
            "action": "generate",
            "text": "नमस्ते",
            "instruct": "american accent, sad"
        }
        mock_model.generate.return_value = [np.zeros(24000)]
        with patch('app.process_to_ulaw') as mock_process:
            mock_process.return_value = "dGVzdF9hdWRpb19kYXRh"
            response = self.client.post("/tts", json=payload)
            self.assertEqual(response.status_code, 200)
            # 'american accent' is valid and should be passed, while 'sad' is filtered out.
            call_kwargs = mock_model.generate.call_args[1]
            self.assertEqual(call_kwargs.get("instruct"), "american accent")

    def test_generate_single_tts_with_numstep_alias(self):
        """Test single TTS generation with numstep alias parameter."""
        payload = {
            "action": "generate",
            "text": "नमस्ते",
            "numstep": 18
        }
        mock_model.generate.return_value = [np.zeros(24000)]
        with patch('app.process_to_ulaw') as mock_process:
            mock_process.return_value = "dGVzdF9hdWRpb19kYXRh"
            response = self.client.post("/tts", json=payload)
            self.assertEqual(response.status_code, 200)
            mock_model.generate.assert_called_with(text="नमस्ते", num_step=18)

    def test_generate_single_tts_with_quality_preset(self):
        """Test single TTS generation with quality preset parameter."""
        # Test speed preset
        payload = {
            "action": "generate",
            "text": "नमस्ते",
            "quality": "speed"
        }
        mock_model.generate.return_value = [np.zeros(24000)]
        with patch('app.process_to_ulaw') as mock_process:
            mock_process.return_value = "dGVzdF9hdWRpb19kYXRh"
            response = self.client.post("/tts", json=payload)
            self.assertEqual(response.status_code, 200)
            mock_model.generate.assert_called_with(text="नमस्ते", num_step=16)

        # Test max preset
        payload_max = {
            "action": "generate",
            "text": "नमस्ते",
            "quality": "max"
        }
        mock_model.generate.reset_mock()
        with patch('app.process_to_ulaw') as mock_process:
            mock_process.return_value = "dGVzdF9hdWRpb19kYXRh"
            response = self.client.post("/tts", json=payload_max)
            self.assertEqual(response.status_code, 200)
            mock_model.generate.assert_called_with(text="नमस्ते", num_step=128)

    def test_generate_single_tts_with_unified_temperature(self):
        """Test single TTS generation with unified temperature parameter."""
        payload = {
            "action": "generate",
            "text": "नमस्ते",
            "temperature": 0.3
        }
        mock_model.generate.return_value = [np.zeros(24000)]
        with patch('app.process_to_ulaw') as mock_process:
            mock_process.return_value = "dGVzdF9hdWRpb19kYXRh"
            response = self.client.post("/tts", json=payload)
            self.assertEqual(response.status_code, 200)
            mock_model.generate.assert_called_with(
                text="नमस्ते", 
                num_step=32,
                class_temperature=0.3,
                position_temperature=6.0
            )
    def test_normalize_text_numbers_cardinals(self):
        """Test number normalization for cardinal numbers."""
        self.assertEqual(app.normalize_text_numbers("I have 17 apples"), "I have seventeen apples")
        self.assertEqual(app.normalize_text_numbers("We are in 2026"), "We are in two thousand twenty-six")
        self.assertEqual(app.normalize_text_numbers("The code is 1,250"), "The code is one thousand two hundred fifty")

    def test_normalize_text_numbers_decimals(self):
        """Test number normalization for decimal numbers."""
        self.assertEqual(app.normalize_text_numbers("It is 17.5 degrees"), "It is seventeen point five degrees")
        self.assertEqual(app.normalize_text_numbers("Value is 0.05"), "Value is zero point zero five")

    def test_normalize_text_numbers_ordinals(self):
        """Test number normalization for ordinal numbers."""
        self.assertEqual(app.normalize_text_numbers("June 17th is today"), "June seventeenth is today")
        self.assertEqual(app.normalize_text_numbers("This is the 2nd time"), "This is the second time")
        self.assertEqual(app.normalize_text_numbers("On the 31st floor"), "On the thirty-first floor")

    def test_normalize_text_numbers_digits(self):
        """Test number normalization for long or zero-padded sequences read digit-by-digit."""
        self.assertEqual(app.normalize_text_numbers("Call 984123"), "Call nine eight four one two three")
        self.assertEqual(app.normalize_text_numbers("Pin is 007"), "Pin is zero zero seven")

    def test_normalize_text_numbers_mixed_and_devanagari(self):
        """Devanagari digits become Nepali words; Latin digits become English words."""
        self.assertEqual(app.normalize_text_numbers("नमस्ते १७, जुन १७"), "नमस्ते सत्र, जुन सत्र")
        self.assertEqual(app.normalize_text_numbers("नमस्ते 17, जुन 17"), "नमस्ते seventeen, जुन seventeen")
        # Mixed scripts in one string keep their own language's words.
        self.assertEqual(app.normalize_text_numbers("२ apples, 16 मान्छे"), "दुई apples, sixteen मान्छे")

if __name__ == '__main__':
    unittest.main()
