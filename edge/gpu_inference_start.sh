#!/bin/bash
# Launch the GPU inference server (Whisper STT / TTS / Stable Diffusion).
#
# Why this wrapper exists rather than calling python directly — three things
# that each silently break startup and cost real time to diagnose:
#
#  1. HOME. On an UNPRIVILEGED LXC, /root is often owned by `nobody` and is
#     unreadable even by container-root (the uid falls outside the mapping).
#     Every model cache write then fails with EACCES and the app exits during
#     startup, before it binds a port — so it looks like "the server won't
#     start" rather than "a directory is unwritable".
#
#  2. HF_HOME. If this is not pointed at the cache that already holds the
#     models, HuggingFace re-downloads Stable Diffusion (~4GB) into a fresh
#     directory while the existing copy sits untouched on disk.
#
#  3. The venv. The dependencies (torch, kokoro-onnx, whisper, diffusers) live
#     in ./env, not in the system interpreter.
#
# Config comes from EnvironmentFile when run under systemd, or from the
# environment when run by hand. See vera-inference.env.example for the
# GPU-node and CPU-node profiles.
set -euo pipefail

APP_DIR="${INFER_APP_DIR:-/home/Servers/StableDiffustionWhisper}"
cd "$APP_DIR"

# A writable HOME. Never leave this as /root on an unprivileged container.
export HOME="${INFER_HOME:-$APP_DIR}"
export XDG_CACHE_HOME="${INFER_CACHE:-$APP_DIR/cache}"
export TORCH_HOME="${TORCH_HOME:-$XDG_CACHE_HOME/torch}"

# Point at the cache that ALREADY holds the models. Overriding this to a new
# path is what triggers a redundant multi-gigabyte download.
export HF_HOME="${HF_HOME:-/.cache/huggingface}"

mkdir -p "$XDG_CACHE_HOME" "$TORCH_HOME"

if [ ! -x "./env/bin/python3" ]; then
  echo "no venv at $APP_DIR/env — create it and install requirements.txt" >&2
  exit 1
fi

# shellcheck disable=SC1091
source ./env/bin/activate
exec python3 ./GPU_inference.py
