"""FastAPI dependencies and middleware for auth.

`current_user` (any logged-in user) and `require_admin` (vault management, user admin) are
the only gates; routers attach them, so a route can't be reached without one.
"""

from __future__ import annotations

from urllib.parse import urlparse

from fastapi import Depends, HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.auth.store import AuthStore, LoginThrottle, User
from app.config import get_settings

COOKIE = "pc_session"

_store: AuthStore | None = None
_pinned = False  # set_store() was used: keep that store regardless of settings
throttle = LoginThrottle()


def auth_store() -> AuthStore:
    global _store
    if _pinned and _store is not None:
        return _store
    s = get_settings()
    if _store is None or _store.path != s.db_path:
        _store = AuthStore(s.db_path, s.session_days)
    return _store


def set_store(store: AuthStore | None) -> None:
    """Tests point the app at a temporary database (None goes back to DATA_DIR)."""
    global _store, _pinned
    _store, _pinned = store, store is not None


def current_user(request: Request) -> User:
    token = request.cookies.get(COOKIE)
    user = auth_store().session_user(token) if token else None
    if user is None:
        raise HTTPException(401, "Log in to continue.")
    request.state.user = user
    return user


def require_admin(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(403, "Only admins can do this.")
    return user


class SameOriginMiddleware(BaseHTTPMiddleware):
    """Reject state-changing API requests sent from another site. The session cookie is
    SameSite=Lax already; this also covers browsers or setups where that isn't enough."""

    async def dispatch(self, request: Request, call_next):
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.url.path.startswith("/api/"):
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.headers.get("host", ""):
                return JSONResponse({"detail": "Cross-site request refused."}, status_code=403)
        return await call_next(request)
