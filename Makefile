.PHONY: dev bootstrap migrate revision seed test run clean fetch up down docker-fetch docker-stats docker-logs clean-db-docker

VENV ?= .venv
PY := $(VENV)/bin/python

## Container stack (recommended): build + serve on :9000, DB in a named volume
up:
	docker compose up -d --build

## Stop the stack; add `clean-db` below to also drop the volume
down:
	docker compose down

## One-shot fetch into the container's volume, e.g. `make docker-fetch TICKER=MSFT`
docker-fetch:
	docker compose run --rm ingest python -m app.cli fetch $(TICKER) --period 1y

## Stats from inside the container (reads the volume, not ./data)
docker-stats:
	docker compose run --rm ingest python -m app.cli stats

docker-logs:
	docker compose logs -f app

## Delete the container volume (destructive)
clean-db-docker:
	docker compose down -v

## Bring up everything: venv -> deps -> migrate -> seed -> serve on :9000
dev: bootstrap
	$(PY) -m scripts.seed_demo
	$(PY) -m uvicorn app.main:app --host 0.0.0.0 --port 9000

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
	$(PY) -m uvicorn app.main:app --host 0.0.0.0 --port 9000

## Quick data check, e.g. `make fetch TICKER=AAPL`
fetch:
	$(PY) -m app.cli fetch $(TICKER) --period 1y

clean:
	rm -rf .pytest_cache **/__pycache__
