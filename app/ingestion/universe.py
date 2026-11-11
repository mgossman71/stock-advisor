"""Screening universe: every US-listed common stock above the scan floors.

Preference order:

1. Nasdaq's stock screener (``api.nasdaq.com/api/screener/stocks``): one call
   returns all ~7,000 NYSE/Nasdaq/AMEX listings with sector, industry, market
   cap, last price and volume.  :func:`filter_listed` keeps common stocks above
   the floors (market cap, price, dollar volume) - roughly 2,500-3,000 names -
   and drops warrants, preferreds, notes, closed-end funds and SPAC shells.
   The filtered list is cached to ``data/us_listed.csv``.
2. ``data/us_listed.csv`` - the last good Nasdaq list, so an offline machine
   still scans a real market.
3. The S&P 500 (Wikipedia, then ``data/sp500.csv``) - large caps only.
4. ``SEED_SYMBOLS`` from ``.env`` as the last resort (logged loudly, never
   silently trusted).

There is no alphabetical cut-off any more: the old ``universe_size=100`` sliced
Wikipedia's name-sorted table, so only "3M" through "Chevron" were ever
screened.  An optional cap now keeps the *largest* names by market cap.

Share classes use the yfinance convention (``BRK.B`` -> ``BRK-B``) so one symbol
works against every provider.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.config import BASE_DIR, Settings, get_settings
from app.db.models import Ticker
from app.ingestion.ingest import ensure_ticker
from app.ingestion.normalize import clean_text, to_decimal, to_int
from app.providers.base import UniverseMember

WIKIPEDIA_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
CSV_PATH: Path = BASE_DIR / "data" / "sp500.csv"
NASDAQ_SCREENER_URL = "https://api.nasdaq.com/api/screener/stocks"
US_LISTED_CSV_PATH: Path = BASE_DIR / "data" / "us_listed.csv"
_UA = "Mozilla/5.0 (compatible; stock-advisor/0.1; local research tool)"


def normalize_symbol(raw: str) -> str:
    """Yahoo convention: uppercase; share-class dots/slashes become dashes."""
    return (str(raw).strip().upper().replace(".", "-").replace("/", "-")
            .replace(" ", ""))


# ----------------------------------------------------------- US-listed scan

#: Listing names that are not operating-company common stock.
#: Names are whitespace-collapsed by ``clean_text`` first, so ``$`` anchors
#: to the last word.  Partnership "Units" go too (K-1 partnerships, not
#: common stock); ADRs ("American Depositary Shares") stay.
_NOT_COMMON = re.compile(
    r"\b(?:warrants?|preferred|notes? due|debentures?|subordinated|fund|etf|etn"
    r"|exchange[- ]traded|acquisition corp\w*|units? each)\b"
    r"|\b(?:rights?|units?)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ListedStock:
    """One row of the Nasdaq screener, numbers parsed (None = not given)."""

    symbol: str
    name: str | None
    sector: str | None
    industry: str | None
    market_cap: Decimal | None
    price: Decimal | None
    volume: int | None


def fetch_us_listed(timeout: float = 30.0) -> list[ListedStock]:
    """Every NYSE/Nasdaq/AMEX listing in one call.  Raises on any problem.

    ``api.nasdaq.com`` rejects plain httpx (TLS fingerprinting), so this goes
    through ``curl_cffi`` browser impersonation like the Nasdaq provider.
    """
    from curl_cffi import requests as browser_requests

    with browser_requests.Session(
        impersonate="chrome", timeout=timeout,
        headers={"Accept": "application/json, text/plain, */*",
                 "Referer": "https://www.nasdaq.com/", "Origin": "https://www.nasdaq.com"},
    ) as client:
        response = client.get(NASDAQ_SCREENER_URL,
                              params={"tableonly": "true", "download": "true"})
    if response.status_code != 200:
        raise ValueError(f"Nasdaq screener HTTP {response.status_code}")
    rows = ((response.json() or {}).get("data") or {}).get("rows") or []
    if not rows:
        raise ValueError("Nasdaq screener returned no rows")
    return [parse_listed_row(row) for row in rows if row.get("symbol")]


def parse_listed_row(row: dict) -> ListedStock:
    """One raw screener row -> :class:`ListedStock` (missing stays None)."""
    return ListedStock(
        symbol=str(row.get("symbol") or "").strip().upper(),
        name=clean_text(row.get("name"), 200),
        sector=clean_text(row.get("sector"), 100),
        industry=clean_text(row.get("industry"), 120),
        market_cap=to_decimal(row.get("marketCap")),
        price=to_decimal(row.get("lastsale")),
        volume=to_int(row.get("volume")),
    )


def filter_listed(
    rows: list[ListedStock], *, min_market_cap: Decimal, min_price: Decimal,
    min_dollar_volume: Decimal,
) -> list[UniverseMember]:
    """Common stocks above every floor.  A row missing a number fails that
    floor - "unknown" is never read as "big enough"."""
    members: dict[str, tuple[Decimal, UniverseMember]] = {}
    for row in rows:
        if "^" in row.symbol or not row.symbol:
            continue  # Nasdaq writes preferred series as ABC^A
        if row.name and _NOT_COMMON.search(row.name):
            continue
        if row.market_cap is None or row.market_cap < min_market_cap:
            continue
        if row.price is None or row.price < min_price:
            continue
        if row.volume is None or row.price * row.volume < min_dollar_volume:
            continue
        symbol = normalize_symbol(row.symbol)
        if not symbol.replace("-", "").isalnum():
            continue
        name = _short_name(row.name)
        # One share class per company (GOOG/GOOGL, BRK-A/BRK-B): the most
        # traded one, so two classes of the same business can't take two
        # shortlist slots.
        company = (name or symbol).lower()
        dollar_volume = row.price * row.volume
        kept = members.get(company)
        if kept is not None and kept[0] >= dollar_volume:
            continue
        members[company] = (dollar_volume, UniverseMember(
            symbol=symbol, name=name, sector=row.sector,
            industry=row.industry, market_cap=row.market_cap,
        ))
    return sorted((m for _, m in members.values()),
                  key=lambda m: m.market_cap or 0, reverse=True)


def _short_name(name: str | None) -> str | None:
    """'Alcoa Corporation Common Stock' -> 'Alcoa Corporation'."""
    if not name:
        return None
    trimmed = re.sub(r"\s+(Class [A-Z] )?(Common Stock|Ordinary Shares|Common Shares|Capital Stock|Common)\b.*$",
                     "", name, flags=re.IGNORECASE).strip()
    return trimmed or name


def fetch_sp500_live(timeout: float = 20.0) -> list[UniverseMember]:
    """Scrape the S&P 500 constituents table from Wikipedia.

    Raises on any problem so the caller can fall back to the checked-in CSV.
    """
    import pandas as pd  # local import keeps CLI start-up light

    response = httpx.get(
        WIKIPEDIA_URL, headers={"User-Agent": _UA}, timeout=timeout, follow_redirects=True
    )
    response.raise_for_status()
    # pandas 3 dropped raw-string input for read_html (it is treated as a path),
    # so the page has to be handed over as a text stream.
    frames = pd.read_html(io.StringIO(response.text), match="Symbol")
    if not frames:
        raise ValueError("no constituent table found on the Wikipedia page")
    table = frames[0]
    if not {"Symbol", "Security"}.issubset(table.columns):
        raise ValueError(f"unexpected columns: {list(table.columns)}")

    members: list[UniverseMember] = []
    for _, row in table.iterrows():
        symbol = normalize_symbol(row["Symbol"])
        if not symbol or not symbol.replace("-", "").isalnum():
            continue
        members.append(UniverseMember(
            symbol=symbol,
            name=clean_text(row.get("Security"), 200),
            sector=clean_text(row.get("GICS Sector"), 100),
            industry=clean_text(row.get("GICS Sub-Industry"), 120),
        ))
    if not members:
        raise ValueError("constituent table parsed but produced no symbols")
    return members


def write_universe_csv(members: list[UniverseMember], path: Path = CSV_PATH) -> None:
    """Persist the universe so later runs work offline."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["symbol", "name", "sector", "industry", "market_cap"])
        for member in members:
            writer.writerow([
                member.symbol, member.name or "", member.sector or "", member.industry or "",
                "" if member.market_cap is None else str(member.market_cap),
            ])


def read_universe_csv(path: Path = CSV_PATH) -> list[UniverseMember]:
    """Read the checked-in fallback universe (empty list when absent)."""
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return [
            UniverseMember(
                symbol=normalize_symbol(row["symbol"]),
                name=row.get("name") or None,
                sector=row.get("sector") or None,
                industry=row.get("industry") or None,
                market_cap=to_decimal(row.get("market_cap")),
            )
            for row in csv.DictReader(handle)
            if row.get("symbol")
        ]


@dataclass
class UniverseResult:
    """Where the universe came from, plus the members themselves."""

    source: str
    members: list[UniverseMember]
    notes: list[str]


def load_universe(
    settings: Settings | None = None,
    *,
    limit: int | None = None,
    csv_path: Path | None = None,
    listed_csv_path: Path | None = None,
    min_market_cap: Decimal | None = None,
    min_price: Decimal | None = None,
    min_dollar_volume: Decimal | None = None,
) -> UniverseResult:
    """Resolve the screening universe, preferring the live US-listed scan.

    Floors default to ``settings``; the Settings page passes its own.  The
    paths are parameters so tests (and alternate checkouts) can redirect them.
    """
    settings = settings or get_settings()
    limit = limit or settings.universe_size
    csv_path = csv_path or CSV_PATH
    listed_csv_path = listed_csv_path or US_LISTED_CSV_PATH
    floors = {
        "min_market_cap": settings.scan_min_market_cap if min_market_cap is None
        else min_market_cap,
        "min_price": settings.scan_min_price if min_price is None else min_price,
        "min_dollar_volume": settings.scan_min_dollar_volume if min_dollar_volume is None
        else min_dollar_volume,
    }
    notes: list[str] = []

    def done(source: str, members: list[UniverseMember]) -> UniverseResult:
        return UniverseResult(source=source, members=cap_universe(members, limit), notes=notes)

    try:
        listed = fetch_us_listed(timeout=max(settings.provider_timeout_seconds, 30.0))
        members = filter_listed(listed, **floors)
        if not members:
            raise ValueError("no listing passed the scan floors")
        write_universe_csv(members, listed_csv_path)
        notes.append(f"live Nasdaq screener: {len(members)} of {len(listed)} listings "
                     "pass the floors (CSV refreshed)")
        return done("nasdaq", members)
    except Exception as exc:
        notes.append(f"Nasdaq screener failed ({type(exc).__name__}: {exc})")

    members = [m for m in read_universe_csv(listed_csv_path)
               if m.market_cap is not None and m.market_cap >= floors["min_market_cap"]]
    if members:
        notes.append(f"cached US-listed CSV fallback: {len(members)} stocks")
        return done("us_listed_csv", members)

    try:
        members = fetch_sp500_live(timeout=settings.provider_timeout_seconds)
        write_universe_csv(members, csv_path)
        notes.append(f"live Wikipedia fetch: {len(members)} constituents (CSV refreshed)")
        return done("wikipedia", members)
    except Exception as exc:
        notes.append(f"Wikipedia fetch failed ({type(exc).__name__}: {exc})")

    members = read_universe_csv(csv_path)
    if members:
        notes.append(f"checked-in CSV fallback: {len(members)} constituents")
        return done("csv", members)

    fallback = [UniverseMember(symbol=symbol) for symbol in settings.seed_symbol_list]
    notes.append(f"SEED_SYMBOLS fallback: {len(fallback)} symbols")
    return UniverseResult(source="config", members=fallback, notes=notes)


def cap_universe(members: list[UniverseMember], limit: int | None) -> list[UniverseMember]:
    """Keep the ``limit`` largest by market cap (unknown caps last), never an
    alphabetical slice.  ``None`` keeps everything."""
    if not limit or len(members) <= limit:
        return members
    ranked = sorted(members, key=lambda m: (m.market_cap is None, -(m.market_cap or 0)))
    return ranked[:limit]


def persist_universe(
    session: Session, members: list[UniverseMember], *, replace: bool = False,
) -> int:
    """Upsert universe members as tickers flagged ``in_universe``.

    ``replace=True`` also un-flags tickers that are no longer members (fell
    below a floor, delisted).  Rows are never deleted: their history, reports
    and any trades stay intact.
    """
    symbols = set()
    for member in members:
        ensure_ticker(
            session, member.symbol, name=member.name, sector=member.sector,
            industry=member.industry, market_cap=member.market_cap, in_universe=True,
        )
        symbols.add(member.symbol.upper())
    if replace and symbols:
        session.execute(
            update(Ticker).where(Ticker.in_universe.is_(True), Ticker.symbol.not_in(symbols))
            .values(in_universe=False)
        )
    session.flush()
    return len(members)


def universe_symbols(session: Session, settings: Settings | None = None) -> list[str]:
    """Symbols to screen: universe members + watchlist + anything else active."""
    settings = settings or get_settings()
    rows = session.scalars(
        select(Ticker).where(
            (Ticker.in_universe.is_(True))
            | (Ticker.in_watchlist.is_(True))
            | (Ticker.active.is_(True))
        )
    ).all()
    symbols = {row.symbol for row in rows}
    symbols.update(settings.seed_symbol_list)
    return sorted(symbols)

