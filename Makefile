.PHONY: dev bootstrap migrate revision seed test run clean fetch

VENV ?= .venv
PY := $(VENV)/bin/python

## Bring up everything: venv -> deps -> migrate -> seed -> serve on :8000
dev: bootstrap
	$(PY) -m scripts.seed_demo
	$(PY) -m uvicorn app.main:app --host 127.0.0.1 --port 8000

## Create venv, install deps, write .env, run migrations (idempotent)
bootstrap:
	bash scripts/bootstrap.sh

migrate:
	$(PY) -m alembic upgrade head

revision:
	$(PY) -m alembic revision --autogenerate -m "$(M)"

seed:
	$(PY) -m scripts.seed_demo

test:
	$(PY) -m pytest

run:
	$(PY) -m uvicorn app.main:app --host 127.0.0.1 --port 8000

## Quick data check, e.g. `make fetch TICKER=AAPL`
fetch:
	$(PY) -m app.cli fetch $(TICKER) --period 1y

clean:
	rm -rf .pytest_cache **/__pycache__
