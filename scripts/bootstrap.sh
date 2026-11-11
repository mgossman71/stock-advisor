#!/usr/bin/env bash
# Create the virtualenv, install dependencies, run migrations, seed demo data.
# Safe to re-run: every step is skipped/no-ops when already satisfied.
set -euo pipefail

cd "$(dirname "$0")/.."

VENV="${VENV:-.venv}"
PY_MIN=11

pick_python() {
  # Prefer an interpreter in the 3.11-3.12 range: pandas/yfinance wheels are
  # well-tested there, and the spec asks for Python 3.11+.
  local candidates=("${PYTHON:-}" "python3.12" "python3.11" "python3")
  local bin
  for bin in "${candidates[@]}"; do
    [ -z "$bin" ] && continue
    command -v "$bin" >/dev/null 2>&1 || continue
    if "$bin" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if (3, 11) <= sys.version_info[:2] < (3, 13) else 1)
PY
    then
      echo "$bin"
      return 0
    fi
  done
  echo "No Python 3.11/3.12 interpreter found. Install one (e.g. brew install python@3.11)." >&2
  return 1
}

PYBIN="$(pick_python)"

if [ ! -x "$VENV/bin/python" ]; then
  echo "==> Creating virtualenv with $PYBIN ($($PYBIN --version 2>&1))"
  "$PYBIN" -m venv "$VENV"
else
  echo "==> Virtualenv already present ($VENV)"
fi

VPY="$VENV/bin/python"
echo "==> Python in venv: $($VPY --version)"

echo "==> Installing dependencies"
"$VPY" -m pip install --upgrade pip >/dev/null
"$VPY" -m pip install -r requirements.txt

echo "==> Writing .env from .env.example (if missing)"
[ -f .env ] || cp .env.example .env

echo "==> Applying migrations"
"$VPY" -m alembic upgrade head
