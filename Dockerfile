# Cloud Run / CPU-only fork of the omniServerless GPU image. No CUDA runtime, no SSH — Cloud Run
# routes exactly one port per service and provides no persistent shell access.
FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/app/.cache \
    MODELSCOPE_CACHE=/app/.cache/modelscope \
    PYTHONDONTWRITEBYTECODE=1

# libsndfile1 + ffmpeg: audio codec support (soundfile / pydub MP3 export).
# libgoogle-perftools4: tcmalloc (see start.sh for why it's runtime-detected, not hardcoded).
# git: required because requirements.txt pulls transformers from its GitHub 'main' branch.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libsndfile1 \
    ffmpeg \
    git \
    ca-certificates \
    libgoogle-perftools4 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# CPU-only torch/torchaudio wheels — these do NOT bundle CUDA libraries, so the image stays a
# fraction of the size of the GPU build (no cudnn/cublas/nccl, no cuda12.4 base layer).
RUN pip install --no-cache-dir \
    torch==2.5.1 torchaudio==2.5.1 \
    --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Bake model weights into the image at build time so a Cloud Run cold start never pays for a
# network download from Hugging Face — only local disk read + model init.
COPY preload.py /app/preload.py
RUN python /app/preload.py

# App code. tts_core holds the shared TTS logic; voices is the voice-profile registry; store/
# tenancy/credits/admin/security provide multi-tenant auth, metering, and admin API support;
# console_app is deployed as a SEPARATE Cloud Run service from this same image (see start.sh).
COPY voices.py /app/voices.py
COPY tts_core.py /app/tts_core.py
COPY store.py /app/store.py
COPY tenancy.py /app/tenancy.py
COPY credits.py /app/credits.py
COPY security.py /app/security.py
COPY admin.py /app/admin.py
COPY console_app.py /app/console_app.py
COPY app.py /app/app.py
COPY api_documentation.md /app/api_documentation.md
COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh

# Informational only — Cloud Run ignores EXPOSE and routes $PORT regardless.
EXPOSE 8080

CMD ["/app/start.sh"]
