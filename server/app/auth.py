"""Admin authentication: signed-cookie sessions, CSRF and the first-login gate.

Authentication uses a signed session cookie (Starlette ``SessionMiddleware``,
which signs with ``itsdangerous``).  All state-changing API calls additionally
require an ``X-CSRF-Token`` header that matches a random token stored in the
session, defeating cross-site request forgery.

The appliance ships with the account ``admin``/``admin``; that account can do
exactly one thing until the password is changed — see
:class:`PasswordChangeGuardMiddleware`.
"""
from __future__ import annotations

import secrets
from typing import Optional

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import JSONResponse

from .config import AppConfig
from .security import constant_time_eq
from .users import UserService

SESSION_USER = "u"
SESSION_CSRF = "c"

# Endpoints that must stay reachable while the password still is the shipped
# default (plus the static assets the login/change form is built from).
PASSWORD_CHANGE_ALLOWED = {
    "/api/login",
    "/api/logout",
    "/api/me",
    "/api/password",
    "/healthz",
}
PASSWORD_CHANGE_ALLOWED_PREFIXES = ("/static/",)


def install_session(app, config: AppConfig) -> None:
    app.add_middleware(
        SessionMiddleware,
        secret_key=config.secret_key,
        session_cookie=config.security.cookie_name,
        max_age=config.security.session_max_age_hours * 3600,
        same_site="lax",
        https_only=config.security.cookie_secure,
    )


class PasswordChangeGuardMiddleware(BaseHTTPMiddleware):
    """Refuse every administrative call until the default password is replaced.

    The session cookie is read by the session middleware, which therefore has to
    run *before* this one (it is added after it in ``create_admin_app``).
    """

    async def dispatch(self, request, call_next):
        path = request.url.path
        if path in PASSWORD_CHANGE_ALLOWED or path.startswith(PASSWORD_CHANGE_ALLOWED_PREFIXES):
            return await call_next(request)
        if not path.startswith("/api/"):
            return await call_next(request)

        user = request.session.get(SESSION_USER) if "session" in request.scope else None
        if user:
            users: UserService = request.app.state.users
            if users.must_change(user):
                return JSONResponse(
                    {
                        "error": "password_change_required",
                        "detail": "Сначала смените пароль по умолчанию.",
                    },
                    status_code=428,
                )
        return await call_next(request)


def authenticate(users: UserService, username: str, password: str) -> bool:
    return users.authenticate(username, password)


def start_session(request: Request, username: str) -> str:
    request.session.clear()
    request.session[SESSION_USER] = username
    request.session[SESSION_CSRF] = secrets.token_urlsafe(24)
    return request.session[SESSION_CSRF]


def end_session(request: Request) -> None:
    request.session.clear()


def current_user(request: Request) -> Optional[str]:
    return request.session.get(SESSION_USER)


def current_csrf(request: Request) -> Optional[str]:
    return request.session.get(SESSION_CSRF)


def require_user(request: Request) -> str:
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


def require_csrf(request: Request) -> None:
    config: AppConfig = request.app.state.config
    if not config.security.csrf_enabled:
        return
    header = request.headers.get("x-csrf-token")
    session_token = request.session.get(SESSION_CSRF)
    if not header or not session_token or not constant_time_eq(header, session_token):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token")
