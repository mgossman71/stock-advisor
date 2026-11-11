"""Single-user password protection: scrypt hash + HMAC-signed session cookie.

Standard library only.  State lives in ``app_settings``:

* ``auth_password_hash`` - ``scrypt$n$r$p$salt$hash`` (base64 parts).
* ``auth_session_secret`` - random HMAC key for session cookies.  It is
  rotated on every password change, which logs out every existing session.

Fails closed: with no password set, every protected route redirects to the
login page, which explains how to set one (``python -m app.cli set-password``).
There is deliberately no "auth off" switch - the app is meant to be exposed.
"""

from __future__ import annotations

import base64
import collections
import datetime as dt
import hashlib
import hmac
import secrets
import threading
import time

from sqlalchemy.orm import Session

from app.db.models import AppSetting

COOKIE_NAME = "sa_session"
SESSION_SECONDS = 14 * 24 * 3600
MIN_PASSWORD_LENGTH = 12

HASH_KEY = "auth_password_hash"
SECRET_KEY = "auth_session_secret"

#: scrypt cost: ~50-100 ms per check, 32 MiB of memory.
_N, _R, _P = 2 ** 15, 8, 1
_MAXMEM = 64 * 1024 * 1024


# ------------------------------------------------------------------ hashing

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P,
                            maxmem=_MAXMEM, dklen=32)
    return "$".join(["scrypt", str(_N), str(_R), str(_P),
                     base64.b64encode(salt).decode(), base64.b64encode(digest).decode()])


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n),
                                r=int(r), p=int(p), maxmem=_MAXMEM, dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# --------------------------------------------------------------- persistence

def load_auth(session: Session) -> tuple[str | None, str | None]:
    """``(password_hash, session_secret)``; either may be ``None``."""
    rows = {s.key: s.value for s in session.query(AppSetting)
            .filter(AppSetting.key.in_((HASH_KEY, SECRET_KEY)))}
    return rows.get(HASH_KEY), rows.get(SECRET_KEY)


def set_password(session: Session, password: str) -> None:
    """Store a new password hash and rotate the session secret (caller commits)."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    now = dt.datetime.now(dt.timezone.utc)
    for key, value in ((HASH_KEY, hash_password(password)),
                       (SECRET_KEY, secrets.token_urlsafe(32))):
        row = session.query(AppSetting).filter_by(key=key).first()
        if row is None:
            row = AppSetting(key=key, value=value)
            session.add(row)
        row.value = value
        row.updated_at = now


# ------------------------------------------------------------------ cookies

def make_token(secret: str, *, now: float | None = None) -> str:
    """``<expiry>.<nonce>.<hmac>`` - nothing secret inside, tamper-evident."""
    expiry = int((now if now is not None else time.time()) + SESSION_SECONDS)
    body = f"{expiry}.{secrets.token_urlsafe(9)}"
    return f"{body}.{_sign(secret, body)}"


def check_token(token: str | None, secret: str | None, *, now: float | None = None) -> bool:
    if not token or not secret:
        return False
    try:
        expiry, nonce, signature = token.split(".")
        alive = int(expiry) > (now if now is not None else time.time())
    except ValueError:
        return False
    return alive and hmac.compare_digest(signature, _sign(secret, f"{expiry}.{nonce}"))


def _sign(secret: str, body: str) -> str:
    mac = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")


def safe_next(target: str | None) -> str:
    """Only same-site relative paths survive the login redirect (no open redirect)."""
    if not target or not target.startswith("/") or target.startswith("//") \
            or "\\" in target:
        return "/"
    return target


# --------------------------------------------------------------- throttling

class LoginThrottle:
    """Failed logins per client address in a sliding window (in memory).

    Keyed on the socket peer, not ``X-Forwarded-For``: a header the client
    controls can't be allowed to reset its own counter.  Behind a reverse
    proxy every client shares the proxy's address, so the limit then acts
    globally - acceptable for a single-user app.
    """

    def __init__(self, max_failures: int = 10, window_seconds: int = 900):
        self.max_failures = max_failures
        self.window = window_seconds
        self._failures: dict[str, collections.deque] = {}
        self._lock = threading.Lock()

    def blocked(self, client: str, *, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        with self._lock:
            recent = self._failures.get(client)
            while recent and recent[0] <= now - self.window:
                recent.popleft()
            return bool(recent) and len(recent) >= self.max_failures

    def fail(self, client: str, *, now: float | None = None) -> None:
        with self._lock:
            self._failures.setdefault(client, collections.deque()).append(
                now if now is not None else time.time())

    def reset(self, client: str | None = None) -> None:
        with self._lock:
            if client is None:
                self._failures.clear()
            else:
                self._failures.pop(client, None)


throttle = LoginThrottle()
