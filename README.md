# Stock Advisor

Personal research + paper-trading app. FastAPI / SQLAlchemy / Jinja2 + HTMX +
Alpine.js + Chart.js / SQLite (Alembic-migrated, Postgres-portable). Behavior
that matters (rationale disclosure, trade-cost readout) uses native HTML or a
local script — it keeps working even when CDN scripts are blocked.

> **Educational simulation — not financial advice. No real orders are placed.**

## What it does

- **Research** — pulls real market data (yfinance primary, Nasdaq/FRED
  fallbacks), scores five transparent blocks, and writes an evidence-backed
  report per ticker. Missing data renders as *unavailable* — never invented.
- **Recommendations** — a rule-based engine emits ranked LONG/SHORT calls with
   plain-English, evidence-cited rationale (e.g. `Value: EV/EBITDA of 8.98
   scores 94.26/100 - lower numbers are better (8 earns 100, 25 earns 0)`),
   stop/target and confidence. Rows show **Entry @ call** (price when the call
   was generated) next to **Last** (newest stored close + date — never a live
   quote, never a guess). Per-call actions: **simulate** (paper order),
   **I bought it** (record the real purchase it drove), **close**, **dismiss**
   (ignored calls are kept so the scoreboard can grade the decision). The
   dashboard's "Top ideas today" list ranks open calls by confidence (one per
   ticker, newest wins) and tags tickers you already hold in either ledger.
- **Paper trading (Portfolio page — fake money)** — one account, market and
   limit simulated fills at the latest *session's* price ± slippage.  An order
   never fills at an old price: if the stored price predates the latest US
   trading day (New York time), a live quote is fetched first, and if none is
   available the order is refused with the price's age (the ticket shows each
   price's date).  Cash dividends are booked on the ex-date — longs are paid,
   shorts pay — to whoever held at the prior close, once per dividend (an
   unreachable limit rests as `pending`, is retried nightly, and can be
   cancelled), commissions carried in cost basis so realised P&L matches the
   cash that moved, full short support with Reg-T style initial margin
   (`SHORT_MARGIN_PCT`) and daily borrow debited from cash, and
   risk-per-trade position sizing.  The order ticket shows a live
   **est. trade cost** (qty × latest close; prefilled server-side, so it is
   correct even with JS disabled).  You set the balance: deposits/withdrawals
   (`adjust-cash` or the portfolio page) are booked in an auditable
   `cash_adjustments` ledger and excluded from daily P&L and return %.
- **Real-money tracking (Investments page — actual money)** — record the
   purchases you actually make (optionally linked to the recommendation that
   drove them).  The two ledgers are deliberately separate: simulated fills
   never appear here, keeping real cost basis and tax estimates clean.
   FIFO cost basis, gain/loss
   and *estimated* tax liability classified by holding period (long-term only
   when sold after the one-year anniversary, so leap years count) at your
   configured rates.  Short- and long-term results are netted against each
   other Schedule-D style, realised figures cover the current tax year, and
   a net loss shows as a deductible loss (up to $3,000/yr), never as negative
   tax.  A trade is validated against the whole date-ordered ledger, so a
   back-dated sell needs the shares on *that* date.  The trade form shows the
   live **cost of this trade** (qty × fill price).  Estimates, not tax
   advice; wash sales and prior-year carryforwards not yet handled.
- **Monitoring** — a nightly job (APScheduler + manual `POST /jobs/refresh`)
  refreshes prices (universe, watchlist, paper positions, real holdings,
  open calls), retries pending limits, screens the universe, grades every
  call at 30/90/180 days against `^GSPC`, evaluates alerts and writes the
  daily equity snapshot.  A live quote prices today as a *provisional* bar
  that the next history fetch replaces; it never overwrites a settled bar.
  Stock splits are detected when re-fetched history disagrees with storage by
  a clean ratio (2:1, 3:2, 1:10 …): old bars, paper positions, pending limits,
  calls and price alerts are rescaled once, and real trades are replayed
  through the recorded split without editing your entries.
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
| `SHORT_BORROW_APY` | daily borrow charge on shorts (debited from cash) | `0.03` |
| `SHORT_MARGIN_PCT` | equity must be ≥ this % of total short value after a short | `50` |
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

## AI analyst (self-hosted LLM)

Settings → **AI analyst** points the app at any OpenAI-compatible server
(`POST {base_url}/chat/completions`): Ollama `http://localhost:11434/v1`,
LM Studio `http://localhost:1234/v1`, vLLM, llama.cpp `server`, LocalAI.
From the Docker container use `host.docker.internal` instead of `localhost`.
**Test connection** checks the values as typed, before you save them. Config
lives in the `app_settings` table, so the web app, CLI and nightly job all
use it. The API key is optional and never shown again after saving.

With it on, `LLMAdvisor` (`app/research/llm_advisor.py`) handles the
research button, `app.cli research`, and (unless unticked) the nightly
screen:

1. The rules report above is built first and becomes the model's baseline.
2. The model gets a JSON dossier of *stored* data only: that baseline,
   latest fundamentals (percent fields suffixed `_pct`), a monthly 1-year
   price path, 30-day news, 90-day analyst actions, the last 4 earnings and
   the next one. Nulls stay null.
3. It replies with stance, 0–100 score, thesis, bull/bear/risk points and an
   optional long/short call. That becomes the report's headline; the rules
   view stays under `payload.rules` and shows as the "Rules baseline".

Guards: the entry price is always the latest stored close, never a price
the model wrote. Stops/targets on the wrong side of entry (or outside
0.5×–3× entry) fall back to the rules defaults, and the rationale says so.
No call is made when it contradicts the model's own stance or confidence is
below 55. A bad reply or an unreachable server falls back to the rules
report with `llm_error` shown on the page. After one transport failure the
rest of a screen run skips the model, so a down server costs one timeout,
not one per ticker. Calls are stamped `engine_version = llm:<model>`, so
the scoreboard can tell the two engines apart.

## How the app grades its own advice

Each recommendation gets a `recommendation_outcomes` row. For every window
(30/90/180 d) whose calendar date has passed in stored data:

```
outcome% = (price_window / entry − 1) × 100          (shorts: sign flipped)
benchmark% = same window on ^GSPC, same base date
alpha% = outcome% − benchmark%                       (longs)
alpha% = outcome% + benchmark%                       (shorts)
```

A short call claims the stock will *underperform the market*, so its alpha
is `benchmark% − stock%`.  Grading shorts against a long benchmark would
mark every short a loser in a rising market even when the stock lagged.

Status auto-updates when a later bar actually touches the stop/target
(`hit_stop`/`hit_target`), or on age (`expired`). The scoreboard shows
hit rate and average alpha per window on the dashboard and recommendations
pages — a call with an unfinished window is *not graded*, not "0".

## Tests

```
make test   # 277 tests, <4s, no network
```

Pinned invariants: P&L math (incl. shorts + borrow), scoring thresholds
(incl. negative/implausible multiples excluded, never "cheap"),
no-fabrication (missing fields stay NULL / render "unavailable"), ingestion
idempotency, Alembic-vs-models schema parity, 30d alpha with fixtures.

The **web layer** has its own suite (`tests/test_web_pages.py`) because the
recurring bugs lived in templates/CSS, which unit tests could not see: every
page renders; every table row's colspan-sum matches its header; the action-
button contract per open call (simulate / I bought it / close / dismiss,
retired labels forbidden); every class the templates emit has a rule in
`app.css`; the stylesheet link stays cache-busted; rationale stays a native
`<details>` (no CDN-JS dependency); paper fills land on Portfolio and never
in Investments; both tickets expose live cost readouts that work server-side
without JavaScript.
