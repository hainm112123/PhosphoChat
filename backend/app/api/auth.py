"""Login, logout, own password, and (admin) user management."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from app.auth.deps import COOKIE, auth_store, current_user, require_admin, throttle
from app.auth.store import AuthError, Conflict, User
from app.config import get_settings

router = APIRouter(prefix="/api")


class LoginIn(BaseModel):
    username: str
    password: str


class PasswordIn(BaseModel):
    current: str
    new: str


class NewUser(BaseModel):
    username: str
    password: str
    role: Literal["user", "admin"] = "user"


class UserPatch(BaseModel):
    role: Literal["user", "admin"] | None = None
    password: str | None = None
    disabled: bool | None = None


def _or_http(fn):
    try:
        return fn()
    except Conflict as e:
        raise HTTPException(409, str(e)) from e
    except AuthError as e:
        raise HTTPException(400, str(e)) from e
    except KeyError:
        raise HTTPException(404, "No such user.") from None


# --------------------------------------------------------------------------- session

@router.get("/auth/status")
def status() -> dict:
    return {"needs_admin": not auth_store().has_admin()}


@router.post("/auth/login")
def login(body: LoginIn, request: Request, response: Response) -> dict:
    client = request.client.host if request.client else "?"
    if throttle.blocked(body.username, client):
        raise HTTPException(429, "Too many failed attempts. Wait a few minutes and try again.")
    store = auth_store()
    user = store.authenticate(body.username, body.password)
    if user is None:
        throttle.failed(body.username, client)
        raise HTTPException(401, "Wrong username or password.")
    throttle.succeeded(body.username, client)
    s = get_settings()
    response.set_cookie(COOKIE, store.create_session(user.id), max_age=s.session_days * 86400,
                        httponly=True, samesite="lax", secure=s.cookie_secure, path="/")
    return user.public()


@router.post("/auth/logout", status_code=204)
def logout(request: Request, response: Response) -> None:
    token = request.cookies.get(COOKIE)
    if token:
        auth_store().end_session(token)
    response.delete_cookie(COOKIE, path="/")


@router.get("/auth/me")
def me(user: User = Depends(current_user)) -> dict:
    return user.public()


@router.post("/auth/password", status_code=204)
def change_password(body: PasswordIn, user: User = Depends(current_user)) -> None:
    _or_http(lambda: auth_store().change_own_password(user.id, body.current, body.new))


# --------------------------------------------------------------------------- users (admin)

@router.get("/users", dependencies=[Depends(require_admin)])
def list_users() -> list[dict]:
    return [u.public() for u in auth_store().list_users()]


@router.post("/users", status_code=201, dependencies=[Depends(require_admin)])
def create_user(body: NewUser) -> dict:
    return _or_http(lambda: auth_store().create_user(body.username, body.password, body.role)).public()


@router.patch("/users/{user_id}")
def update_user(user_id: int, body: UserPatch, admin: User = Depends(require_admin)) -> dict:
    return _or_http(lambda: auth_store().update_user(
        user_id, actor_id=admin.id, role=body.role, password=body.password,
        disabled=body.disabled)).public()


@router.delete("/users/{user_id}", status_code=204)
def delete_user(user_id: int, admin: User = Depends(require_admin)) -> None:
    _or_http(lambda: auth_store().delete_user(user_id, actor_id=admin.id))
