"""Login / logout / first-run setup / password change.

Enforcement itself is the ``require_login`` middleware in ``app/main.py``.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app import auth
from app.config import get_settings
from app.db.base import session_scope
from app.web.common import templates

router = APIRouter()


def _peer(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _signed_in(request: Request, secret: str, target: str) -> RedirectResponse:
    """Redirect to ``target`` carrying a fresh session cookie."""
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(
        auth.COOKIE_NAME, auth.make_token(secret), max_age=auth.SESSION_SECONDS,
        httponly=True, samesite="lax", path="/",
        secure=get_settings().session_cookie_secure or request.url.scheme == "https",
    )
    return response


def _login_page(request: Request, *, error: str | None = None, status_code: int = 200,
                next_path: str = "/"):
    with session_scope() as session:
        password_hash, _ = auth.load_auth(session)
    return templates.TemplateResponse(
        request=request, name="login.html", status_code=status_code,
        context={"error": error, "next": next_path, "configured": password_hash is not None,
                 "can_setup": auth.may_bootstrap(_peer(request), request.headers),
                 "min_length": auth.MIN_PASSWORD_LENGTH},
    )


@router.get("/login")
def login_form(request: Request, next: str = "/"):
    return _login_page(request, next_path=auth.safe_next(next))


@router.post("/login")
def login(request: Request, password: str = Form(""), next: str = Form("/")):
    client = _peer(request)
    next_path = auth.safe_next(next)
    if auth.throttle.blocked(client):
        return _login_page(request, status_code=429, next_path=next_path,
                           error="Too many failed attempts. Try again in 15 minutes.")
    with session_scope() as session:
        password_hash, secret = auth.load_auth(session)
    if password_hash is None or secret is None:
        return _login_page(request, status_code=503, next_path=next_path,
                           error="No password is set yet.")
    if not auth.verify_password(password, password_hash):
        auth.throttle.fail(client)
        return _login_page(request, status_code=401, next_path=next_path,
                           error="Wrong password.")
    auth.throttle.reset(client)
    return _signed_in(request, secret, next_path)


@router.post("/setup")
def first_run_setup(request: Request, password: str = Form(""), confirm: str = Form("")):
    """Create the first password from the browser (local/LAN requests only)."""
    if not auth.may_bootstrap(_peer(request), request.headers):
        return _login_page(request, status_code=403,
                           error="The first password can only be set from this machine "
                                 "or your local network, or with the CLI.")
    if password != confirm:
        return _login_page(request, status_code=400, error="The passwords do not match.")
    with session_scope() as session:
        if auth.load_auth(session)[0] is not None:
            return _login_page(request, status_code=409, error="A password is already set.")
        try:
            auth.set_password(session, password)
        except ValueError as exc:
            return _login_page(request, status_code=400, error=str(exc).capitalize() + ".")
        session.commit()
        _, secret = auth.load_auth(session)
    return _signed_in(request, secret, "/")


@router.post("/settings/password")
def change_password(request: Request, current: str = Form(""), password: str = Form(""),
                    confirm: str = Form("")):
    """Change the password; every other session is signed out, this one stays."""
    client = _peer(request)
    if auth.throttle.blocked(client):
        return RedirectResponse("/settings?pw=locked#password", status_code=303)
    with session_scope() as session:
        password_hash, _ = auth.load_auth(session)
        if not auth.verify_password(current, password_hash):
            auth.throttle.fail(client)
            return RedirectResponse("/settings?pw=wrong#password", status_code=303)
        if password != confirm:
            return RedirectResponse("/settings?pw=mismatch#password", status_code=303)
        try:
            auth.set_password(session, password)
        except ValueError:
            return RedirectResponse("/settings?pw=short#password", status_code=303)
        session.commit()
        _, secret = auth.load_auth(session)
    return _signed_in(request, secret, "/settings?pw=changed#password")


@router.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return response
