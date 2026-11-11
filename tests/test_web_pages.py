"""Web-layer regression tests: the rendered HTML itself.

Engine/ranking/tracking logic had solid unit coverage, but every layout bug
(stacked buttons, wrap-hell tables, template classes with no CSS rule, stale
button labels, colspan drift when a column was added) slipped through because
*nothing exercised the rendered pages*. These tests pin:

* every page returns 200;
* every table is well-formed: each row's colspan-sum equals the header's;
* the action-button contract on open recommendations (one tidy .row-actions
  cell with simulate / I bought it / close / dismiss - nothing stale);
* every class the templates emit has a rule in app.css (the old
  "class present, CSS missing" failure mode);
* the CSS link is cache-busted (browser staleness bit us repeatedly);
* rationale renders as a native <details> disclosure (no JS dependency);
* close/dismiss endpoints and the investments prefill behave.
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal as D
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db.base import Base, dispose_all, get_engine, session_scope
from app.db.models import PriceHistory, Recommendation
from app.ingestion.ingest import ensure_ticker

BASE = dt.datetime(2026, 1, 5, 12, tzinfo=dt.timezone.utc)
TEST_PASSWORD = "correct horse battery staple"


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Signed-in TestClient bound to a throwaway DB, scheduler off."""
    db_path = tmp_path / "web_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("SCHEDULER_ENABLED", "0")
    get_settings.cache_clear()
    dispose_all()
    Base.metadata.create_all(get_engine())
    from app.main import app  # imported late: sees the patched settings
    from tests.conftest import FakeProvider

    # Orders refresh stale prices from a live quote; tests never hit the
    # network, so by default no quote is available (override per test).
    monkeypatch.setattr("app.web.routes_portfolio.order_provider",
                        lambda settings: FakeProvider())
    from app import auth

    with session_scope() as s:
        auth.set_password(s, TEST_PASSWORD)
        s.commit()
    auth.throttle.reset()
    with TestClient(app) as c:
        assert c.post("/login", data={"password": TEST_PASSWORD},
                      follow_redirects=False).status_code == 303
        yield c
    dispose_all()
    get_settings.cache_clear()


def _seed_open_calls() -> dict[str, int]:
    """2 open longs, 1 open short, 1 closed call; PRC also gets 2 price bars."""
    ids: dict[str, int] = {}
    with session_scope() as s:
        prc = ensure_ticker(s, "PRC")
        ids["PRC"] = _rec(s, prc.id, "long", "80")
        amc = ensure_ticker(s, "AMC")
        ids["AMC"] = _rec(s, amc.id, "long", "70")
        sht = ensure_ticker(s, "SHT")
        ids["SHT"] = _rec(s, sht.id, "short", "75")
        cls = ensure_ticker(s, "CLS")
        ids["CLS"] = _rec(s, cls.id, "long", "65", status="closed")
        s.add(PriceHistory(ticker_id=prc.id, date=dt.date(2026, 1, 2),
                           close=D("100.00"), source="fake"))
        s.add(PriceHistory(ticker_id=prc.id, date=dt.date(2026, 1, 9),
                           close=D("101.50"), source="fake"))
        s.commit()
    return ids


def _rec(session, ticker_id: int, direction: str, confidence: str,
         *, status: str = "open") -> int:
    rec = Recommendation(
        ticker_id=ticker_id, direction=direction, horizon="medium",
        entry_price_at_call=D("100"), stop_price=D("90"), target_price=D("125"),
        confidence=D(confidence), rationale=(
            "Value: P/B of 2.50 scores 73.08/100 - lower numbers are better "
            "(0.5 earns 100, 8 earns 0)"),
        generated_at=BASE, status=status, evidence_json=[], engine_version="rules-v1",
    )
    session.add(rec)
    session.flush()
    return rec.id


# ------------------------------------------------------------------ rendering


PAGES = ["/", "/recommendations", "/investments", "/portfolio", "/screen", "/settings"]


def test_every_page_renders(client):
    _seed_open_calls()
    for path in PAGES:
        resp = client.get(path)
        assert resp.status_code == 200, f"{path} -> {resp.status_code}"
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/api/status").status_code == 200


def test_research_page_renders_for_seeded_ticker(client):
    _seed_open_calls()
    assert client.get("/research/PRC").status_code == 200


# --------------------------------------------------------- table well-formedness


class _Tables(HTMLParser):
    """Collect every table as rows of (tag, colspan) - enough to pin shape."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[tuple[str, int]]]] = []
        self._table = None
        self._row = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
            self._table.append(self._row)
        elif tag in ("td", "th") and self._row is not None:
            self._row.append((tag, int(dict(attrs).get("colspan", 1))))

    def handle_endtag(self, tag):
        if tag == "table":
            self.tables.append(self._table)
            self._table = self._row = None


def _tables_in(html: str):
    parser = _Tables()
    parser.feed(html)
    return parser.tables


def test_all_tables_have_consistent_columns(client):
    """Colspan-sum of every row == header row's. Catches the recurring bug of
    adding a column and forgetting to update the empty-state colspan."""
    _seed_open_calls()
    checked = 0
    for path in PAGES:
        for table in _tables_in(client.get(path).text):
            assert table, f"{path}: empty <table>"
            width = sum(c for _, c in table[0])
            assert width >= 1
            for row in table[1:]:
                assert sum(c for _, c in row) == width, f"{path}: ragged row {row}"
            checked += 1
    assert checked >= 6  # sanity: the pages really do contain tables


def test_wide_tables_are_wrapped_for_horizontal_scroll(client):
    """10+ column tables must sit inside .tablewrap and carry .wide."""
    _seed_open_calls()
    html = client.get("/recommendations").text
    assert '<div class="tablewrap">' in html
    assert '<table class="wide">' in html


# ------------------------------------------------------- action-button contract


def _row_action_blocks(html: str) -> list[str]:
    return re.findall(r'<div class="row-actions">(.*?)</div>', html, re.S)


def _btn_labels(fragment: str) -> list[str]:
    return [lbl.strip()
            for lbl in re.findall(r'class="btn[^"]*"[^>]*>([^<]+)<', fragment)]


def test_open_calls_get_one_tidy_action_cell(client):
    _seed_open_calls()
    html = client.get("/recommendations").text
    blocks = _row_action_blocks(html)
    assert len(blocks) == 3, "one action cell per OPEN call (2 long, 1 short)"
    for block in blocks:
        labels = _btn_labels(block)
        assert labels[0] == "simulate"
        assert "close" in labels and "dismiss" in labels
        assert 'hx-post="/recommendations/' in block
    # "I bought it" only on longs (real ledger is long-only); count the link,
    # not the label (the legend repeats the label).
    assert html.count('href="/investments?rec=') == 2
    # every recommendation row has an actions column (closed ones: empty);
    # 4 = all seeded rows: 3 open with buttons + 1 closed empty cell
    assert html.count('<td class="actions">') == 4


def test_no_stale_button_labels_anywhere(client):
    """Labels retired after UX confusion must never come back."""
    _seed_open_calls()
    for path in PAGES:
        for btn in _btn_labels(client.get(path).text):
            assert btn not in {"paper", "bought", "I bought this", "execute"}, (
                f"{path}: stale button label {btn!r}")


def test_action_legend_visible(client):
    _seed_open_calls()
    for path in ("/", "/recommendations"):
        html = client.get(path).text
        assert "virtual order in the paper account" in html, path


def test_dashboard_top_ideas_matches_recommendations_contract(client):
    _seed_open_calls()
    html = client.get("/").text
    blocks = _row_action_blocks(html)
    assert len(blocks) == 3
    assert html.count('href="/portfolio?prefill=') == 3
    assert html.count('href="/investments?rec=') == 2


# ------------------------------------------------------------- CSS integrity


def _html_classes(html: str) -> set[str]:
    out: set[str] = set()
    for match in re.finditer(r'class="([^"]+)"', html):
        out.update(c for c in match.group(1).split() if c)
    return out


def test_every_class_used_by_templates_has_a_css_rule(client):
    """The old failure mode: template references a class, app.css never got
    the rule (or the browser served a cached app.css). No whitelist needed -
    every emitted class is styled."""
    _seed_open_calls()
    css = client.get("/static/app.css").text
    styled = set(re.findall(r"\.([a-zA-Z][\w-]*)", css))
    for path in PAGES:
        missing = _html_classes(client.get(path).text) - styled
        assert not missing, f"{path}: unstyled classes {sorted(missing)}"


def test_stylesheet_link_is_cache_busted(client):
    """Browsers served stale app.css; the ?v= query must stay (and only the
    digits may change)."""
    _seed_open_calls()
    html = client.get("/recommendations").text
    assert re.search(r'<link rel="stylesheet" href="/static/app\.css\?v=\d+">', html)


def test_rationale_is_a_native_details_disclosure(client):
    """Must not depend on CDN JS - the user's browser doesn't run it."""
    _seed_open_calls()
    html = client.get("/recommendations").text
    assert '<details class="rationale">' in html
    assert "x-data" not in html  # Alpine toggles regressed once already


# --------------------------------------------------------------- behaviour


def test_close_and_dismiss_update_status_and_redirect(client):
    ids = _seed_open_calls()
    resp = client.post(f"/recommendations/{ids['AMC']}/dismiss")
    assert resp.headers["HX-Redirect"] == "/recommendations"
    resp = client.post(f"/recommendations/{ids['SHT']}/close")
    assert resp.headers["HX-Redirect"] == "/recommendations"
    with session_scope() as s:
        assert s.get(Recommendation, ids["AMC"]).status == "dismissed"
        assert s.get(Recommendation, ids["SHT"]).status == "closed"


def test_investments_prefill_uses_latest_stored_close(client):
    """'I bought it' prefills the newest close (101.50), not the call price."""
    ids = _seed_open_calls()
    html = client.get(f"/investments?rec={ids['PRC']}").text
    match = re.search(r'name="price"[^>]*\svalue="([^"]+)"', html, re.S)
    assert match and match.group(1).startswith("101.5"), html[html.find("name=\"price\""):][:200]
    assert 'value="PRC"' in html


# ------------------------------------------------ paper vs real + cost readout


def _live_quote(monkeypatch, symbol: str, price: str) -> None:
    """Make the order ticket's freshness refresh return this live quote."""
    from app.providers.base import Quote
    from tests.conftest import FakeProvider

    quote = Quote(symbol=symbol, price=D(price), as_of=dt.datetime.now(dt.timezone.utc))
    monkeypatch.setattr("app.web.routes_portfolio.order_provider",
                        lambda settings: FakeProvider(quote=quote))


def test_paper_fill_lands_on_portfolio_not_investments(client, monkeypatch):
    """The AIZ confusion: simulated buys are PAPER and must surface on the
    Portfolio page only - the Investments page is the real-money ledger."""
    _seed_open_calls()
    _live_quote(monkeypatch, "PRC", "102.00")
    resp = client.post("/portfolio/order",
                       data={"symbol": "PRC", "side": "buy", "qty": "6"})
    assert resp.status_code == 200
    assert "PAPER account" in resp.text
    port = client.get("/portfolio").text
    assert ">PRC</td>" in port  # position row rendered
    inv = client.get("/investments").text
    assert ">PRC</td>" not in inv  # nowhere in the real lots/tables
    assert "Paper account" in inv  # and the page says where paper trades live


def test_cost_readouts_wired_on_both_tickets(client):
    """Both order forms must expose qty/price-driven cost elements."""
    _seed_open_calls()
    port = client.get("/portfolio").text
    assert 'data-cost="paper-cost"' in port and 'id="paper-cost"' in port
    assert re.search(r'data-close="101\.5', port)  # PRC latest close available
    inv = client.get("/investments").text
    assert 'data-cost="trade-cost"' in inv and 'id="trade-cost"' in inv
    assert re.search(r'data-close="101\.5', inv)


def test_cost_prefilled_server_side_without_js(client):
    """Progressive enhancement: prefill cases render a real number even if
    the browser runs no scripts at all."""
    ids = _seed_open_calls()
    port = client.get(f"/portfolio?prefill={ids['PRC']}").text
    span = re.search(r'id="paper-cost">([^<]+)<', port)
    assert span and span.group(1).startswith("$"), port[max(0, port.find('id="paper-cost"') - 120):][:240]
    inv = client.get(f"/investments?rec={ids['PRC']}").text
    span = re.search(r'id="trade-cost">([^<]+)<', inv)
    assert span and span.group(1).startswith("$")


def test_cost_script_is_local_not_cdn(client):
    """The cost readout must not depend on the CDN scripts that fail in some
    browsers - it ships as a local, cache-busted file."""
    _seed_open_calls()
    html = client.get("/portfolio").text
    assert re.search(r'<script defer src="/static/app\.js\?v=\d+">','' + html)
    assert client.get("/static/app.js").status_code == 200


def test_cross_site_posts_are_refused(client):
    """No login exists, so another site must not be able to submit our forms."""
    form = {"symbol": "XSRF", "side": "buy", "qty": "1", "price": "10"}
    evil = client.post("/investments/trade", data=form, follow_redirects=False,
                       headers={"Origin": "https://evil.example"})
    assert evil.status_code == 403
    fetch_meta = client.post("/investments/trade", data=form, follow_redirects=False,
                             headers={"Sec-Fetch-Site": "cross-site"})
    assert fetch_meta.status_code == 403
    same = client.post("/investments/trade", data=form, follow_redirects=False,
                       headers={"Origin": "http://testserver"})
    assert same.status_code == 303


def test_error_flash_is_query_encoded(client):
    """A message with '&' or '#' must survive the redirect intact."""
    resp = client.post("/investments/trade", follow_redirects=False, data={
        "symbol": "ENC", "side": "sell", "qty": "1", "price": "10"})
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert " " not in location and location.startswith("/investments?err=")
    page = client.get(location)
    assert "cannot record sell of ENC" in page.text


def test_stale_price_order_is_refused_without_a_live_quote(client):
    """PRC's newest bar is from January; with no quote the ticket must refuse."""
    _seed_open_calls()
    resp = client.post("/portfolio/order", data={"symbol": "PRC", "side": "buy", "qty": "1"})
    assert "stale price" in resp.text and "2026-01-09" in resp.text
    assert "PAPER account: filled" not in resp.text


def test_stale_price_order_fills_at_the_live_quote(client, monkeypatch):
    _seed_open_calls()
    _live_quote(monkeypatch, "PRC", "120.00")
    resp = client.post("/portfolio/order", data={"symbol": "PRC", "side": "buy", "qty": "1"})
    assert "PAPER account: filled buy 1 PRC @ 120.06" in resp.text  # 120 + 5 bps slippage
    assert "live quote" in resp.text


def test_ticket_shows_price_age(client):
    _seed_open_calls()
    html = client.get("/portfolio").text
    assert 'data-asof="2026-01-09"' in html and 'data-session="' in html
    assert 'id="paper-asof"' in html
