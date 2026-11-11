"""Screening universe: S&P 500 constituents, with an offline fallback.

Preference order: (1) Wikipedia's constituents table, which also gives
sector/industry for every name; (2) ``data/sp500.csv`` checked into the repo,
rewritten whenever a live fetch succeeds, so an offline machine still screens a
real universe; (3) ``SEED_SYMBOLS`` from ``.env`` as the last resort (logged
loudly, never silently trusted).

Share classes use the yfinance convention (``BRK.B`` -> ``BRK-B``) so one symbol
works against every provider.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import BASE_DIR, Settings, get_settings
from app.db.models import Ticker
from app.ingestion.ingest import ensure_ticker
from app.ingestion.normalize import clean_text
from app.providers.base import UniverseMember

WIKIPEDIA_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
CSV_PATH: Path = BASE_DIR / "data" / "sp500.csv"
_UA = "Mozilla/5.0 (compatible; stock-advisor/0.1; local research tool)"


def normalize_symbol(raw: str) -> str:
    """Yahoo/Nasdaq convention: uppercase, dots become dashes."""
    return str(raw).strip().upper().replace(".", "-").replace(" ", "")


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
        writer.writerow(["symbol", "name", "sector", "industry"])
        for member in members:
            writer.writerow([
                member.symbol, member.name or "", member.sector or "", member.industry or ""
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
) -> UniverseResult:
    """Resolve the screening universe, preferring live data.

    ``csv_path`` is a parameter rather than a default-argument constant so the
    fallback file can be redirected (tests, alternate checkouts).
    """
    settings = settings or get_settings()
    limit = limit or settings.universe_size
    csv_path = csv_path or CSV_PATH
    notes: list[str] = []

    try:
        members = fetch_sp500_live(timeout=settings.provider_timeout_seconds)
        write_universe_csv(members, csv_path)
        notes.append(f"live Wikipedia fetch: {len(members)} constituents (CSV refreshed)")
        return UniverseResult(source="wikipedia", members=members[:limit], notes=notes)
    except Exception as exc:
        notes.append(f"Wikipedia fetch failed ({type(exc).__name__}: {exc})")

    members = read_universe_csv(csv_path)
    if members:
        notes.append(f"checked-in CSV fallback: {len(members)} constituents")
        return UniverseResult(source="csv", members=members[:limit], notes=notes)

    fallback = [UniverseMember(symbol=symbol) for symbol in settings.seed_symbol_list]
    notes.append(f"SEED_SYMBOLS fallback: {len(fallback)} symbols")
    return UniverseResult(source="config", members=fallback, notes=notes)


def persist_universe(session: Session, members: list[UniverseMember]) -> int:
    """Upsert universe members as tickers flagged ``in_universe``."""
    for member in members:
        ensure_ticker(
            session, member.symbol, name=member.name, sector=member.sector,
            industry=member.industry, in_universe=True,
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

