# stock-advisor — agent notes

Personal stock research + paper-trading app. FastAPI / Jinja2 / HTMX / SQLite.
**Phases 2–6 of the plan in `stock-advisor-agent-prompt.md` are not built yet** — the
trading core, scoring, recommendations, backtesting, web UI and dashboard are all
still to come. Do not assume a module exists; check the tree.

What exists so far: config, DB models + Alembic, provider layer, ingestion, universe
loading, the `python -m app.cli` entry point, and `app/main.py` — a read-only status
page plus `/healthz` for the container healthcheck. The real UI (HTMX pages, charts,
portfolio screens) is still Phase 5.

## Commands

```bash
make up                        # docker compose up -d --build -> http://localhost:8000
make down                      # stop the stack
make clean-db-docker           # down -v: ALSO deletes the database volume
make docker-stats              # stats as the container sees them (volume, not ./data)
make docker-fetch TICKER=MSFT  # one-shot fetch into the container's volume

make test                      # .venv/bin/python -m pytest -q  -> 117 tests, <1s, no network
make migrate                   # alembic upgrade head
rm -f data/stock_advisor.db* && make migrate    # rebuild the local DB from scratch
bash scripts/bootstrap.sh      # .venv (Python 3.11/3.12), install deps, migrate — idempotent

.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000    # serve locally
.venv/bin/python -m app.cli providers                # which sources are reachable
.venv/bin/python -m app.cli fetch-universe           # seed tickers only (--force = + fundamentals, slow)
.venv/bin/python -m app.cli fetch AAPL --period 1y   # history, fundamentals, news, analysts, earnings
.venv/bin/python -m app.cli stats                    # row counts + coverage + fetch log
```

`make dev` and `run.sh` both still fail: they call `python -m scripts.seed_demo`, which
is Phase 6 and does not exist yet. There is no `make lint` target (ruff is not
installed). There is no package `__main__.py`, so `python -m app` fails — it is
`python -m app.cli`.

Table names (get these right): `tickers`, `price_history`, `fundamentals`,
`news_items`, `analyst_actions`, `earnings_events`, `data_fetch_log`, plus the
trading/advice tables declared in the models but not exercised yet.

## Tests that must stay green

`tests/test_migrations.py::test_migration_matches_models` compares the Alembic
migration's columns against the model metadata — the guard against the bug in
"Schema changes" below. `test_normalize.py` pins fraction-vs-percent-point unit
decisions, `test_throttle.py` proves retry/backoff without sleeping,
`test_ingest_*.py` prove idempotency and no-fabrication.

## Gotchas

- **yfinance works on this machine** for market data (it routes through `curl_cffi`
  browser impersonation; plain `curl` to Yahoo returns 429). `app/providers/yfinance_provider.py`
  is the primary source and is verified live.
- **`api.nasdaq.com` 403s plain `httpx`** (TLS fingerprinting) while `curl` succeeds, so
  `app/providers/nasdaq_provider.py` uses `curl_cffi` with browser impersonation and needs
  `?assetclass=stocks` on the quote endpoints.
- **`pandas.read_html` no longer accepts a raw HTML string** (pandas ≥3): pass
  `io.StringIO(html)` and always select the table by column names, never by index —
  Wikipedia reorders tables. See `app/ingestion/universe.py`.
- **Never default a missing value to 0.** `0` is a real market price; an absent
  `pe_ratio` / `target_mean` must stay NULL so "we don't know" can't leak into scoring
  as "cheap". Likewise strip binary float noise from Yahoo values (0.1999999999999)
  before it reaches SQLite. Both live in `app/ingestion/normalize.py`.
- **Fractions vs percent points are mixed across sources**: `debt_to_equity` is a ratio
  (0.843), `return_on_equity`/`profit_margin`/`revenue_growth` are percent points
  (142.3 = 142.3 %), Nasdaq returns both as percent points (`105.58 %` → 1.0558).
  Only `dividend_yield` is already a percent point everywhere.
- Schema changes go through **Alembic autogenerate**, never hand-written migrations: a
  column that exists in `app/db/models_market.py` but has no `op.add_column` in
  `migrations/versions/0001_initial_schema.py` crashes the first insert (this actually
  happened with `fundamentals.market_cap`, and an index was duplicated by naming it
  twice in the model).
- **All money and ratio columns are `NUMERIC`** so the ORM returns `Decimal`; settings
  holding money are `Decimal` too. Never widen market values to float.
- SQLite has no `ALTER TABLE ADD CONSTRAINT`, so composite uniqueness (e.g. on
  `price_history`) is only expressible as an index — enforced by
  `tests/test_ingest_history.py::test_reingesting_same_range_does_not_duplicate_bars`.
- **Container database lives in a named volume**, not in `./data`: both Dockerfile and
  compose set `DATABASE_URL=sqlite:////data/stock_advisor.db`, mounted from
  `stock_advisor_data`. The repo-relative default `sqlite:///./data/...` would put the
  DB on the container layer (gone when the container is removed), and a bind mount is
  deliberately avoided because SQLite locking over Docker Desktop binds on macOS is
  unreliable. Consequence: the host `./data/stock_advisor.db` and the container's DB
  are two different databases — read the container's with `make docker-stats`.
- **`.dockerignore` excludes `.env` on purpose.** pydantic-settings reads
  `BASE_DIR/.env`, so a host `.env` baked into the image would override the image's
  `DATABASE_URL` and silently move the database off the volume. In the container,
  settings come only from compose `environment:`. There is no `FRED_API_KEY` setting
  (FRED is reached key-free via `fredgraph.csv`) — do not invent env vars.
- **Compose searches parent directories.** A project without its own compose file picks
  up the stale `/Users/markgossman/docker-compose.yml` (an unrelated, deleted Node
  project whose `./server` / `./client` build contexts no longer exist), which fails as
  `unable to prepare context`. That is why this file exists here.
- **`sqlalchemy.inspect(engine)` checks out a second connection.** Harmless on a file
  DB, but for in-memory SQLite (where the database *is* the connection) it discards
  the data being inspected — it broke `app/ingestion/stats.py` until the catalog was
  read on the session's own connection (`sqlite_master` / `information_schema`).
- `data/stock_advisor.db*` is git-ignored — delete it and `make migrate` for a clean rebuild.
- A real `.env` may hold `FRED_API_KEY`; never print its value.
