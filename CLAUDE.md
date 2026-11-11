# stock-advisor — agent notes

Personal stock research + paper-trading app. FastAPI / Jinja2 / HTMX / SQLite.
All six build phases are implemented: data layer, paper-trading core, research
engine, screener/recommendations, full web UI + scheduler, seed script + README.
The `AdvisorEngine` protocol (`app/research/engine.py`) is the seam where an
LLM-based advisor replaces `RuleBasedAdvisor` later.

What exists: config, DB models + Alembic, provider layer (yfinance -> nasdaq ->
fred fallback chain), ingestion, universe loading, `python -m app.cli`
(fetch/research/trade/portfolio/stats), `app/trading/` (orders, positions,
P&L, sizing, snapshots), `app/research/` (indicators, scoring, report, engine,
screener, outcomes), `app/jobs.py` (the nightly pipeline + alerts), and
`app/web/` (all pages + `POST /jobs/refresh`).

## Commands

```bash
make dev                       # bootstrap -> seed (REAL data, slow once) -> uvicorn :8000
make up                        # docker compose up -d --build -> http://localhost:8000
make down                      # stop the stack
make clean-db-docker           # down -v: ALSO deletes the database volume
make docker-stats              # stats as the container sees them (volume, not ./data)
make docker-fetch TICKER=MSFT  # one-shot fetch into the container's volume

make test                      # .venv/bin/python -m pytest -q -> 173 tests, <2s, no network
make migrate                   # alembic upgrade head
rm -f data/stock_advisor.db* && make migrate    # rebuild the local DB from scratch
bash scripts/bootstrap.sh      # .venv (Python 3.11/3.12), install deps, migrate — idempotent
.venv/bin/python -m scripts.seed_demo   # seed real history for SEED_SYMBOLS (idempotent)

.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
.venv/bin/python -m app.cli providers / stats / fetch AAPL --period 1y
.venv/bin/python -m app.cli research AAPL [--refresh]   # full report + calls
.venv/bin/python -m app.cli trade buy AAPL 10 [--fetch] # paper order
.venv/bin/python -m app.cli adjust-cash 500 [--withdraw] [--note ...]  # cash in/out, not a trade
.venv/bin/python -m app.cli record-buy AAPL 10 210.50 [--date --rec N --commission --note]  # real trade
.venv/bin/python -m app.cli record-sell ... # same args; sells cannot exceed the real ledger
.venv/bin/python -m app.cli holdings                   # real lots + gain/loss + est tax
.venv/bin/python -m app.cli portfolio                  # marks + day snapshot
```

Table names (get these right): `tickers`, `price_history`, `fundamentals`,
`news_items`, `analyst_actions`, `earnings_events`, `data_fetch_log`,
`cash_adjustments` (paper cash deposits/withdrawals), `real_trades` (the
user's real-broker ledger feeding `app/tracking/` FIFO + tax estimates),
plus the other trading/advice tables declared in the models.

## Tests that must stay green

`tests/test_migrations.py::test_migration_matches_models` compares the Alembic
migration's columns against the model metadata — the guard against the bug in
"Schema changes" below. `test_normalize.py` pins fraction-vs-percent-point unit
decisions, `test_throttle.py` proves retry/backoff without sleeping,
`test_ingest_*.py` prove idempotency and no-fabrication. New suites pin the
business rules: `test_pnl_math.py`/`test_orders.py`/`test_snapshots.py`
(the exact P&L + short-equity math), `test_scoring.py` (thresholds — change
the README whenever these change), `test_report.py`/`test_advisor.py`
(no-fabrication + when calls fire), `test_outcomes.py` (30d alpha fixtures).

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
- **Scheduler ownership:** the app container arms APScheduler (nightly cron);
  one-shot containers (`ingest`) must set `SCHEDULER_ENABLED=0` or two
  processes race the same SQLite volume. `POST /jobs/refresh` is the manual
  trigger and is idempotent — short-borrow accrual is gated once per calendar
  day by a `data_fetch_log` row (`provider='system'`, `endpoint='short_borrow'`).
- **Web routes use `session_scope()` directly**; expire_on_commit is False so
  ORM objects stay readable after commit, but lazy relationships must be
  touched *inside* the scope before rendering. HTMX full-page actions return
  an `HX-Redirect` header, plain forms return 303s.
