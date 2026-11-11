"""Login / logout.  Enforcement itself is the ``require_login`` middleware."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app import auth
from app.config import get_settings
from app.db.base import session_scope
from app.web.common import templates

router = APIRouter()


def _login_page(request: Request, *, error: str | None = None, status_code: int = 200,
                next_path: str = "/"):
    with session_scope() as session:
        password_hash, _ = auth.load_auth(session)
    return templates.TemplateResponse(
        request=request, name="login.html", status_code=status_code,
        context={"error": error, "next": next_path, "configured": password_hash is not None,
                 "min_length": auth.MIN_PASSWORD_LENGTH},
    )


@router.get("/login")
def login_form(request: Request, next: str = "/"):
    return _login_page(request, next_path=auth.safe_next(next))


@router.post("/login")
def login(request: Request, password: str = Form(""), next: str = Form("/")):
    client = request.client.host if request.client else "unknown"
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
    response = RedirectResponse(next_path, status_code=303)
    response.set_cookie(
        auth.COOKIE_NAME, auth.make_token(secret), max_age=auth.SESSION_SECONDS,
        httponly=True, samesite="lax", path="/",
        secure=get_settings().session_cookie_secure or request.url.scheme == "https",
    )
    return response


@router.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return response
