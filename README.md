# Stock Advisor

Personal research + paper-trading app. FastAPI / SQLAlchemy / Jinja2 + HTMX +
Alpine.js + Chart.js / SQLite (Alembic-migrated, Postgres-portable).

> **Educational simulation — not financial advice. No real orders are placed.**

## What it does

- **Research** — pulls real market data (yfinance primary, Nasdaq/FRED
  fallbacks), scores five transparent blocks, and writes an evidence-backed
  report per ticker. Missing data renders as *unavailable* — never invented.
- **Recommendations** — a rule-based engine emits ranked LONG/SHORT calls with
  rationale, stop/target and confidence. Every call cites its data.  The
  dashboard's "Top ideas today" list ranks open calls by confidence (one per
  ticker, newest wins) and tags tickers you already hold in the real or
  paper ledger.
- **Paper trading** — one account, market/limit-style simulated fills at the
  latest stored close ± slippage, full short support with borrow accrual,
  risk-per-trade position sizing.  You set the balance: deposits/withdrawals
  (`adjust-cash` or the portfolio page) are booked in an auditable
  `cash_adjustments` ledger and excluded from daily P&L and return %.
- **Real-money tracking** — record the purchases you actually make (optionally
  linked to the recommendation that drove them).  FIFO cost basis, gain/loss
  and *estimated* tax liability classified by holding period (≤365d
  short-term / >365d long-term) at your configured rates.  Estimates, not
  tax advice; wash sales not yet handled.
- **Monitoring** — a nightly job (APScheduler + manual `POST /jobs/refresh`)
  refreshes prices, screens the universe, grades every call at 30/90/180 days
  against `^GSPC`, evaluates alerts and writes the daily equity snapshot.
- **Stores everything** — all prices, research, calls, trades and snapshots
  live in SQLite; `GET /api/export.json` dumps it all, and the DB file itself
  is the backup (copy `data/stock_advisor.db`).

## Setup

```bash
make dev          # venv -> deps -> migrate -> seed (real data) -> serve on :8000
# or the container stack (DB in a named volume, NOT ./data):
make up           # docker compose up -d --build -> http://localhost:8000
make test         # pytest, no network, ~1s
```

`make dev` runs `scripts/seed_demo.py`, which fetches **>= 2 years of real
price history** for the 19 `SEED_SYMBOLS` plus `^GSPC` and `^VIX`, then
researches a handful so the dashboard has content on first load.

## Configuration (`.env`)

Copy `.env.example` to `.env`. Nothing secret is ever in code.

| Key | Meaning | Default |
|---|---|---|
| `DATABASE_URL` | sqlite/postgres URL | `sqlite:///./data/stock_advisor.db` |
| `MARKET_DATA_PROVIDER` | `auto` (yfinance→nasdaq→fred) or pin one | `auto` |
| `FINNHUB_API_KEY`, `ALPHAVANTAGE_API_KEY`, `POLYGON_API_KEY` | present = ready for a keyed provider to be swapped in behind `MarketDataProvider` | unset |
| `INITIAL_CASH` | paper account start | `100000` |
| `COMMISSION_PER_SHARE` / `MIN_COMMISSION` / `SLIPPAGE_BPS` | fill friction | `0` / `0` / `5` |
| `SHORT_BORROW_APY` | daily borrow charge on shorts | `0.03` |
| `RISK_PER_TRADE_PCT` | sizing helper risk budget | `1.0` |
| `BENCHMARK_SYMBOL` / `VIX_SYMBOL` | grading + regime panel | `^GSPC` / `^VIX` |
| `NIGHTLY_CRON` | APScheduler cron (UTC) | `15 21 * * *` |
| `SCHEDULER_ENABLED` | set `0` in one-shot containers | `true` |

## How the scoring works (exact thresholds)

Each block scores 0–100 from its components; the composite is the weighted
mean over **known** blocks (weights live in the `scoring_weights` table and
are editable on `/settings`; a missing block's weight is renormalised away,
never zero-filled). Stance: **bullish >= 60**, **bearish <= 40**.

- **Value (0.30)** — PE vs sector median (median→50, half→100, double→0;
  negative PE = 0, loss-making is not "cheap"); EV/EBITDA 25→0 … 8→100;
  P/S 12→0 … 0.5→100; P/B 8→0 … 0.5→100; FCF yield 0→0 … 8→100.
  Negative EV/EBITDA, P/S or P/B (e.g. negative book value) is *excluded* as
  not meaningful, like missing data — a negative multiple is never "cheap".
- **Momentum (0.25)** — 1m/3m/6m/12m returns mapped −20 %→0 … +20 %→100;
  distance from 52-wk high −50 %→0 … 0→100; RSI(14) directly, but ≥ 80 loses
  2 points per RSI point above 70 (overheated); above 50/200-day SMA
  0/50/100.
- **Quality (0.25)** — profit margin 0→0 … 20→100; ROE 0→0 … 25→100;
  debt/equity (ratio) 2.5→0 … 0.3→100; revenue growth −5→0 … 15→100;
  EPS growth −10→0 … 20→100.
- **Sentiment (0.10)** — analyst actions (90 d): 50 + net(upgrades+raises −
  downgrades−cuts)×10; upside to consensus target −20 %→0 … +20 %→100;
  mean news sentiment −50…+50 when the provider supplies it.
- **Risk (0.10, higher = safer)** — annualised vol 45 %→0 … 15 %→100;
  max 252-session drawdown −40 %→0 … 0→100; last EPS surprise −10 %→0 …
  +5 %→100; short interest 15 %→0 … 2 %→100.

Recommendation rules (`rules-v1`): LONG when bullish and score ≥ 65; SHORT
when bearish and ≤ 35, or price ≥ 10 % above consensus target with ≥ 2 net
downgrades. Stop = ±15 % (long/short), target from the consensus when
sane, bounded to +15 %…+50 % (longs) / −15 % (shorts). Confidence =
50 + 1.25 × (score − 60) − 2 × missing-fields, clamped to 10–95. No stored
price → no call.

## How the app grades its own advice

Each recommendation gets a `recommendation_outcomes` row. For every window
(30/90/180 d) whose calendar date has passed in stored data:

```
outcome% = (price_window / entry − 1) × 100          (shorts: sign flipped)
benchmark% = same window on ^GSPC, same base date
alpha% = outcome% − benchmark%
```

Status auto-updates when a later bar actually touches the stop/target
(`hit_stop`/`hit_target`), or on age (`expired`). The scoreboard shows
hit rate and average alpha per window on the dashboard and recommendations
pages — a call with an unfinished window is *not graded*, not "0".

## Tests

```
make test   # 173 tests, <2s, no network
```

Pinned invariants: P&L math (incl. shorts + borrow), scoring thresholds,
no-fabrication (missing fields stay NULL / render "unavailable"), ingestion
idempotency, Alembic-vs-models schema parity, 30d alpha with fixtures.
