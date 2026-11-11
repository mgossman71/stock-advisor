# BUILD PROMPT: Personal Stock Advisor & Paper-Trading App

You are building a complete, working personal stock advisor web application. Build it end-to-end, phase by phase, and verify each phase runs before moving on. Do not stop at stubs. Every feature must actually work against real market data.

## 1. What the app does (product spec)

A single-user, locally-run web app that:
1. **Researches stocks** — pulls real price data, fundamentals, news, and analyst signals, then produces evidence-backed research reports per ticker.
2. **Recommends** — generates ranked LONG and SHORT candidates with explicit rationale, bull/bear cases, risk flags, and a confidence score. Every recommendation must cite the specific data points it is based on. The app must NEVER fabricate data; if a data source fails, the report says so.
3. **Simulates purchases (paper trading)** — a virtual cash account where I can "buy" and "sell" at real market prices, track positions, and see realized/unrealized P&L.
4. **Monitors performance** — ongoing tracking of positions and recommendations vs. the S&P 500 benchmark, price alerts, earnings-date reminders, and recommendation hit-rate stats.
5. **Stores everything in a database** — all research, recommendations, trades, prices, and portfolio history in a relational DB, queryable and exportable.

## 2. Tech stack (use exactly this)

- **Backend:** Python 3.11+, FastAPI, SQLAlchemy 2.x, APScheduler for background jobs
- **Database:** SQLite via SQLAlchemy (schema must be portable to Postgres later; use Alembic migrations)
- **Market data:** `yfinance` as the zero-key default (daily OHLCV, fundamentals, news headlines, earnings dates). Wrap it behind a `MarketDataProvider` abstract interface so a keyed provider (Finnhub/Alpha Vantage/Polygon — enabled via `.env` keys if present) can be swapped in without touching business logic.
- **Frontend:** Server-rendered Jinja2 templates + HTMX + Alpine.js + Chart.js (no Node build step). Clean dark dashboard.
- **Config:** `.env` (python-dotenv). No secrets in code.
- **Testing:** pytest; unit tests for P&L math, recommendation engine, and data-normalization. A `make dev` (or single script) target that creates the venv, installs deps, migrates, seeds, and launches.

## 3. Database schema (minimum tables)

- `tickers` — symbol, name, sector, industry, market_cap, currency, active
- `price_history` — ticker_id, date, open/high/low/close, adj_close, volume (unique on ticker+date)
- `fundamentals` — ticker_id, as_of_date, pe_trailing, pe_forward, ev_ebitda, ps, pb, debt_to_equity, roe, revenue_growth_yoy, eps_growth_yoy, dividend_yield, fcf_yield
- `news_items` — ticker_id, published_at, source, title, url, sentiment_score (nullable), body_excerpt
- `analyst_actions` — ticker_id, date, firm, action (upgrade/downgrade/target_cut/target_raise), old_rating, new_rating, old_target, new_target
- `earnings_events` — ticker_id, date, eps_estimate, revenue_estimate, eps_actual, revenue_actual, surprise_pct
- `research_reports` — id, ticker_id, created_at, model_or_rule_version, payload_json (full report incl. bull_points, bear_points, risks, evidence list with source URLs/dates, data_freshness), summary, overall_stance (bullish/neutral/bearish), score
- `recommendations` — id, ticker_id, direction (long/short), horizon (short/medium/long), generated_at, entry_price_at_call, status (open/hit_stop/hit_target/closed/expired), confidence (0-100), report_id FK, rationale
- `recommendation_outcomes` — recommendation_id, price_at_30d/90d/180d, benchmark_price_same_windows, outcome_pct, benchmark_pct, alpha
- `accounts` — single account: cash_balance, initial_cash, opened_at
- `orders` — id, ticker_id, side (buy/sell/short/cover), qty, submitted_at, fill_price, fill_date, commission, status, note
- `positions` — ticker_id, qty (signed), avg_cost, realized_pnl, last_price, updated_at
- `portfolio_snapshots` — date, cash, positions_value, total_value, spx_close (one row per day for the equity curve)
- `alerts` — ticker_id, type (price_above/price_below/drawdown_pct/earnings_within/news), threshold, triggered_at, acknowledged
- `data_fetch_log` — provider, endpoint, ticker, fetched_at, status, error (for freshness/debugging)

## 4. Feature requirements

### 4.1 Research engine
- `POST /research/{ticker}` runs the full pipeline: fetch → normalize → score → report.
- Quantitative scoring (transparent, rule-based — no black box):
  - **Value:** PE/forward PE vs sector median and own 5-yr history, EV/EBITDA, FCF yield
  - **Momentum:** 1m/3m/6m/12m returns, distance from 52-wk high, RSI(14), vs 50/200-day SMA
  - **Quality:** margins, ROE, debt/equity, revenue & EPS growth trends
  - **Sentiment signals:** count of analyst upgrades vs downgrades in last 90 days, price-target distance (current price vs consensus target), news tone
  - **Risk flags:** volatility (annualized), drawdown from peak, recent earnings surprise direction, short-interest if available
- Composite score with user-tunable weights saved in a config table.
- Report renders bull/bone-cold bear case, explicit "data as of" freshness stamp, and a list of every evidence item with source + date.
- If yfinance returns nothing for a field, show "unavailable" — never interpolate or invent.

### 4.2 Screener & recommendations
- Universe: S&P 500 + user watchlist (fetchable constituent list; fall back to a checked-in CSV of tickers if the fetch fails).
- Scheduled nightly screen that ranks candidates and writes `recommendations` rows for both LONG (high value+quality, or washout-with-intact-fundamentals setups) and SHORT (price far above analyst targets, multiple downgrades, guidance misses).
- Include a "regime panel" on the dashboard: index vs 50/200-day SMA, VIX proxy, sector 1-month leaders/laggards — displayed on every recommendation so context is unavoidable.

### 4.3 Paper trading
- Start with configurable cash (default $100,000). Market/limit-style fills at latest close or intraday price with configurable slippage + commission model.
- Support shorts: short entry records borrow-cost assumption in settings; P&L math must be correct for shorts (document the formulas in code comments and test them).
- Suggested-size helper: risk-per-trade sizing given my account value and a stop level.
- Buy/sell directly from any recommendation card ("execute this call" prefills the order ticket at the recommendation's entry price reference).

### 4.4 Monitoring
- Nightly job (APScheduler): refresh prices for all held positions + open recommendations + watchlist, compute P&L, write `portfolio_snapshots`, update `recommendation_outcomes` at 30/90/180 days, auto-expire stale recommendations, evaluate alerts.
- Dashboard: equity curve vs S&P 500 overlay, per-position cards (cost, last, P&L $ and %, days held), open-recommendation scoreboard showing hit rate and average alpha — the app must grade its own advice.
- Alerts surface in-app (badge + alerts page). Email/webhook notification hooks behind an interface, not required for v1.

### 4.5 UI pages
1. **Dashboard** — account summary, equity curve vs SPX, alerts, top long/short calls today, market regime panel
2. **Research** — ticker search, full report view, "run new research" button
3. **Screen** — sortable/filterable screener table over the universe with the sub-scores as columns
4. **Portfolio** — positions, order ticket, trade history, realized P&L by ticker
5. **Recommendations** — all past calls, status, outcome, alpha vs benchmark, filterable
6. **Settings** — starting cash, scoring weights, provider keys, alert defaults

## 5. Non-negotiables (acceptance criteria)

1. `make dev` (or `./run.sh`) brings up the app on http://localhost:8000 with a seeded demo DB (at least 15 real tickers with ≥1 year of real price history fetched, not mock numbers).
2. Researching AAPL end-to-end produces a real report from live yfinance data with zero fabricated values; missing data renders as "unavailable."
3. I can execute a paper buy of 10 shares of a ticker and immediately see the position, updated cash, and P&L; then sell for a correct realized-P&L figure (unit-tested).
4. Nightly monitor job runs via a manually-triggerable endpoint (`POST /jobs/refresh`) AND on schedule; idempotent on re-run.
5. Recommendation scoreboard correctly computes 30d alpha for any recommendation whose window has elapsed (unit-tested with fixtures).
6. Every page that shows a price shows its as-of timestamp.
7. Disclaimer banner: "Educational simulation — not financial advice. No real orders are placed."
8. pytest suite passes; P&L, scoring, and data-normalization logic have tests.

## 6. Build order (deliver in this order, verify each before continuing)

- **Phase 1:** Repo scaffold, config, DB schema + Alembic, yfinance provider with retry/backoff and fetch logging. Verify: migration runs, fetch AAPL history, rows land in DB.
- **Phase 2:** Paper-trading core (accounts, orders, positions, snapshot math) + tests. Verify: CLI demo buy/sell produces correct numbers.
- **Phase 3:** Research engine + report generation + research UI page.
- **Phase 4:** Screener + recommendation generation + recommendations UI + scoreboard.
- **Phase 5:** Portfolio UI, dashboard, charts, alerts, scheduler.
- **Phase 6:** Polish: seed script, README with screenshots description, backup/export (sqlite file copy + JSON export endpoint), pytest pass.

## 7. Style requirements

- Small modules, type hints everywhere, docstrings on public functions.
- Money math in Decimal, never float, for anything touching P&L.
- All external fetches rate-limit-aware (yfinance hammering gets you throttled): cache responses, respect a configurable min-interval between provider calls, exponential backoff.
- The recommendation logic must be rule-based and explainable in v1; design an interface (`AdvisorEngine`) where an LLM-based engine could be plugged in later, but ship the rules engine.
- README must include: setup, how to add API keys, how the scoring works (with the actual thresholds), and how the app grades its own recommendations.

Start with Phase 1 now. After each phase, run the verification step, report what actually happened (commands + output), and only then proceed to the next phase.
