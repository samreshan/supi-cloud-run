#!/bin/bash
set -euo pipefail

# Cloud Run injects PORT (default 8080) and routes exactly one port per service — there is no
# SSH access and no second admin port here, unlike the RunPod dedicated-pod deployment this repo
# was forked from. This same image backs TWO Cloud Run services by switching APP_MODULE:
#   - public TTS API (default):        APP_MODULE=app:app
#   - operator-only admin console:     APP_MODULE=console_app:console_app
# Deploy them as separate Cloud Run services from the same image/tag; the admin console should be
# deployed with --no-allow-unauthenticated (IAM-gated) or ingress restricted to internal traffic.
APP_MODULE=${APP_MODULE:-app:app}
PORT=${PORT:-8080}

# tcmalloc materially helps PyTorch's CPU allocation pattern vs glibc malloc, but the exact .so path
# is base-image-dependent (the upstream omniServerless repo hardcoded a path via ENV that silently
# didn't exist on its base image, so tcmalloc was never actually active). Detect it at runtime instead.
for tc in /usr/lib/x86_64-linux-gnu/libtcmalloc.so.4 /usr/lib/aarch64-linux-gnu/libtcmalloc.so.4; do
  if [ -f "$tc" ]; then
    export LD_PRELOAD="$tc"
    echo "Using tcmalloc: $tc"
    break
  fi
done

# Single worker: this app's tenant/credit accounting uses a SQLite store (see docs), which requires
# exactly one writer for correct accounting. Cloud Run's own per-instance `--concurrency` setting
# (recommend concurrency=1 for this CPU-bound synthesis workload — see README.md) governs how many
# requests reach this one process at a time; it does not need multiple uvicorn workers to do that.
echo "Starting ${APP_MODULE} on port ${PORT}..."
exec uvicorn "${APP_MODULE}" --host 0.0.0.0 --port "${PORT}" --workers 1
