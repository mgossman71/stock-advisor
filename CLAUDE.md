# stock-advisor — agent notes

Personal stock research + paper-trading app. FastAPI / Jinja2 / HTMX / SQLite.
All six build phases are implemented: data layer, paper-trading core, research
engine, screener/recommendations, full web UI + scheduler, seed script + README.
The `AdvisorEngine` protocol (`app/research/engine.py`) has two engines:
`RuleBasedAdvisor` and `LLMAdvisor` (`app/research/llm_advisor.py`, any
OpenAI-compatible self-hosted model, configured on the Settings page).
Call sites get theirs from `advisor_for(session, screening=...)`.

What exists: config, DB models + Alembic, provider layer (yfinance -> nasdaq ->
fred fallback chain), ingestion, universe loading, `python -m app.cli`
(fetch/research/trade/portfolio/stats), `app/trading/` (orders, positions,
P&L, sizing, snapshots), `app/research/` (indicators, scoring, report, engine,
screener, outcomes), `app/jobs.py` (the nightly pipeline + alerts), and
`app/web/` (all pages + `POST /jobs/refresh`).

## Commands

```bash
make dev                       # bootstrap -> seed (REAL data, slow once) -> uvicorn 0.0.0.0:9000
make up                        # docker compose up -d --build -> http://localhost:9000
make down                      # stop the stack
make clean-db-docker           # down -v: ALSO deletes the database volume
make docker-stats              # stats as the container sees them (volume, not ./data)
make docker-fetch TICKER=MSFT  # one-shot fetch into the container's volume

make test                      # .venv/bin/python -m pytest -q -> 437 tests, no network
make migrate                   # alembic upgrade head
rm -f data/stock_advisor.db* && make migrate    # rebuild the local DB from scratch
bash scripts/bootstrap.sh      # .venv (Python 3.11/3.12), install deps, migrate — idempotent
.venv/bin/python -m scripts.seed_demo   # seed real history for SEED_SYMBOLS (idempotent)

.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 9000
.venv/bin/python -m app.cli providers / stats / fetch AAPL --period 1y
.venv/bin/python -m app.cli research AAPL [--refresh]   # full report + calls
.venv/bin/python -m app.cli trade buy AAPL 10 [--fetch] [--limit 95]  # paper order
.venv/bin/python -m app.cli cancel-order N             # withdraw a pending limit
.venv/bin/python -m app.cli adjust-cash 500 [--withdraw] [--note ...]  # cash in/out, not a trade
.venv/bin/python -m app.cli record-buy AAPL 10 210.50 [--date --rec N --commission --note]  # real trade
.venv/bin/python -m app.cli record-sell ... # same args; sells cannot exceed the real ledger
.venv/bin/python -m app.cli set-password               # web login fallback/reset (stdin ok)
.venv/bin/python -m app.cli holdings                   # real lots + gain/loss + est tax
.venv/bin/python -m app.cli portfolio                  # marks + day snapshot
```

Table names (get these right): `tickers`, `price_history`, `fundamentals`,
`news_items`, `analyst_actions`, `earnings_events`, `data_fetch_log`,
`cash_adjustments` (paper cash deposits/withdrawals), `real_trades` (the
user's real-broker ledger feeding `app/tracking/` FIFO + tax estimates),
`stock_splits` (detected splits; real trades are replayed through them),
`dividends` (per-share cash dividends by ex-date), `dividend_payments`
(paper dividends booked, unique per account+dividend),
plus the other trading/advice tables declared in the models.

Ranking rule (`app/research/ranking.py`, deliberate and simple): open calls,
newest per ticker wins, then confidence DESC — do not blend in scoreboard
track record without a new decision.

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
  up the stale `~/docker-compose.yml` (an unrelated, deleted Node
  project whose `./server` / `./client` build contexts no longer exist), which fails as
  `unable to prepare context`. That is why this file exists here.
- **`sqlalchemy.inspect(engine)` checks out a second connection.** Harmless on a file
  DB, but for in-memory SQLite (where the database *is* the connection) it discards
  the data being inspected — it broke `app/ingestion/stats.py` until the catalog was
  read on the session's own connection (`sqlite_master` / `information_schema`).
- **Quote bars are `provisional`.** `ingest_quote` writes today's price with
  `price_history.provisional = 1` and no OHLC; freshness only counts settled
  bars, and a quote never overwrites a settled bar.  Before this, one quote a
  day made history look fresh forever and the post-close quote wiped the
  day's OHLC - don't reintroduce a freshness check over *all* rows.
- **Splits rescale stored state once** (`app/ingestion/splits.py`): detection
  needs >= 2 pre-split overlap bars agreeing on a small-integer ratio, so a
  single revised bar is never mistaken for one.  Real trades are never
  rewritten - `app/tracking/lots.py` applies `stock_splits` during replay.
- **Tax rules** (`app/tracking/taxes.py`): holding period is calendar-based
  (`holding_class(bought, sold)`), ST/LT net against each other before rates
  apply, realised figures are current-tax-year only, tax is never negative.
- **Paper fills need a current-session price** (`app/trading/freshness.py`):
  order entry points call `ensure_fresh_price` (live quote if the stored price
  predates the last NY weekday) and pass `price_not_before` to
  `execute_order` only when that failed.  No holiday calendar on purpose: a
  successful quote counts as current whatever its date.  `execute_order`
  stays network-free; tests override `routes_portfolio.order_provider`.
- **Paper dividends** (`app/trading/dividends.py`): entitlement = current qty
  minus fills on/after the ex-date; booked on the ex-date into cash *and*
  `realized_pnl`; 30-day lookback.  The fallback chain treats an empty
  dividend list as success (non-payers), like an empty quote.
- **LLM advisor** (`app/research/llm.py`, `llm_advisor.py`): config is
  `app_settings` rows `llm_*`, not env vars. The model never sets the entry
  price (latest stored close only), bad levels fall back to rule defaults,
  and any model failure persists the plain rules report with
  `payload.llm_error`. `seed_demo` deliberately stays on `RuleBasedAdvisor`.
  Tests inject a fake client; `test_llm_advisor.py` pins the guards.
- **Password login** (`app/auth.py`, `require_login` in `app/main.py`): every
  route except `/login`, `/setup`, `/healthz`, `/static/` needs the
  `sa_session` cookie. Fails closed - no password set means locked, and there
  is deliberately no auth-off switch (the app is internet-facing on
  `0.0.0.0:9000`). First-run `/setup` is browser-based but only for
  `may_bootstrap` peers (loopback/private, no proxy headers); under Docker
  Desktop every peer is `192.168.65.1`, so that guard can't tell a direct
  port-forward from the LAN. Hash and session secret live in `app_settings`;
  any password change rotates the secret.
  The login throttle keys on the socket peer, never `X-Forwarded-For`. Web
  tests' `client` fixture signs in; `tests/test_auth.py` uses a signed-out one.
- **Writes refuse cross-site requests** (`same_origin_writes` in
  `app/main.py`): the CSRF guard, alongside the `SameSite=Lax` cookie.
- `data/stock_advisor.db*` is git-ignored — delete it and `make migrate` for a clean rebuild.
- A real `.env` may hold `FRED_API_KEY`; never print its value.
- **Scheduler ownership:** the app container arms APScheduler (nightly cron)
  and, on a fresh DB (`needs_first_run`: no universe, or under half of it screened), starts
  first-run setup (`AUTO_SETUP`); one-shot containers (`ingest`) must set
  `SCHEDULER_ENABLED=0` or two processes race the same SQLite volume.
- **Two lenses, one scan** (`app/research/emerging.py`): `run_scan` also scores
  every stock on the emerging-opportunity lens (price/volume only, RS is a
  universe percentile, so it is computed after all rows) into
  `scan_scores.emerging_*`. `emerging_symbols` (top `emerging_size`, score
  >= 55) joins the shortlist for the full fetch + report + AI;
  `screener.emerging_recommendation` makes `emerging-v1[+llm:...]` longs
  (lens >= 70, AI must be bullish / rules not bearish, -8%/+25%, horizon
  short = 120-day expiry). Scoreboard/recommendations split by
  `lens_filter` on `engine_version` - keep the `emerging` prefix.
- **Earnings calendar** (`get_earnings_calendar`, `ingest_earnings_calendar`):
  one Nasdaq call per day, market-wide; only updates tickers already stored
  (never creates them); a day is "settled" once fetched >= 3 days after it
  (`earnings_calendar_days`); a calendar "scheduled" never overwrites a
  "reported" row.  An empty day is success in the fallback chain.
- **AI budget, not AI count** (`screener.screen_universe`): candidates come
  from `screening_candidates` in priority order (owned/watched -> emerging by
  `confirm_emerging` score -> shortlist); `AIBudget` (injectable clock) stops
  model calls at `ai_minutes_per_night`; `reusable_ai_report` skips the model
  for unchanged names (< 7 days, price +-5%, no new earnings/analyst action,
  lens +-10).  Budget-skipped reports carry `payload.ai_skipped="budget"`.
- **Backtest** (`app/research/backtest.py`): must call the live
  `emerging.signals/score/percentiles` on bars sliced to the as-of date -
  `tests/test_backtest.py::test_no_look_ahead` pins it.  History extension
  (`jobs.extend_history`) is one-time, stamped `history_extended_from`.
- **"Refresh prices" = everything** (`jobs.refresh_prices`): bulk
  `ingest_quotes` over every ticker with stored prices via
  `get_quotes` (Yahoo `v7/finance/quote` through yfinance's `YfData`, which
  handles the crumb; `QUOTE_CHUNK`=200), then one-at-a-time `ingest_quote`
  only for held/watched symbols the batch missed.  Both store through
  `_store_quote` (provisional bar, never over a settled close).  The Nasdaq
  screener's `lastsale` is NOT live (it is the previous close) - never use
  it for prices.
- **Long work goes through `app/background.py`'s `runner`** (single-flight,
  daemon thread, progress via `GET /jobs/status`): first-run setup, nightly,
  `POST /jobs/refresh`, `POST /screen/run`. Never run the pipeline inside a
  request - a proxy timeout kills it. Background screens commit per ticker
  (`commit_each`) so a slow LLM doesn't hold SQLite's write lock.
  `POST /jobs/refresh` is the manual trigger and is idempotent — short-borrow accrual is gated once per calendar
  day by a `data_fetch_log` row (`provider='system'`, `endpoint='short_borrow'`).
- **Two refresh jobs** (`app/jobs.py`): `refresh_prices` (`POST /jobs/prices`)
  only quotes `price_symbols` (held/watched, never screening-only universe
  members) then fills/marks/snapshot/alerts; `run_nightly`
  (`POST /jobs/refresh`) is the full pass: the discovery funnel, then the
  full fetch over `target_symbols` (= `price_symbols` + scan shortlist + VIX)
  and the screen. Borrow and dividends stay in the full pass only.
- **Discovery funnel** (`app/research/scan.py`, `app/ingestion/universe.py`):
  the universe is every US-listed common stock above the scan floors (~3,000,
  Nasdaq screener `api.nasdaq.com/api/screener/stocks`, curl_cffi), not the
  S&P 500. The old `universe_size=100` sliced Wikipedia's *name-sorted*
  table (3M..Chevron only); `universe_size` is now an optional cap that keeps
  the largest by market cap. Universe stocks get history only
  (`scan_fetch_symbols`) plus a rotating fundamentals slice
  (`fundamentals_due`), then `run_scan` writes `scan_scores`. Only the top
  `shortlist_size` + held/watched get `refresh_symbol(with_alternatives)`
  and `screen_universe` (so the LLM never sees the whole universe).
  Price-only scores rank after names with fundamentals. Floors/shortlist live
  in `app_settings` `scan_*` rows (Settings -> Stock discovery); a changed
  floor clears `universe_refreshed_at` so the list rebuilds on the next full
  run. Only a live Nasdaq fetch stamps the weekly refresh, and a degraded
  fallback (S&P/seed) never replaces an existing universe.
  `needs_first_run` counts *scanned* (not reported) universe members.
  A local `.env` with `UNIVERSE_SIZE=100` now means "100 largest".
- **Auto-refresh** (`app/autorefresh.py`): two independent timers, one per
  job (`prices`, `full`; full has a 60-min floor), each with its own
  on/interval/market-hours, in `app_settings` rows
  `autorefresh_<job>_<field>` (off by default; the pre-split single-timer
  `autorefresh_*` rows are read as a fallback and deleted on save).
  `POST /settings/autorefresh` re-arms the live scheduler via
  `apply_schedule` (module-level `_scheduler`, set at startup; `None` in
  tests/CLI; scheduler job ids `autorefresh-prices` / `autorefresh-full`).
  Jobs never overlap: ticks and `nightly_job` use `runner.run_when_free`,
  which polls every 30 s until the running job ends (max 6 h; a
  market-hours tick that waits past the close is dropped) instead of
  skipping. `max_instances=1` stops a waiting timer from queuing copies.
  `runner.waiting()` feeds the "Waiting for it to finish" line in Settings.
- **Web routes use `session_scope()` directly**; expire_on_commit is False so
  ORM objects stay readable after commit, but lazy relationships must be
  touched *inside* the scope before rendering. HTMX full-page actions return
  an `HX-Redirect` header, plain forms return 303s.
