"""Login, the current user, per-user API keys, and admin user management.

Login sets an HttpOnly cookie. It does not mint an API key. ``is_admin`` is
only for creating users and keys. It does not open another user's chats.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from agentforge.tools.sql_schema_tool import clear_schema_cache_for_user
from app.security import auth_enabled
from app.services.qdrant_owner import delete_personal_points
from web.server.identity import (
    ADMIN_USER_ID,
    COOKIE_NAME,
    SESSION_DAYS,
    current_user_id,
    hash_password,
    normalize_username,
)

from .api import get_db, get_upload_base
from .api_user_data import clear_session_attachments
from .conversation_memory import get_conversation_memory
from .ws_endpoint import get_runtime

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])
users_router = APIRouter(prefix="/api/users", tags=["users"])

_COOKIE_MAX_AGE = SESSION_DAYS * 24 * 60 * 60


class LoginRequest(BaseModel):
    username: str
    password: str


class KeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    user_id: str | None = None


class UserCreate(BaseModel):
    username: str
    password: str = Field(min_length=8, max_length=200)


class UserUpdate(BaseModel):
    username: str | None = None
    password: str | None = None


class ImpersonateRequest(BaseModel):
    user_id: str


def _cookie_secure(request: Request) -> bool:
    forwarded = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    scheme = forwarded or request.url.scheme
    return scheme == "https"


def _set_session_cookie(response: Response, request: Request, raw: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        raw,
        httponly=True,
        secure=_cookie_secure(request),
        samesite="lax",
        path="/",
        max_age=_COOKIE_MAX_AGE,
    )


def _require_user():
    user_id = current_user_id()
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    user = get_db().get_user(user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    return user


def _presented_key(request: Request) -> bool:
    auth = request.headers.get("authorization", "")
    if auth[:7].lower() == "bearer " and auth[7:].strip():
        return True
    return bool((request.headers.get("x-api-key") or "").strip())


def _browser_session(request: Request):
    """The live cookie session. None when the caller presented an API key."""
    if _presented_key(request):
        return None
    raw = request.cookies.get(COOKIE_NAME)
    if not raw:
        return None
    return get_db().get_live_auth_session(raw)


def _public_user(user) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "is_admin": bool(user.is_admin),
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


def _public_key(row) -> dict:
    return {
        "id": row.id,
        "user_id": row.user_id,
        "name": row.name,
        "prefix": row.prefix,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
    }


def _check_username(username: str) -> str:
    name = normalize_username(username)
    if name is None:
        raise HTTPException(
            status_code=400,
            detail="username must be 2-32 characters: a letter, then letters, digits, _ or -",
        )
    return name


@router.post("/login")
def login(body: LoginRequest, request: Request, response: Response):
    """Check the password and set the session cookie. Does not return an API key."""
    user = get_db().authenticate(body.username, body.password)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    raw = get_db().create_auth_session(user.id)
    _set_session_cookie(response, request, raw)
    return {"auth_required": True, **_public_user(user)}


@router.post("/logout")
def logout(request: Request, response: Response):
    """Revoke the current cookie. An impersonation session does not return to the admin."""
    raw = request.cookies.get(COOKIE_NAME)
    if raw:
        get_db().revoke_session_token(raw)
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


def _me_payload(request: Request, user) -> dict:
    payload = {"auth_required": True, **_public_user(user)}
    current = _browser_session(request)
    if current is not None and current.impersonator_user_id:
        admin = get_db().get_user(current.impersonator_user_id)
        if admin is not None:
            payload["impersonator"] = _public_user(admin)
    return payload


@router.get("/me")
def me(request: Request):
    """Who is calling.

    Auth off: ``auth_required`` is false and the WebUI skips the wall.
    Auth on and anonymous: the middleware returns 401 before this runs.
    An impersonation cookie adds ``impersonator`` (the admin who started it).
    """
    if not auth_enabled():
        return {"auth_required": False}
    user = _require_user()
    return _me_payload(request, user)


@router.post("/impersonate")
def impersonate(body: ImpersonateRequest, request: Request, response: Response):
    """Replace the admin's browser session with one for another user.

    The new row keeps the admin id. An API key cannot start this, the admin
    row cannot be the target, and a session that is already impersonating
    cannot start another one.
    """
    current = _browser_session(request)
    if current is None:
        raise HTTPException(status_code=403, detail="impersonation uses the browser session")
    if current.impersonator_user_id:
        raise HTTPException(status_code=403, detail="already impersonating")
    actor = _require_user()
    if not actor.is_admin or actor.id != current.user_id:
        raise HTTPException(status_code=403, detail="not allowed")
    if body.user_id == actor.id or body.user_id == ADMIN_USER_ID:
        raise HTTPException(status_code=403, detail="cannot impersonate this user")
    target = get_db().get_user(body.user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="user not found")
    if target.is_admin:
        raise HTTPException(status_code=403, detail="cannot impersonate this user")
    raw = request.cookies.get(COOKIE_NAME) or ""
    get_db().revoke_session_token(raw)
    new_raw = get_db().create_auth_session(target.id, impersonator_user_id=actor.id)
    _set_session_cookie(response, request, new_raw)
    return {"auth_required": True, **_public_user(target), "impersonator": _public_user(actor)}


@router.post("/impersonate/stop")
def stop_impersonation(request: Request, response: Response):
    """Revoke the impersonation cookie and set a new admin session.

    The admin id comes from the row. Logout does not call this.
    """
    current = _browser_session(request)
    if current is None or not current.impersonator_user_id:
        raise HTTPException(status_code=403, detail="not impersonating")
    admin = get_db().get_user(current.impersonator_user_id)
    if admin is None or not admin.is_admin:
        raw = request.cookies.get(COOKIE_NAME) or ""
        get_db().revoke_session_token(raw)
        response.delete_cookie(COOKIE_NAME, path="/")
        raise HTTPException(status_code=403, detail="not allowed")
    raw = request.cookies.get(COOKIE_NAME) or ""
    get_db().revoke_session_token(raw)
    new_raw = get_db().create_auth_session(admin.id)
    _set_session_cookie(response, request, new_raw)
    return {"auth_required": True, **_public_user(admin)}


@router.get("/keys")
def list_keys(user_id: str | None = None):
    user = _require_user()
    target = user.id
    if user_id and user_id != user.id:
        if not user.is_admin:
            raise HTTPException(status_code=403, detail="not allowed")
        if not get_db().user_exists(user_id):
            raise HTTPException(status_code=404, detail="user not found")
        target = user_id
    return [_public_key(row) for row in get_db().list_api_keys(target)]


@router.post("/keys", status_code=201)
def create_key(body: KeyCreate):
    """Create a key. The secret is in this response only.

    A key created for someone else is stamped with their id. It is not an admin key.
    """
    user = _require_user()
    target = user.id
    if body.user_id and body.user_id != user.id:
        if not user.is_admin:
            raise HTTPException(status_code=403, detail="not allowed")
        if not get_db().user_exists(body.user_id):
            raise HTTPException(status_code=404, detail="user not found")
        target = body.user_id
    row, secret = get_db().create_api_key(target, body.name.strip())
    return {**_public_key(row), "secret": secret}


@router.delete("/keys/{key_id}")
def revoke_key(key_id: str):
    user = _require_user()
    deleted = get_db().revoke_api_key(key_id, actor_id=user.id, as_admin=bool(user.is_admin))
    if not deleted:
        raise HTTPException(status_code=404, detail="key not found")
    return {"deleted": True, "id": key_id}


@users_router.get("")
def list_users():
    user = _require_user()
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="not allowed")
    return [_public_user(row) for row in get_db().list_users()]


@users_router.post("", status_code=201)
def create_user(body: UserCreate):
    """Admin creates a non-admin user. The new user does not inherit admin."""
    user = _require_user()
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="not allowed")
    name = _check_username(body.username)
    try:
        created = get_db().create_user(name, hash_password(body.password))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _public_user(created)


def _user_error(exc: ValueError) -> HTTPException:
    text = str(exc)
    if text == "user not found":
        return HTTPException(status_code=404, detail=text)
    if text.startswith("cannot "):
        return HTTPException(status_code=403, detail=text)
    return HTTPException(status_code=409, detail=text)


@users_router.patch("/{user_id}")
def update_user(user_id: str, body: UserUpdate):
    """Admin renames a user or sets a new password. The admin row stays put."""
    actor = _require_user()
    if not actor.is_admin:
        raise HTTPException(status_code=403, detail="not allowed")
    name = _check_username(body.username) if body.username is not None else None
    password = body.password if body.password else None
    if password is not None and not 8 <= len(password) <= 200:
        raise HTTPException(status_code=400, detail="password must be 8-200 characters")
    if name is None and password is None:
        raise HTTPException(status_code=400, detail="nothing to change")
    try:
        updated = get_db().update_user(
            user_id,
            username=name,
            password_hash=hash_password(password) if password else None,
        )
    except ValueError as exc:
        raise _user_error(exc) from exc
    return _public_user(updated)


def _drop_deleted_user_runtime(user_id: str) -> None:
    """Unload that user's connector tools and schema cache. Rows are already gone."""
    try:
        rt = get_runtime()
        mgr = getattr(rt, "connection_manager", None)
        if mgr is not None:
            mgr.drop_user(user_id, rt.custom_agents)
    except Exception:
        logger.debug("connector unload after user delete failed", exc_info=True)
    try:
        clear_schema_cache_for_user(user_id)
    except Exception:
        logger.debug("schema cache clear after user delete failed", exc_info=True)


@users_router.delete("/{user_id}")
def delete_user(user_id: str):
    """Admin deletes a user, their chats, keys, and login sessions.

    The admin row cannot be deleted. Upload directories, that user's
    conversation-memory points, and their personal knowledge points go with the row.
    """
    actor = _require_user()
    if not actor.is_admin:
        raise HTTPException(status_code=403, detail="not allowed")
    try:
        session_ids = get_db().delete_user(user_id)
    except ValueError as exc:
        raise _user_error(exc) from exc
    _drop_deleted_user_runtime(user_id)
    clear_session_attachments(get_upload_base(), only_session_ids=set(session_ids))
    memory = get_conversation_memory()
    if memory is not None:
        memory.delete_for_user(user_id)
    try:
        delete_personal_points(user_id)
    except Exception:
        logger.debug("knowledge point delete after user delete failed", exc_info=True)
    return {"deleted": True, "id": user_id}
