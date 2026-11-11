#!/usr/bin/env bash
# One-command entrypoint: bootstrap the environment, seed demo data, serve the app.
# Equivalent to `make dev` for people who would rather not remember Make targets.
set -euo pipefail
cd "$(dirname "$0")"
bash scripts/bootstrap.sh
.venv/bin/python -m scripts.seed_demo
exec .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 9000
