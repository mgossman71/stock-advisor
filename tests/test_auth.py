"""Password protection: fails closed, cookies are tamper-proof, logins throttled."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.cli import main as cli_main
from app.config import get_settings
from app.db.base import Base, dispose_all, get_engine, session_scope

PASSWORD = "correct horse battery staple"


@pytest.fixture
def anon(tmp_path, monkeypatch):
    """Signed-out TestClient on a throwaway DB with no password set."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'auth.db'}")
    monkeypatch.setenv("SCHEDULER_ENABLED", "0")
    get_settings.cache_clear()
    dispose_all()
    Base.metadata.create_all(get_engine())
    auth.throttle.reset()
    from app.main import app

    with TestClient(app) as c:
        yield c
    dispose_all()
    get_settings.cache_clear()


def _set(password: str = PASSWORD) -> None:
    with session_scope() as s:
        auth.set_password(s, password)
        s.commit()


def _login(client, password: str = PASSWORD, **extra):
    return client.post("/login", data={"password": password, **extra}, follow_redirects=False)


# ------------------------------------------------------------------ units

def test_hash_round_trip_and_rejects():
    stored = auth.hash_password("s3cret-enough!")
    assert stored.startswith("scrypt$") and "s3cret" not in stored
    assert auth.verify_password("s3cret-enough!", stored)
    assert not auth.verify_password("s3cret-enough?", stored)
    assert not auth.verify_password("x", None)
    assert not auth.verify_password("x", "garbage")
    assert auth.hash_password("same") != auth.hash_password("same")  # salted


def test_tokens_are_tamper_evident_and_expire():
    token = auth.make_token("k", now=1000)
    assert auth.check_token(token, "k", now=1001)
    assert not auth.check_token(token, "other-secret", now=1001)
    assert not auth.check_token(token, "k", now=1000 + auth.SESSION_SECONDS + 1)
    expiry, nonce, sig = token.split(".")
    forged = f"{int(expiry) + 10**6}.{nonce}.{sig}"
    assert not auth.check_token(forged, "k", now=1001)
    for junk in (None, "", "a.b", "x.y.z"):
        assert not auth.check_token(junk, "k")


def test_safe_next_blocks_open_redirects():
    assert auth.safe_next("/research/AAPL?x=1") == "/research/AAPL?x=1"
    for bad in ("https://evil.com", "//evil.com", "/\\evil.com", "", None, "evil"):
        assert auth.safe_next(bad) == "/"


def test_throttle_window():
    t = auth.LoginThrottle(max_failures=3, window_seconds=60)
    for i in range(3):
        assert not t.blocked("ip", now=100 + i)
        t.fail("ip", now=100 + i)
    assert t.blocked("ip", now=110)
    assert not t.blocked("other", now=110)
    assert not t.blocked("ip", now=170)  # failures aged out


# ---------------------------------------------------------------- the app

def test_may_bootstrap_only_direct_local_requests():
    assert auth.may_bootstrap("127.0.0.1", {})
    assert auth.may_bootstrap("192.168.1.20", {})
    assert auth.may_bootstrap("::1", {})
    assert not auth.may_bootstrap("8.8.8.8", {})
    # A tunnel/proxy connects from loopback but forwards internet clients.
    assert not auth.may_bootstrap("127.0.0.1", {"x-forwarded-for": "8.8.8.8"})
    assert not auth.may_bootstrap("127.0.0.1", {"cf-connecting-ip": "8.8.8.8"})
    assert not auth.may_bootstrap(None, {})


def test_fails_closed_without_a_password(anon):
    resp = anon.get("/", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/login?next=/"
    assert _login(anon, "anything at all").status_code == 503
    # Through a proxy: no setup form, only the CLI instructions.
    page = anon.get("/login", headers={"X-Forwarded-For": "8.8.8.8"}).text
    assert "No password is set" in page and "app.cli set-password" in page
    assert 'action="/setup"' not in page
    refused = anon.post("/setup", data={"password": PASSWORD, "confirm": PASSWORD},
                        headers={"X-Forwarded-For": "8.8.8.8"})
    assert refused.status_code == 403
    assert anon.get("/", follow_redirects=False).status_code == 303  # still locked


def test_first_run_setup_from_the_browser(anon):
    page = anon.get("/login").text
    assert "Create your password" in page and 'action="/setup"' in page
    bad = anon.post("/setup", data={"password": PASSWORD, "confirm": "different!!!!"})
    assert bad.status_code == 400 and "do not match" in bad.text
    short = anon.post("/setup", data={"password": "short", "confirm": "short"})
    assert short.status_code == 400 and "at least 12" in short.text
    resp = anon.post("/setup", data={"password": PASSWORD, "confirm": PASSWORD},
                     follow_redirects=False)
    assert resp.status_code == 303 and auth.COOKIE_NAME in resp.headers["set-cookie"]
    assert anon.get("/", follow_redirects=False).status_code == 200  # signed in
    # Setup is one-shot: it can't overwrite an existing password.
    again = anon.post("/setup", data={"password": "attacker passphrase",
                                      "confirm": "attacker passphrase"})
    assert again.status_code == 409
    assert "Sign in" in anon.get("/login").text


def test_change_password_on_settings_page(anon):
    _set()
    _login(anon)
    html = anon.get("/settings").text
    assert 'action="/settings/password"' in html and "Sign out" in html
    wrong = anon.post("/settings/password", follow_redirects=False,
                      data={"current": "nope", "password": "x" * 12, "confirm": "x" * 12})
    assert wrong.headers["location"].startswith("/settings?pw=wrong")
    assert "Current password is wrong" in anon.get("/settings?pw=wrong").text
    mismatch = anon.post("/settings/password", follow_redirects=False,
                         data={"current": PASSWORD, "password": "x" * 12, "confirm": "y" * 12})
    assert mismatch.headers["location"].startswith("/settings?pw=mismatch")
    ok = anon.post("/settings/password", follow_redirects=False,
                   data={"current": PASSWORD, "password": "new passphrase!",
                         "confirm": "new passphrase!"})
    assert ok.headers["location"].startswith("/settings?pw=changed")
    assert anon.get("/settings", follow_redirects=False).status_code == 200  # still in
    anon.post("/logout")
    assert _login(anon).status_code == 401
    assert _login(anon, "new passphrase!").status_code == 303


def test_every_route_but_login_health_static_is_locked(anon):
    _set()
    assert anon.get("/healthz").status_code == 200
    assert anon.get("/static/app.css").status_code == 200
    icon = anon.get("/favicon.ico")
    assert icon.status_code == 200 and icon.headers["content-type"] == "image/x-icon"
    assert anon.get("/static/favicon.svg").status_code == 200
    assert anon.get("/login").status_code == 200
    for path in ("/", "/settings", "/research/AAPL", "/portfolio", "/docs"):
        resp = anon.get(path, follow_redirects=False)
        assert resp.status_code == 303, path
        assert resp.headers["location"].startswith("/login?next=")
    for path in ("/api/status", "/api/export.json"):
        assert anon.get(path).status_code == 401
    write = anon.post("/settings/llm", data={"llm_base_url": "http://evil"},
                      follow_redirects=False)
    assert write.status_code == 303 and write.headers["location"].startswith("/login")


def test_htmx_requests_get_hx_redirect(anon):
    _set()
    resp = anon.post("/jobs/refresh", headers={"HX-Request": "true"})
    assert resp.status_code == 401
    assert resp.headers["HX-Redirect"] == "/login?next=/jobs/refresh"


def test_login_sets_httponly_cookie_and_honours_next(anon):
    _set()
    assert _login(anon, "wrong password!!").status_code == 401
    resp = _login(anon, next="/settings")
    assert resp.status_code == 303 and resp.headers["location"] == "/settings"
    cookie = resp.headers["set-cookie"]
    assert "HttpOnly" in cookie and "samesite=lax" in cookie.lower()
    assert "Secure" not in cookie  # plain http test server, setting off
    assert anon.get("/settings").status_code == 200
    assert _login(anon, next="https://evil.com").headers["location"] == "/"


def test_cookie_secure_setting(anon, monkeypatch):
    _set()
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "1")
    get_settings.cache_clear()
    assert "Secure" in _login(anon).headers["set-cookie"]


def test_logout_and_password_change_end_sessions(anon):
    _set()
    _login(anon)
    assert anon.get("/", follow_redirects=False).status_code == 200
    anon.post("/logout")
    assert anon.get("/", follow_redirects=False).status_code == 303

    _login(anon)
    _set("a brand new passphrase")  # rotates the session secret
    assert anon.get("/", follow_redirects=False).status_code == 303
    assert _login(anon).status_code == 401
    assert _login(anon, "a brand new passphrase").status_code == 303


def test_repeated_failures_lock_the_client_out(anon):
    _set()
    for _ in range(auth.throttle.max_failures):
        assert _login(anon, "not the password").status_code == 401
    assert _login(anon).status_code == 429  # even the right password, for now
    auth.throttle.reset()
    assert _login(anon).status_code == 303


def test_cli_sets_password_from_stdin(anon, monkeypatch, capsys):
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("too short\n"))
    assert cli_main(["set-password"]) == 1
    monkeypatch.setattr("sys.stdin", io.StringIO(f"{PASSWORD}\n"))
    assert cli_main(["set-password"]) == 0
    assert "password set" in capsys.readouterr().out
    assert _login(anon).status_code == 303
