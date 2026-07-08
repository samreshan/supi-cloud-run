"""
tts_core.py — Shared OmniVoice TTS logic, CPU/Cloud Run fork.

Forked from the omniServerless GPU/RunPod codebase. [app.py] (FastAPI, the only entry point
here — there is no RunPod handler in this repo) imports this module so the text/emotion/audio/
inference logic lives in one place.

Design notes:
  * The expensive `model` is injected into the orchestration functions as a parameter rather
    than being a module global, so tests can inject a mock model.
  * Cheap, deterministic runtime config (DEVICE / DTYPE / thread tuning) is computed once here
    at import — importing this module does NOT download the model.
  * Functions raise `TTSInputError` (client's fault -> 4xx) or `TTSServerError` (our fault -> 5xx);
    the entry point translates those into its own error shape.
  * CPU-specific knobs (thread count, int8 dynamic quantization, ASR loading, default flow-matching
    step count) are env-gated in `load_model()` / `resolve_num_step()` — see README.md for tuning.
"""

import os
import io
import gc
import re
import uuid
import base64
import socket
import hashlib
import logging
import tempfile
import ipaddress
from urllib.parse import urlparse
from collections import OrderedDict
from typing import Optional, List, Dict, Any, Union, Tuple

import requests
import numpy as np
import torch
import torchaudio
import soundfile as sf
import librosa
from omnivoice import OmniVoice

import voices  # lightweight (stdlib-only) registry; owns the profile cache path + sharing metadata

# ==============================================================================
# Logging (structured-ish, no raw user text — avoids leaking PII to stdout)
# ==============================================================================
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("omnivoice")


# ==============================================================================
# Error types — translated to HTTP / job errors by each entry point
# ==============================================================================
class TTSError(Exception):
    """Base class for TTS errors."""


class TTSInputError(TTSError):
    """Client error — bad/oversized/unsafe input (maps to 4xx)."""


class TTSServerError(TTSError):
    """Server error — model/codec/internal failure (maps to 5xx)."""


# ==============================================================================
# Runtime config & GPU/CPU optimisations (computed once at import; no model download)
# ==============================================================================
DEVICE = "cuda:0" if torch.cuda.is_available() else "cpu"
DTYPE = torch.bfloat16 if torch.cuda.is_available() else torch.float32

NATIVE_SR = 24000  # OmniVoice native output sample rate

# ------------------------------------------------------------------------------
# CPU thread tuning — Cloud Run assigns a fixed vCPU count per instance (CPU_LIMIT, if unset
# falls back to os.cpu_count()). Intra-op threads default to that count; inter-op stays at 1
# because there is exactly one request in flight at a time (concurrency=1 on Cloud Run — see
# README.md). Oversubscribing threads is a common cause of CPU inference being *slower* than
# expected, not faster.
# ------------------------------------------------------------------------------
if not torch.cuda.is_available():
    _cpu_count = int(os.getenv("CPU_LIMIT", str(os.cpu_count() or 1)))
    try:
        torch.set_num_threads(max(1, _cpu_count))
        torch.set_num_interop_threads(1)
        logger.info("CPU inference: torch intra-op threads=%d, inter-op threads=1.", _cpu_count)
    except Exception as e:  # pragma: no cover - platform dependent
        logger.warning("Failed to configure CPU thread counts: %s", e)

gpu_resampler = None
if torch.cuda.is_available():
    try:
        gpu_resampler = torchaudio.transforms.Resample(orig_freq=NATIVE_SR, new_freq=8000).to(DEVICE)
        logger.info("GPU-accelerated resampler initialised.")
    except Exception as e:  # pragma: no cover - hardware dependent
        logger.warning("Failed to initialise GPU resampler: %s", e)

if torch.cuda.is_available():
    try:
        major, minor = torch.cuda.get_device_capability()
        logger.info("CUDA device: %s (Compute Capability %d.%d)",
                    torch.cuda.get_device_name(0), major, minor)
        if major >= 8:  # TF32 supported on Ampere+
            torch.backends.cuda.matmul.allow_tf32 = True
            torch.backends.cudnn.allow_tf32 = True
            logger.info("Enabled TF32 for Ampere+ GPU.")
        torch.backends.cuda.enable_flash_sdp(True)
        torch.backends.cuda.enable_mem_efficient_sdp(True)
        torch.backends.cuda.enable_math_sdp(True)
        logger.info("SDPA attention backends configured.")
    except Exception as e:  # pragma: no cover - hardware dependent
        logger.warning("Failed to configure GPU optimisation flags: %s", e)


# ASR (used only for auto-transcribing reference audio when a voice-clone call omits `ref_text`)
# adds real load time + RAM on a CPU cold start. Default on to preserve behaviour; set LOAD_ASR=false
# once you've confirmed callers always pass `ref_text` for cloning.
LOAD_ASR = os.getenv("LOAD_ASR", "true").lower() in ("1", "true", "yes")

# Dynamic int8 quantization of Linear layers roughly halves CPU matmul cost with a small, generally
# inaudible quality tradeoff at telephony (8kHz mu-law) output — but it IS a quality tradeoff, so it's
# opt-in. Validate with the admin /admin/sweep A/B grid before enabling in production.
CPU_INT8 = os.getenv("CPU_INT8", "false").lower() in ("1", "true", "yes")


def load_model() -> Any:
    """Load the OmniVoice model. On CUDA, also compiles it. Called by the entry point."""
    logger.info("Loading OmniVoice model on %s (%s), load_asr=%s...", DEVICE, DTYPE, LOAD_ASR)
    m = OmniVoice.from_pretrained(
        "k2-fsa/OmniVoice",
        device_map=DEVICE,
        dtype=DTYPE,
        load_asr=LOAD_ASR,
    )
    if torch.cuda.is_available():
        try:
            logger.info("Compiling model with torch.compile(dynamic=True)...")
            m = torch.compile(m, dynamic=True)
        except Exception as e:
            logger.warning("torch.compile failed (using uncompiled): %s", e)
        torch.cuda.empty_cache()
    elif CPU_INT8:
        # torch.compile is intentionally NOT used on CPU: Cloud Run bills/measures cold starts,
        # and inductor's first-call compilation adds tens of seconds we can't afford there.
        try:
            logger.info("Applying dynamic int8 quantization to Linear layers (CPU_INT8=true)...")
            m = torch.ao.quantization.quantize_dynamic(m, {torch.nn.Linear}, dtype=torch.qint8)
        except Exception as e:
            logger.warning("int8 quantization failed (using fp32): %s", e)
    gc.collect()
    logger.info("OmniVoice model ready.")
    return m


def warmup(model: Any) -> None:
    """Run short generations to compile lazy kernels and pre-cache allocator segments.

    Runs count is env-tunable (WARMUP_RUNS, default 1): each run is a real generation, and on a
    Cloud Run cold start every second here is a second the caller's first request is waiting.
    """
    if model is None:
        return
    warmup_texts = ["नमस्ते", "नमस्ते साथीहरु, तपाईहरुलाई स्वागत छ।"]
    runs = max(0, int(os.getenv("WARMUP_RUNS", "1")))
    try:
        with torch.inference_mode():
            for text in warmup_texts[:runs]:
                _ = model.generate(text=text, num_step=8)
        logger.info("Warm-up runs complete (%d run(s)).", runs)
    except Exception as e:
        logger.warning("Warm-up runs failed: %s", e)


# ==============================================================================
# Request limits & quality presets (shared so both entry points enforce the same)
# ==============================================================================
MAX_TEXT_CHARS = int(os.getenv("MAX_TEXT_CHARS", "8000"))
MAX_BULK_ITEMS = int(os.getenv("MAX_BULK_ITEMS", "100"))
NUM_STEP_MIN = 1
NUM_STEP_MAX = 128
# Flow-matching step count is linear in inference cost. On GPU 32 was the historical default; on
# CPU (billed per vCPU-second on Cloud Run), fewer steps directly cut cost. Validate the chosen
# value with the admin /admin/sweep A/B grid on real telephony (8kHz mu-law) audio before lowering.
DEFAULT_NUM_STEP = int(os.getenv("DEFAULT_NUM_STEP", "32"))
SPEED_MIN, SPEED_MAX = 0.25, 4.0
TEMP_MIN, TEMP_MAX = 0.0, 5.0
GUIDANCE_MIN, GUIDANCE_MAX = 0.0, 10.0

# Quality presets currently map to num_step (kept identical to prior behaviour).
# Structured as dicts so guidance/temperature bundles can be added after listening tests
# without another refactor.
QUALITY_PRESETS: Dict[str, Dict[str, Any]] = {
    "speed": {"num_step": 16}, "fast": {"num_step": 16},
    "standard": {"num_step": 24}, "normal": {"num_step": 24}, "medium": {"num_step": 24},
    "high": {"num_step": 32}, "premium": {"num_step": 32}, "best": {"num_step": 32},
    "very_high": {"num_step": 64}, "veryhigh": {"num_step": 64}, "ultra": {"num_step": 64},
    "max": {"num_step": 128}, "highest": {"num_step": 128}, "supreme": {"num_step": 128},
}


def resolve_quality(quality: Optional[str]) -> Dict[str, Any]:
    """Return the preset bundle for a quality name (empty dict if unknown/None)."""
    if not quality:
        return {}
    return dict(QUALITY_PRESETS.get(str(quality).lower(), {}))


def resolve_num_step(num_step, numstep, quality) -> int:
    """Resolve final num_step: explicit num_step > alias numstep > quality preset > DEFAULT_NUM_STEP."""
    preset = resolve_quality(quality)
    resolved = num_step if num_step is not None else (
        numstep if numstep is not None else preset.get("num_step", DEFAULT_NUM_STEP)
    )
    return _clamp_int(int(resolved), NUM_STEP_MIN, NUM_STEP_MAX)


def resolve_temperatures(temperature, position_temperature, class_temperature) -> Tuple[float, float]:
    """Resolve (position_temperature, class_temperature) honouring the unified `temperature` knob."""
    if class_temperature is None:
        class_temperature = float(temperature) if temperature is not None else 0.25
    if position_temperature is None:
        position_temperature = float(temperature) * 20.0 if temperature is not None else 5.0
    return (
        _clamp_float(float(position_temperature), TEMP_MIN, TEMP_MAX * 20.0),
        _clamp_float(float(class_temperature), TEMP_MIN, TEMP_MAX),
    )


def _clamp_int(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def _clamp_float(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


# ==============================================================================
# Instruction (style) handling — accents / pitch / age / gender / whisper
# ==============================================================================
# k2-fsa/OmniVoice's `instruct` vocabulary. Anything outside this set is dropped to avoid
# 500s from the model. Emotion words like "sad"/"excited" are NOT here — they are translated
# to these knobs by EMOTION_MAP below.
VALID_INSTRUCTS = {
    # English instructions
    "american accent", "australian accent", "british accent", "canadian accent",
    "child", "chinese accent", "elderly", "female", "high pitch", "indian accent",
    "japanese accent", "korean accent", "low pitch", "male", "middle-aged",
    "moderate pitch", "portuguese accent", "russian accent", "teenager",
    "very high pitch", "very low pitch", "whisper", "young adult",
    # Chinese instructions
    "东北话", "中年", "中音调", "云南话", "低音调", "儿童", "四川话", "女",
    "宁夏话", "少年", "极低音调", "极高音调", "桂林话", "河南话", "济南话",
    "甘肃话", "男", "石家庄话", "老年", "耳语", "贵州话", "陕西话", "青岛话",
    "青年", "高音调",
}


def filter_instruct(instruct_str: Optional[str]) -> Optional[str]:
    """Keep only instruction tokens OmniVoice supports; returns None if nothing valid remains."""
    if not instruct_str:
        return None
    cleaned_str = instruct_str.replace("，", ",")  # normalise full-width commas
    items = [item.strip() for item in cleaned_str.split(",")]

    valid_items = []
    for item in items:
        if item in VALID_INSTRUCTS:
            valid_items.append(item)
        elif item.lower() in VALID_INSTRUCTS:
            valid_items.append(item.lower())

    if not valid_items:
        return None

    is_chinese = any('一' <= char <= '鿿' for char in valid_items[0])
    return ("，" if is_chinese else ", ").join(valid_items)


# ==============================================================================
# Emotion / tone tag mapping (Part B)
# ==============================================================================
# OmniVoice has no native emotion conditioning, so [sad]/[excited]/etc. are mapped onto the
# acoustic knobs it DOES honour (pitch via instruct, speaking rate, sampling temperature).
# These are approximations — tune the multipliers by ear. Non-verbal events like [laughs]/
# [sighs] are intentionally absent; OmniVoice cannot produce them (see api_documentation.md).
EMOTION_MAP: Dict[str, Dict[str, Any]] = {
    "sad":      {"instruct": "low pitch",      "speed_mult": 0.92, "class_temperature": 0.20},
    "excited":  {"instruct": "high pitch",     "speed_mult": 1.10, "class_temperature": 0.35},
    "happy":    {"instruct": "high pitch",     "speed_mult": 1.05, "class_temperature": 0.30},
    "cheerful": {"instruct": "high pitch",     "speed_mult": 1.05, "class_temperature": 0.30},
    "angry":    {"instruct": "low pitch",      "speed_mult": 1.05, "class_temperature": 0.35},
    "calm":     {"instruct": "moderate pitch", "speed_mult": 0.95, "class_temperature": 0.20},
    "serious":  {"instruct": "low pitch",      "speed_mult": 0.97},
    "fearful":  {"instruct": "high pitch",     "speed_mult": 1.08, "class_temperature": 0.30},
    "whisper":  {"instruct": "whisper",        "speed_mult": 0.95},
    "shouting": {"instruct": "high pitch",     "speed_mult": 1.05, "class_temperature": 0.35},
    "fast":     {"speed_mult": 1.15},
    "slow":     {"speed_mult": 0.85},
}

SUPPORTED_EMOTIONS = sorted(EMOTION_MAP.keys())


def resolve_style(tag: Optional[str],
                  base_speed: Optional[float],
                  base_class_temp: Optional[float],
                  base_pos_temp: Optional[float]) -> Dict[str, Any]:
    """
    Translate a raw bracket tag (e.g. "sad", "excited, fast", "british accent") into concrete
    generation params. Emotion tokens apply EMOTION_MAP adjustments; accent/pitch tokens pass
    through filter_instruct; unknown tokens are ignored. Returns a dict with keys
    {instruct, speed, class_temperature, position_temperature}.
    """
    instruct_parts: List[str] = []
    speed_mult = 1.0
    class_temp = base_class_temp
    pos_temp = base_pos_temp

    if tag:
        for raw in str(tag).replace("，", ",").split(","):
            token = raw.strip()
            key = token.lower()
            if key in EMOTION_MAP:
                e = EMOTION_MAP[key]
                if e.get("instruct"):
                    instruct_parts.append(e["instruct"])
                if "speed_mult" in e:
                    speed_mult *= e["speed_mult"]
                if e.get("class_temperature") is not None:
                    class_temp = e["class_temperature"]
                if e.get("position_temperature") is not None:
                    pos_temp = e["position_temperature"]
            else:
                # Accent/pitch/age/gender words handled by filter_instruct; junk dropped.
                instruct_parts.append(token)

    combined = filter_instruct(", ".join(dict.fromkeys(instruct_parts))) if instruct_parts else None

    final_speed = base_speed
    if speed_mult != 1.0:
        final_speed = (base_speed if base_speed is not None else 1.0) * speed_mult

    return {
        "instruct": combined,
        "speed": final_speed,
        "class_temperature": class_temp,
        "position_temperature": pos_temp,
    }


# ==============================================================================
# Number → words normalisation
#   Latin digits (0-9)      -> English words   (2  -> "two",  16 -> "sixteen")
#   Devanagari digits (०-९) -> Nepali words    (२  -> "दुई",  १६ -> "सोह्र")
# Both paths are pure table lookups + arithmetic, so they add no model latency.
# ==============================================================================
ONES = {
    0: "zero", 1: "one", 2: "two", 3: "three", 4: "four",
    5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine",
    10: "ten", 11: "eleven", 12: "twelve", 13: "thirteen", 14: "fourteen",
    15: "fifteen", 16: "sixteen", 17: "seventeen", 18: "eighteen", 19: "nineteen",
}
TENS = {
    2: "twenty", 3: "thirty", 4: "forty", 5: "fifty",
    6: "sixty", 7: "seventy", 8: "eighty", 9: "ninety",
}


def contains_devanagari(text: str) -> bool:
    """Detect any Devanagari script characters (U+0900–U+097F)."""
    return any('ऀ' <= char <= 'ॿ' for char in text)


def _int_to_words(n: int) -> str:
    if n < 20:
        return ONES[n]
    if n < 100:
        tens_val = n // 10
        ones_val = n % 10
        return TENS[tens_val] + ("-" + ONES[ones_val] if ones_val > 0 else "")
    if n < 1000:
        hundreds_val = n // 100
        rem = n % 100
        return ONES[hundreds_val] + " hundred" + (" " + _int_to_words(rem) if rem > 0 else "")
    if n < 1000000:
        thousands_val = n // 1000
        rem = n % 1000
        return _int_to_words(thousands_val) + " thousand" + (" " + _int_to_words(rem) if rem > 0 else "")
    if n < 1000000000:
        millions_val = n // 1000000
        rem = n % 1000000
        return _int_to_words(millions_val) + " million" + (" " + _int_to_words(rem) if rem > 0 else "")
    billions_val = n // 1000000000
    rem = n % 1000000000
    return _int_to_words(billions_val) + " billion" + (" " + _int_to_words(rem) if rem > 0 else "")


def _digits_to_words(digits_str: str) -> str:
    digit_words = {
        '0': 'zero', '1': 'one', '2': 'two', '3': 'three', '4': 'four',
        '5': 'five', '6': 'six', '7': 'seven', '8': 'eight', '9': 'nine',
    }
    return " ".join(digit_words[d] for d in digits_str)


def _cardinal_to_ordinal(cardinal_str: str) -> str:
    parts = re.split(r'(\s+|-)', cardinal_str)
    for i in range(len(parts) - 1, -1, -1):
        word = parts[i].lower()
        if word.isalpha():
            if word == "zero":
                ord_word = "zeroth"
            elif word == "one":
                ord_word = "first"
            elif word == "two":
                ord_word = "second"
            elif word == "three":
                ord_word = "third"
            elif word == "five":
                ord_word = "fifth"
            elif word == "eight":
                ord_word = "eighth"
            elif word == "nine":
                ord_word = "ninth"
            elif word == "twelve":
                ord_word = "twelfth"
            elif word.endswith("y"):
                ord_word = word[:-1] + "ieth"
            else:
                ord_word = word + "th"

            if parts[i].isupper():
                ord_word = ord_word.upper()
            elif parts[i][0].isupper():
                ord_word = ord_word.capitalize()

            parts[i] = ord_word
            break
    return "".join(parts)


def _integer_to_words_wrapper(num_str: str) -> str:
    if len(num_str) >= 6 or (len(num_str) > 1 and num_str.startswith('0')):
        return _digits_to_words(num_str)
    return _int_to_words(int(num_str))


def _decimal_to_words(num_str: str) -> str:
    if '.' not in num_str:
        return _integer_to_words_wrapper(num_str)
    integer_part, decimal_part = num_str.split('.')
    return f"{_integer_to_words_wrapper(integer_part)} point {_digits_to_words(decimal_part)}"


# ------------------------------------------------------------------------------
# Devanagari (Nepali) numerals -> Nepali words
# The flow-matching model mispronounces raw Devanagari digits, so we spell them
# out in Nepali (e.g. "२" -> "दुई", "१६" -> "सोह्र") rather than reading the glyph.
# ------------------------------------------------------------------------------
_DEV_TO_ASCII_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")

# Nepali cardinals are irregular through 99, so they need an explicit table.
NEPALI_CARDINALS = {
    0: "शून्य", 1: "एक", 2: "दुई", 3: "तीन", 4: "चार", 5: "पाँच", 6: "छ",
    7: "सात", 8: "आठ", 9: "नौ", 10: "दस", 11: "एघार", 12: "बाह्र", 13: "तेह्र",
    14: "चौध", 15: "पन्ध्र", 16: "सोह्र", 17: "सत्र", 18: "अठार", 19: "उन्नाइस",
    20: "बीस", 21: "एक्काइस", 22: "बाइस", 23: "तेइस", 24: "चौबिस", 25: "पच्चिस",
    26: "छब्बिस", 27: "सत्ताइस", 28: "अठ्ठाइस", 29: "उनन्तिस", 30: "तीस",
    31: "एकतिस", 32: "बत्तिस", 33: "तेत्तिस", 34: "चौँतिस", 35: "पैँतिस",
    36: "छत्तिस", 37: "सैँतिस", 38: "अठतीस", 39: "उनन्चालिस", 40: "चालिस",
    41: "एकचालिस", 42: "बयालिस", 43: "त्रिचालिस", 44: "चवालिस", 45: "पैँतालिस",
    46: "छयालिस", 47: "सच्चालिस", 48: "अठचालिस", 49: "उनन्चास", 50: "पचास",
    51: "एकाउन्न", 52: "बाउन्न", 53: "त्रिपन्न", 54: "चवन्न", 55: "पचपन्न",
    56: "छपन्न", 57: "सन्ताउन्न", 58: "अन्ठाउन्न", 59: "उनन्साठी", 60: "साठी",
    61: "एकसट्ठी", 62: "बयसट्ठी", 63: "त्रिसट्ठी", 64: "चौंसट्ठी", 65: "पैंसट्ठी",
    66: "छयसट्ठी", 67: "सतसट्ठी", 68: "अठसट्ठी", 69: "उनन्सत्तरी", 70: "सत्तरी",
    71: "एकहत्तर", 72: "बहत्तर", 73: "त्रिहत्तर", 74: "चौहत्तर", 75: "पचहत्तर",
    76: "छयहत्तर", 77: "सतहत्तर", 78: "अठहत्तर", 79: "उनासी", 80: "असी",
    81: "एकासी", 82: "बयासी", 83: "त्रियासी", 84: "चौरासी", 85: "पचासी",
    86: "छयासी", 87: "सतासी", 88: "अठासी", 89: "उनान्नब्बे", 90: "नब्बे",
    91: "एकानब्बे", 92: "बयानब्बे", 93: "त्रियानब्बे", 94: "चौरानब्बे",
    95: "पन्चानब्बे", 96: "छयानब्बे", 97: "सन्तानब्बे", 98: "अन्ठानब्बे",
    99: "उनान्सय",
}
# Indian/Nepali place-value grouping: सय (100) हजार (1k) लाख (100k) करोड (10M).
_NEPALI_SCALES = [(10_000_000, "करोड"), (100_000, "लाख"), (1000, "हजार"), (100, "सय")]


def _nepali_int_to_words(n: int) -> str:
    if n < 100:
        return NEPALI_CARDINALS[n]
    parts: List[str] = []
    for value, name in _NEPALI_SCALES:
        if n >= value:
            count = n // value
            n %= value
            # करोड may carry a multiplier > 99 (recurse); सय/हजार/लाख stay 1–99.
            parts.append(_nepali_int_to_words(count) + " " + name)
    if n:
        parts.append(NEPALI_CARDINALS[n])
    return " ".join(parts)


def _nepali_digits_to_words(digits_str: str) -> str:
    return " ".join(NEPALI_CARDINALS[int(d)] for d in digits_str)


def _nepali_integer_to_words_wrapper(num_str: str) -> str:
    # Long or zero-padded runs read digit-by-digit (phone numbers, codes, etc.).
    if len(num_str) >= 6 or (len(num_str) > 1 and num_str.startswith('0')):
        return _nepali_digits_to_words(num_str)
    return _nepali_int_to_words(int(num_str))


def _nepali_decimal_to_words(num_str: str) -> str:
    if '.' not in num_str:
        return _nepali_integer_to_words_wrapper(num_str)
    integer_part, decimal_part = num_str.split('.')
    return f"{_nepali_integer_to_words_wrapper(integer_part)} दशमलव {_nepali_digits_to_words(decimal_part)}"


def normalize_devanagari_numbers(text: str) -> str:
    """Convert Devanagari digit runs (०-९) to spoken Nepali words; leaves Latin digits alone."""
    text = re.sub(r'(?<=[०-९]),(?=[०-९])', '', text)  # १,२५० -> १२५०
    text = re.sub(
        r'[०-९]+\.[०-९]+',
        lambda m: _nepali_decimal_to_words(m.group(0).translate(_DEV_TO_ASCII_DIGITS)),
        text,
    )
    text = re.sub(
        r'[०-९]+',
        lambda m: _nepali_integer_to_words_wrapper(m.group(0).translate(_DEV_TO_ASCII_DIGITS)),
        text,
    )
    return text


def normalize_text_numbers(text: str) -> str:
    """Spell out digits: Latin 0-9 -> English words, Devanagari ०-९ -> Nepali words."""
    text = normalize_devanagari_numbers(text)
    text = re.sub(r'(?<=[0-9]),(?=[0-9])', '', text)  # 1,000 -> 1000

    def replace_ordinal(match):
        return _cardinal_to_ordinal(_integer_to_words_wrapper(match.group(1)))

    text = re.sub(r'\b([0-9]+)(st|nd|rd|th)\b', replace_ordinal, text, flags=re.IGNORECASE)
    text = re.sub(r'\b([0-9]+\.[0-9]+)\b', lambda m: _decimal_to_words(m.group(1)), text)
    text = re.sub(r'\b([0-9]+)\b', lambda m: _integer_to_words_wrapper(m.group(1)), text)
    return text


# ==============================================================================
# Full text normalisation & sentence chunking (Part C)
# ==============================================================================
_WS_RE = re.compile(r'\s+')
# Unambiguous symbols expanded so the model gets a consistent spoken cue instead of raw glyphs.
_SYMBOL_MAP = [('%', ' percent '), ('&', ' and '), ('@', ' at '), ('+', ' plus '), ('=', ' equals ')]
_QUOTE_MAP = [('“', '"'), ('”', '"'), ('‘', "'"), ('’', "'"), ('—', '-'), ('–', '-'), ('…', '...')]
_SENTENCE_SPLIT_RE = re.compile(r'(?<=[.!?।])\s+')


def normalize_text(text: Optional[str]) -> str:
    """
    Clean text before synthesis: normalise quotes/dashes, expand a few symbols, convert numbers,
    collapse whitespace, and ensure terminal punctuation (helps the model's sentence prosody).
    """
    if not text:
        return ""
    for src, dst in _QUOTE_MAP:
        text = text.replace(src, dst)
    for src, dst in _SYMBOL_MAP:
        text = text.replace(src, dst)
    text = normalize_text_numbers(text)
    text = _WS_RE.sub(' ', text).strip()
    if text and text[-1] not in '.!?।,;:':
        text = text + ('।' if contains_devanagari(text) else '.')
    return text


def split_sentences(text: str) -> List[str]:
    """Split on sentence terminators (. ! ? and the Devanagari danda ।), keeping the terminator."""
    text = text.strip()
    if not text:
        return []
    return [p.strip() for p in _SENTENCE_SPLIT_RE.split(text) if p.strip()]


def chunk_text(text: str, max_chars: int = 240) -> List[str]:
    """
    Break long text into coherent, sentence-aligned chunks (greedy merge up to max_chars) so
    flow-matching prosody stays stable instead of degrading on one oversized generation.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    sentences = split_sentences(text)
    if not sentences:
        return [text]
    chunks: List[str] = []
    current = ""
    for s in sentences:
        if not current:
            current = s
        elif len(current) + 1 + len(s) <= max_chars:
            current = current + " " + s
        else:
            chunks.append(current)
            current = s
    if current:
        chunks.append(current)
    return chunks


def parse_emotion_segments(text: str, default_instruct: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Split text on emotion/style tags like [happy] or [sad, fast] into segments, each carrying the
    active tag under "instruct" (raw tag string; resolved later by resolve_style).
    Example: "I am [happy] glad, but [sad] sorry." -> three segments.
    """
    tag_pattern = re.compile(r"\[([a-zA-Z\s,]+)\]")
    matches = list(tag_pattern.finditer(text))
    if not matches:
        return [{"text": text, "instruct": default_instruct}]

    segments = []
    last_idx = 0
    current_instruct = default_instruct
    for match in matches:
        start_idx, end_idx = match.span()
        segment_text = text[last_idx:start_idx]
        if segment_text.strip():
            segments.append({"text": segment_text, "instruct": current_instruct})
        current_instruct = match.group(1).strip()
        last_idx = end_idx
    segment_text = text[last_idx:]
    if segment_text.strip():
        segments.append({"text": segment_text, "instruct": current_instruct})
    return segments


# ==============================================================================
# Audio encoding (Part A1: selectable output fidelity)
# ==============================================================================
# label is what we report back so callers know exactly what they received.
OUTPUT_FORMATS: Dict[str, Dict[str, Any]] = {
    "telephony": {"sr": 8000, "container": "WAV", "subtype": "ULAW", "label": "8kHz_ulaw"},
    "hd":        {"sr": NATIVE_SR, "container": "WAV", "subtype": "PCM_16", "label": "24kHz_pcm16"},
    "hd_wav":    {"sr": NATIVE_SR, "container": "WAV", "subtype": "PCM_16", "label": "24kHz_pcm16"},
    "hd_flac":   {"sr": NATIVE_SR, "container": "FLAC", "subtype": "PCM_16", "label": "24kHz_flac"},
    "hd_opus":   {"sr": NATIVE_SR, "container": "OGG", "subtype": "OPUS", "label": "24kHz_opus"},
    "hd_mp3":    {"sr": NATIVE_SR, "container": "MP3", "subtype": None, "label": "24kHz_mp3"},
}
DEFAULT_OUTPUT_FORMAT = "telephony"


def _to_mono_numpy(audio_array) -> np.ndarray:
    """Convert torch tensor / list / ndarray to a 1-D float32 numpy mono array."""
    if isinstance(audio_array, torch.Tensor):
        audio_array = audio_array.detach().to(torch.float32).cpu().numpy()
    elif not isinstance(audio_array, np.ndarray):
        audio_array = np.array(audio_array, dtype=np.float32)
    if audio_array.ndim > 1:
        audio_array = audio_array[0]
    return audio_array.astype(np.float32, copy=False)


def _concat_with_pauses(audios: List[Any], sample_rate: int = NATIVE_SR, pause_ms: int = 140) -> np.ndarray:
    """Concatenate audio chunks with a short natural silence between them (Part C2 stitching)."""
    pause = np.zeros(int(sample_rate * pause_ms / 1000.0), dtype=np.float32)
    pieces: List[np.ndarray] = []
    for i, a in enumerate(audios):
        if a is None:
            continue
        arr = _to_mono_numpy(a)
        if len(arr) == 0:
            continue
        if pieces:
            pieces.append(pause)
        pieces.append(arr)
    return np.concatenate(pieces) if pieces else np.zeros(0, dtype=np.float32)


def _encode_audio(audio: np.ndarray, sr: int, spec: Dict[str, Any]) -> Tuple[str, str]:
    """Encode a mono float32 array to the requested container; falls back to WAV/PCM16."""
    container = spec["container"]
    label = spec["label"]

    if container == "MP3":
        try:
            from pydub import AudioSegment
            pcm16 = np.clip(audio * 32767.0, -32768, 32767).astype(np.int16)
            seg = AudioSegment(pcm16.tobytes(), frame_rate=sr, sample_width=2, channels=1)
            buffer = io.BytesIO()
            seg.export(buffer, format="mp3", bitrate="64k")
            return base64.b64encode(buffer.getvalue()).decode('utf-8'), label
        except Exception as e:
            logger.warning("MP3 encode failed (%s); falling back to WAV PCM16.", e)
            container, label = "WAV", f"{sr // 1000}kHz_pcm16"
            spec = {"container": "WAV", "subtype": "PCM_16", "label": label}

    buffer = io.BytesIO()
    try:
        sf.write(buffer, audio, sr, format=container, subtype=spec.get("subtype"))
    except Exception as e:
        logger.warning("%s/%s encode failed (%s); falling back to WAV PCM16.",
                       container, spec.get("subtype"), e)
        buffer = io.BytesIO()
        sf.write(buffer, audio, sr, format="WAV", subtype="PCM_16")
        label = f"{sr // 1000}kHz_pcm16"
    return base64.b64encode(buffer.getvalue()).decode('utf-8'), label


def process_audio(audio_array, output_format: str = DEFAULT_OUTPUT_FORMAT) -> Tuple[str, int, str]:
    """
    Resample (only when needed) and encode model output to the requested format.
    Returns (base64_audio, sample_rate, format_label).

    - telephony -> 24kHz downsampled to 8kHz G.711 µ-law (GPU resampler fast-path when available)
    - hd / hd_wav / hd_flac / hd_opus / hd_mp3 -> kept at native 24kHz (no fidelity loss)
    """
    spec = OUTPUT_FORMATS.get((output_format or DEFAULT_OUTPUT_FORMAT).lower())
    if spec is None:
        raise TTSInputError(
            f"Unknown output_format '{output_format}'. Valid: {', '.join(sorted(OUTPUT_FORMATS))}."
        )
    target_sr = spec["sr"]
    try:
        if audio_array is None or (hasattr(audio_array, "__len__") and len(audio_array) == 0):
            raise TTSServerError("The provided audio array is empty or None.")

        # Telephony fast-path: resample 24k -> 8k on GPU when available.
        if target_sr == 8000 and gpu_resampler is not None:
            if isinstance(audio_array, torch.Tensor):
                t = audio_array.to(DEVICE)
                if t.ndim > 1:
                    t = t[0]
            else:
                t = torch.tensor(_to_mono_numpy(audio_array), dtype=torch.float32, device=DEVICE)
            with torch.inference_mode():
                audio = gpu_resampler(t).cpu().numpy()
        else:
            audio = _to_mono_numpy(audio_array)
            if target_sr != NATIVE_SR:
                audio = librosa.resample(audio, orig_sr=NATIVE_SR, target_sr=target_sr)

        audio = np.clip(audio, -1.0, 1.0)
        b64, label = _encode_audio(audio, target_sr, spec)
        return b64, target_sr, label
    except TTSError:
        raise
    except Exception as e:
        logger.error("Audio processing/codec conversion failed: %s", e)
        raise TTSServerError(f"Audio processing/codec conversion failed: {e}")


def process_to_ulaw(audio_array, orig_sr: int = NATIVE_SR, target_sr: int = 8000) -> str:
    """Backwards-compatible 8kHz µ-law encoder (telephony). Kept for existing callers/tests."""
    b64, _, _ = process_audio(audio_array, "telephony")
    return b64


# ==============================================================================
# Voice profile cache (L1 memory + L2 disk) and reference-audio handling
# ==============================================================================
# The cache location and filename derivation live in [voices.py] — the registry that also owns the
# ownership / public / per-tenant sharing metadata — so there is exactly one source of truth. They
# are re-exported here so existing callers/tests can keep using tts_core.VOICE_PROFILES_DIR /
# tts_core.get_profile_filepath unchanged.
VOICE_PROFILES_DIR = voices.VOICE_PROFILES_DIR
get_profile_filepath = voices.get_profile_filepath

# SSRF / download guards for reference audio (Part E4)
REF_AUDIO_MAX_BYTES = int(os.getenv("REF_AUDIO_MAX_BYTES", str(25 * 1024 * 1024)))
REF_AUDIO_ALLOW_INSECURE = os.getenv("ALLOW_INSECURE_REF_URL", "false").lower() in ("1", "true", "yes")


def preprocess_reference_audio(input_path: str, output_path: str) -> None:
    """Trim silence and peak-normalise reference audio to 0.95 for consistent cloning."""
    logger.info("Preprocessing reference audio.")
    y, sr = librosa.load(input_path, sr=None, mono=True)
    if len(y) == 0:
        raise TTSInputError("Reference audio is empty.")
    y_trimmed, _ = librosa.effects.trim(y, top_db=40)
    if len(y_trimmed) == 0:
        y_trimmed = y
    max_val = np.max(np.abs(y_trimmed))
    y_normalized = y_trimmed * (0.95 / max_val) if max_val > 0 else y_trimmed
    sf.write(output_path, y_normalized, sr, format='WAV', subtype='PCM_16')


def _assert_public_host(host: str) -> None:
    """Reject hosts that resolve to private/loopback/link-local/reserved IPs (SSRF guard)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        raise TTSInputError(f"Could not resolve reference audio host: {host}")
    for info in infos:
        ip = info[4][0]
        try:
            ip_obj = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if (ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local
                or ip_obj.is_reserved or ip_obj.is_multicast or ip_obj.is_unspecified):
            raise TTSInputError("Reference audio URL resolves to a disallowed (non-public) address.")


def download_audio(url: str, temp_dir: str) -> str:
    """
    Download reference audio with SSRF protection: https(-only by default), public-IP only,
    content-type allowlist, and a streaming size cap. Returns the local file path.

    Note: this is best-effort (DNS rebinding / redirect-to-internal is not fully closed here);
    a hardened deployment should also restrict egress at the network/gateway layer.
    """
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise TTSInputError("Reference audio URL must use http or https.")
    if scheme != "https" and not REF_AUDIO_ALLOW_INSECURE:
        raise TTSInputError("Reference audio URL must use https (set ALLOW_INSECURE_REF_URL to override).")
    if not parsed.hostname:
        raise TTSInputError("Reference audio URL is missing a host.")
    _assert_public_host(parsed.hostname)

    logger.info("Downloading reference audio from host: %s", parsed.hostname)
    try:
        response = requests.get(url, stream=True, timeout=30)
        response.raise_for_status()
    except TTSError:
        raise
    except Exception as e:
        raise TTSInputError(f"Failed to download reference audio: {e}")

    ctype = response.headers.get("Content-Type", "").lower()
    if ctype and not (ctype.startswith("audio/") or "octet-stream" in ctype or "application/ogg" in ctype):
        raise TTSInputError(f"Reference audio URL returned unsupported content-type: {ctype}")

    declared = response.headers.get("Content-Length")
    if declared and declared.isdigit() and int(declared) > REF_AUDIO_MAX_BYTES:
        raise TTSInputError("Reference audio exceeds the maximum allowed size.")

    filename = os.path.basename(parsed.path) or ""
    if not filename.endswith(".wav"):
        filename = f"ref_{uuid.uuid4().hex}.wav"
    local_path = os.path.join(temp_dir, filename)

    total = 0
    try:
        with open(local_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if not chunk:
                    continue
                total += len(chunk)
                if total > REF_AUDIO_MAX_BYTES:
                    raise TTSInputError("Reference audio exceeds the maximum allowed size.")
                f.write(chunk)
    except TTSError:
        raise
    except Exception as e:
        raise TTSServerError(f"Failed to write downloaded reference audio: {e}")
    return local_path


class VoiceProfileCache:
    """LRU cache of voice-clone prompts kept in VRAM/RAM (L1).

    Entries can be *pinned* (e.g. operator-curated persistent voices warm-loaded at startup): pinned
    entries are exempt from LRU eviction and do not count toward `max_size`, so the regular cache of
    ad-hoc clones keeps churning around them.
    """

    def __init__(self, max_size: int = 20):
        self.cache: "OrderedDict[str, Any]" = OrderedDict()
        self.pinned: set = set()
        self.max_size = max_size

    def get(self, key: str) -> Optional[Any]:
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        return None

    def set(self, key: str, value: Any, pin: bool = False) -> None:
        if key not in self.cache and not pin:
            self._evict_to_capacity()
        self.cache[key] = value
        self.cache.move_to_end(key)
        if pin:
            self.pinned.add(key)

    def _evict_to_capacity(self) -> None:
        """Evict the oldest *non-pinned* entries until there is room for one more non-pinned entry."""
        non_pinned = [k for k in self.cache if k not in self.pinned]
        while len(non_pinned) >= self.max_size:
            oldest_key = non_pinned.pop(0)
            oldest_val = self.cache.pop(oldest_key)
            logger.info("Evicting voice profile from L1 cache: %s", oldest_key)
            del oldest_val
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            gc.collect()

    def clear(self) -> None:
        self.cache.clear()
        self.pinned.clear()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        gc.collect()


voice_profile_cache = VoiceProfileCache(max_size=20)


def _register_cached_profile(cache_key: Optional[str], voice_profile_id: Optional[str],
                             owner_tenant_id: Optional[str],
                             ref_audio_url: Optional[str] = None) -> None:
    """Best-effort registration of any cached voice so it shows up in management + warm-load.

    Registers whatever key the clone is cached under — a named `voice_profile_id`, or failing that
    the `ref_audio_url` — regardless of whether the call was authenticated (owner may be None, e.g.
    the RunPod serverless handler or auth-disabled dev). The `ref_audio_url` is recorded so operators
    can preview the voice in the admin console. Without this, cloned voices stay invisible in the
    admin console and ineligible for persist / warm-load. Idempotent; never breaks synthesis.
    """
    if not cache_key:
        return
    try:
        voices.register(cache_key, owner_tenant_id=owner_tenant_id,
                        source="clone" if voice_profile_id else "url",
                        ref_audio_url=ref_audio_url or "")
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to register voice profile '%s': %s", cache_key, e)


def warm_load_persistent_profiles() -> int:
    """Scan the registry for profiles marked *persistent* and load their cached tensors into L1 (pinned).

    Called once at startup (FastAPI lifespan / RunPod warm-boot) so operator-curated voices come back
    already warm after a restart — no re-clone, no first-request disk load. Profiles whose .pt is not
    on disk yet are skipped (they will clone on first use). Never raises; returns the count loaded.
    """
    loaded = 0
    try:
        persistent = voices.list_persistent()
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not read persistent voice profiles for warm-load: %s", e)
        return 0
    for rec in persistent:
        profile_id = rec["profile_id"]
        path = voices.find_cached_file(profile_id)
        if not path:
            logger.info("Persistent profile '%s' has no cached file yet; will clone on first use.",
                        profile_id)
            continue
        try:
            prompt = torch.load(path, map_location=DEVICE)
            voice_profile_cache.set(profile_id, prompt, pin=True)
            loaded += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to warm-load persistent profile '%s': %s", profile_id, e)
    if loaded:
        logger.info("Warm-loaded %d persistent voice profile(s) into L1 cache (pinned).", loaded)
    return loaded


def get_or_create_voice_prompt(model: Any,
                               voice_profile_id: Optional[str],
                               ref_audio_url: Optional[str],
                               ref_text: Optional[str] = None,
                               owner_tenant_id: Optional[str] = None) -> Optional[Any]:
    """Resolve a voice-clone prompt via L1 memory / L2 disk cache, cloning from ref audio on miss.

    When a *named* profile is freshly cloned and `owner_tenant_id` is supplied (an authenticated
    tenant), the profile is registered in [voices.py] so it can be listed and managed. Access control
    is enforced by the caller ([app.py]) before this runs.
    """
    if model is None:
        raise TTSServerError("Model is not initialized on the server.")

    cache_key = voice_profile_id or ref_audio_url
    if not cache_key:
        return None

    cached_prompt = voice_profile_cache.get(cache_key)
    if cached_prompt is not None:
        logger.info("L1 cache hit for voice profile.")
        return cached_prompt

    cached_file = voices.find_cached_file(cache_key)  # working dir, then the persist volume
    if cached_file:
        try:
            logger.info("L2 disk cache hit for voice profile.")
            prompt = torch.load(cached_file, map_location=DEVICE)
            voice_profile_cache.set(cache_key, prompt)
            _register_cached_profile(cache_key, voice_profile_id, owner_tenant_id, ref_audio_url)
            return prompt
        except Exception as e:
            logger.warning("Failed to load voice profile from disk (%s); regenerating.", e)

    if not ref_audio_url:
        raise TTSInputError(
            "Cache miss for voice profile, but no 'ref_audio_url' was provided to clone."
        )

    logger.info("Cloning new voice profile.")
    with tempfile.TemporaryDirectory() as temp_dir:
        local_ref_path = download_audio(ref_audio_url, temp_dir)
        preprocessed_ref_path = os.path.join(temp_dir, "preprocessed_ref.wav")
        try:
            preprocess_reference_audio(local_ref_path, preprocessed_ref_path)
            with torch.inference_mode():
                prompt = model.create_voice_clone_prompt(
                    ref_audio=preprocessed_ref_path,
                    ref_text=ref_text,
                )
        except TTSError:
            raise
        except Exception as e:
            raise TTSServerError(f"Failed to create voice clone prompt: {e}")
        finally:
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            gc.collect()

    filepath = get_profile_filepath(cache_key)
    try:
        torch.save(prompt, filepath)
        logger.info("Saved voice profile to L2 disk cache.")
    except Exception as e:
        logger.warning("Failed to save voice profile to disk: %s", e)

    # Register the freshly-cloned voice so it can be listed/managed/persisted (idempotent).
    _register_cached_profile(cache_key, voice_profile_id, owner_tenant_id, ref_audio_url)

    voice_profile_cache.set(cache_key, prompt)
    return prompt


# ==============================================================================
# Inference & generation orchestration (model injected)
# ==============================================================================
def _run_inference(model: Any,
                   text: Union[str, List[str]],
                   voice_clone_prompt: Optional[Any] = None,
                   num_step: int = 32,
                   speed: Optional[float] = None,
                   guidance_scale: Optional[float] = None,
                   position_temperature: Optional[float] = 5.0,
                   class_temperature: Optional[float] = 0.25,
                   instruct: Optional[str] = None,
                   seed: Optional[int] = None) -> Any:
    """Run model.generate with the given (already-resolved) params. Always returns a batch list."""
    if model is None:
        raise TTSServerError("Model is not initialized on the server.")

    if seed is not None:
        torch.manual_seed(int(seed))
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(int(seed))

    gen_kwargs: Dict[str, Any] = {"text": text, "num_step": num_step}
    if voice_clone_prompt is not None:
        gen_kwargs["voice_clone_prompt"] = voice_clone_prompt
    if speed is not None:
        gen_kwargs["speed"] = speed
    if guidance_scale is not None:
        gen_kwargs["guidance_scale"] = guidance_scale
    if position_temperature is not None:
        gen_kwargs["position_temperature"] = position_temperature
    if class_temperature is not None:
        gen_kwargs["class_temperature"] = class_temperature
    valid_instruct = filter_instruct(instruct)
    if valid_instruct is not None:
        gen_kwargs["instruct"] = valid_instruct

    try:
        with torch.inference_mode():
            if torch.cuda.is_available():
                stream = torch.cuda.Stream()
                with torch.cuda.stream(stream):
                    audio_array = model.generate(**gen_kwargs)
                stream.synchronize()
            else:
                audio_array = model.generate(**gen_kwargs)
    except Exception as e:
        raise TTSServerError(f"Model generation failed: {e}")

    if audio_array is None or len(audio_array) == 0:
        raise TTSServerError("Model inference returned an empty audio array.")
    return audio_array


def generate_audio(model: Any,
                   text: str,
                   voice_clone_prompt: Optional[Any] = None,
                   num_step: int = 32,
                   speed: Optional[float] = None,
                   guidance_scale: Optional[float] = None,
                   position_temperature: Optional[float] = 5.0,
                   class_temperature: Optional[float] = 0.25,
                   instruct: Optional[str] = None,
                   seed: Optional[int] = None,
                   output_format: str = DEFAULT_OUTPUT_FORMAT,
                   max_chunk_chars: int = 240,
                   pause_ms: int = 140) -> Tuple[str, int, str]:
    """
    Single-text synthesis with emotion-tag handling, sentence chunking, style-grouped batching,
    and natural pause stitching. Returns (base64_audio, sample_rate, format_label).

    Units that share the same resolved style (the common no-tag case = all of them) are generated
    in ONE batched call (true CUDA batching) instead of N independent ThreadPoolExecutor calls.
    """
    segments = parse_emotion_segments(text, default_instruct=instruct)

    # Build ordered "units" (one per sentence chunk), each tagged with its resolved style.
    units: List[Dict[str, Any]] = []
    for seg in segments:
        seg_text = normalize_text(seg["text"])
        if not seg_text:
            continue
        style = resolve_style(seg.get("instruct"), speed, class_temperature, position_temperature)
        for chunk in chunk_text(seg_text, max_chunk_chars):
            units.append({"text": chunk, "style": style})

    if not units:
        raise TTSInputError("No synthesizable text after normalization.")

    # Group units by identical style so each group can be batched in a single generate() call.
    audios: List[Any] = [None] * len(units)
    groups: "OrderedDict[Tuple, List[int]]" = OrderedDict()
    for idx, u in enumerate(units):
        s = u["style"]
        key = (s["instruct"], s["speed"], s["class_temperature"], s["position_temperature"])
        groups.setdefault(key, []).append(idx)

    for (instruct_v, speed_v, ctemp_v, ptemp_v), idxs in groups.items():
        batch_texts = [units[i]["text"] for i in idxs]
        out = _run_inference(
            model,
            text=batch_texts,
            voice_clone_prompt=voice_clone_prompt,
            num_step=num_step,
            speed=speed_v,
            guidance_scale=guidance_scale,
            position_temperature=ptemp_v,
            class_temperature=ctemp_v,
            instruct=instruct_v,
            seed=seed,
        )
        for local_i, global_i in enumerate(idxs):
            audios[global_i] = out[local_i]

    final_audio = audios[0] if len(audios) == 1 else _concat_with_pauses(
        audios, sample_rate=NATIVE_SR, pause_ms=pause_ms
    )
    return process_audio(final_audio, output_format=output_format)


def generate_bulk(model: Any,
                  template: str,
                  data: List[Dict[str, Any]],
                  voice_clone_prompt: Optional[Any] = None,
                  num_step: int = 32,
                  speed: Optional[float] = None,
                  guidance_scale: Optional[float] = None,
                  position_temperature: Optional[float] = 5.0,
                  class_temperature: Optional[float] = 0.25,
                  instruct: Optional[str] = None,
                  seed: Optional[int] = None,
                  output_format: str = DEFAULT_OUTPUT_FORMAT) -> List[Dict[str, Any]]:
    """Template-interpolated bulk synthesis using true tensor batching (batches of 8)."""
    if len(data) > MAX_BULK_ITEMS:
        raise TTSInputError(f"Bulk 'data' exceeds the maximum of {MAX_BULK_ITEMS} items.")

    logger.info("Starting batched bulk generation for %d items.", len(data))
    resolved_instruct = filter_instruct(instruct)

    tasks = []
    for index, item_data in enumerate(data):
        try:
            rendered = template.format(**item_data)
        except KeyError as ke:
            raise TTSInputError(f"Template formatting failed at index {index}. Missing variable: {ke}")
        except Exception as e:
            raise TTSInputError(f"Template formatting failed at index {index}: {e}")
        tasks.append({"payload_data": item_data, "text_generated": normalize_text(rendered)})

    results: List[Dict[str, Any]] = []
    batch_size = int(os.getenv("BULK_BATCH_SIZE", "8"))

    for i in range(0, len(tasks), batch_size):
        batch_tasks = tasks[i:i + batch_size]
        batch_texts = [t["text_generated"] for t in batch_tasks]
        logger.info("Processing batch %d/%d (size %d).",
                    i // batch_size + 1, (len(tasks) + batch_size - 1) // batch_size, len(batch_texts))
        try:
            audios = _run_inference(
                model,
                text=batch_texts,
                voice_clone_prompt=voice_clone_prompt,
                num_step=num_step,
                speed=speed,
                guidance_scale=guidance_scale,
                position_temperature=position_temperature,
                class_temperature=class_temperature,
                instruct=resolved_instruct,
                seed=seed,
            )
            for j, audio_data in enumerate(audios):
                task_data = batch_tasks[j]
                try:
                    b64, _, _ = process_audio(audio_data, output_format=output_format)
                    task_data["audio_base64"] = b64
                except Exception as codec_err:
                    task_data["error"] = f"Codec conversion failed: {codec_err}"
                results.append(task_data)
        except Exception as e:
            logger.error("Error processing batch %d: %s", i // batch_size + 1, e)
            for task_data in batch_tasks:
                task_data["error"] = f"Batch generation failed: {e}"
                results.append(task_data)

    return results
