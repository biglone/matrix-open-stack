import os
import secrets
import string
import hashlib
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

APP_DIR = Path(__file__).resolve().parent
INDEX_HTML = APP_DIR / "static" / "index.html"


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


class Settings(BaseModel):
    matrix_base_url: str = os.getenv("MATRIX_BASE_URL", "http://matrix:6167").rstrip("/")
    matrix_server_name: str = os.getenv("MATRIX_SERVER_NAME", "matrix.example.com")
    matrix_admin_user: str = os.getenv("MATRIX_ADMIN_USER", "")
    matrix_admin_password: str = os.getenv("MATRIX_ADMIN_PASSWORD", "")
    matrix_admin_token: str = os.getenv("MATRIX_ADMIN_TOKEN", "")
    control_api_token: str = os.getenv("CONTROL_API_TOKEN", "")
    expose_bot_access_token: bool = os.getenv("EXPOSE_BOT_ACCESS_TOKEN", "false").lower() == "true"
    bot_create_mode: str = os.getenv("BOT_CREATE_MODE", "disabled").strip().lower()
    audit_log_path: str = os.getenv("AUDIT_LOG_PATH", "/var/log/matrix-control/audit.log")
    invite_rate_limit_window_seconds: int = _env_int("INVITE_RATE_LIMIT_WINDOW_SECONDS", 60)
    invite_rate_limit_max: int = _env_int("INVITE_RATE_LIMIT_MAX", 12)


settings = Settings()
app = FastAPI(title="Matrix Control API", version="0.1.0")
_token_cache: str | None = None
_invite_rate_lock = threading.Lock()
_invite_rate_hits: dict[str, list[float]] = {}


class RoomCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    topic: str | None = Field(default=None, max_length=500)
    is_private: bool = True
    invitees: list[str] = Field(default_factory=list)
    alias_localpart: str | None = Field(default=None, max_length=80)
    space_room_id: str | None = None


class SpaceCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    topic: str | None = Field(default=None, max_length=500)
    invitees: list[str] = Field(default_factory=list)
    alias_localpart: str | None = Field(default=None, max_length=80)
    child_room_ids: list[str] = Field(default_factory=list)


class BotCreateRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str | None = Field(default=None, min_length=8, max_length=128)
    display_name: str | None = Field(default=None, max_length=100)
    inhibit_login: bool = False


class BotInviteRequest(BaseModel):
    bot_user_id: str = Field(..., min_length=3, max_length=255)
    room_id: str = Field(..., min_length=3, max_length=255)


def _matrix_error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except Exception:
        return response.text or response.reason_phrase
    if isinstance(body, dict):
        errcode = body.get("errcode")
        error = body.get("error")
        if errcode and error:
            return f"{errcode}: {error}"
        if error:
            return str(error)
    return str(body)


async def _get_admin_token() -> str:
    global _token_cache

    if settings.matrix_admin_token:
        return settings.matrix_admin_token
    if _token_cache:
        return _token_cache
    if not settings.matrix_admin_user or not settings.matrix_admin_password:
        raise HTTPException(
            status_code=500,
            detail="Matrix admin credentials missing. Set MATRIX_ADMIN_TOKEN or MATRIX_ADMIN_USER/MATRIX_ADMIN_PASSWORD.",
        )

    candidates = [settings.matrix_admin_user]
    if not settings.matrix_admin_user.startswith("@"):
        candidates.append(f"@{settings.matrix_admin_user}:{settings.matrix_server_name}")

    response: httpx.Response | None = None
    async with httpx.AsyncClient(timeout=15.0) as client:
        for candidate in candidates:
            payload = {
                "type": "m.login.password",
                "user": candidate,
                "password": settings.matrix_admin_password,
            }
            response = await client.post(f"{settings.matrix_base_url}/_matrix/client/v3/login", json=payload)
            if response.status_code < 400:
                break

    if response is None or response.status_code >= 400:
        detail = _matrix_error_message(response) if response is not None else "unknown login failure"
        raise HTTPException(status_code=502, detail=f"Matrix login failed: {detail}")

    body = response.json()
    access_token = body.get("access_token")
    if not access_token:
        raise HTTPException(status_code=502, detail="Matrix login succeeded but no access_token returned.")

    _token_cache = access_token
    return access_token


async def _matrix_request(method: str, path: str, json_body: dict[str, Any] | None = None, token: str | None = None) -> dict[str, Any]:
    if token is None:
        token = await _get_admin_token()

    headers = {"Authorization": f"Bearer {token}"}
    url = f"{settings.matrix_base_url}{path}"

    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.request(method, url, headers=headers, json=json_body)

    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=_matrix_error_message(response))

    if not response.content:
        return {}
    return response.json()


def _require_control_token(authorization: str | None = Header(default=None)) -> None:
    expected = settings.control_api_token.strip()
    if not expected:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token.")
    provided = authorization.removeprefix("Bearer ").strip()
    if provided != expected:
        raise HTTPException(status_code=403, detail="Invalid Bearer token.")


def _build_room_payload(
    name: str,
    topic: str | None,
    is_private: bool,
    invitees: list[str],
    alias_localpart: str | None,
    is_space: bool,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": name,
        "preset": "private_chat" if is_private else "public_chat",
        "visibility": "private" if is_private else "public",
    }
    if topic:
        payload["topic"] = topic
    if invitees:
        payload["invite"] = invitees
    if alias_localpart:
        payload["room_alias_name"] = alias_localpart
    if is_space:
        payload["creation_content"] = {"type": "m.space"}
    return payload


def _generate_password(length: int = 24) -> str:
    alphabet = string.ascii_letters + string.digits + "-_.!@#$%^&*()"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _audit_log(event: str, status: str, details: dict[str, Any]) -> None:
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "status": status,
        **details,
    }
    payload = json.dumps(record, ensure_ascii=True)

    path = settings.audit_log_path.strip()
    if not path:
        print(payload)
        return

    try:
        log_path = Path(path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(payload + "\n")
    except Exception:
        print(payload)


def _invite_principal_key(request: Request, authorization: str | None) -> tuple[str, str]:
    client_ip = request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "unknown")
    token = ""
    if authorization and authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ").strip()

    token_fingerprint = hashlib.sha256(token.encode("utf-8")).hexdigest()[:12] if token else "no-token"
    return f"{client_ip}:{token_fingerprint}", client_ip


def _check_invite_rate_limit(principal_key: str) -> tuple[bool, int]:
    now = time.time()
    window = settings.invite_rate_limit_window_seconds
    max_hits = settings.invite_rate_limit_max

    with _invite_rate_lock:
        hits = _invite_rate_hits.setdefault(principal_key, [])
        hits[:] = [ts for ts in hits if now - ts < window]
        if len(hits) >= max_hits:
            retry_after = max(1, int(window - (now - hits[0])))
            return False, retry_after
        hits.append(now)
    return True, 0


async def _link_space_child(space_room_id: str, child_room_id: str) -> None:
    encoded_space = quote(space_room_id, safe="")
    encoded_child = quote(child_room_id, safe="")
    payload = {"via": [settings.matrix_server_name]}
    await _matrix_request(
        "PUT",
        f"/_matrix/client/v3/rooms/{encoded_space}/state/m.space.child/{encoded_child}",
        json_body=payload,
    )


@app.get("/", response_class=FileResponse)
async def index() -> FileResponse:
    return FileResponse(INDEX_HTML)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/config", dependencies=[Depends(_require_control_token)])
async def api_config() -> dict[str, Any]:
    return {
        "matrix_base_url": settings.matrix_base_url,
        "matrix_server_name": settings.matrix_server_name,
        "token_protection_enabled": bool(settings.control_api_token.strip()),
        "bot_access_token_exposed": settings.expose_bot_access_token,
        "bot_create_mode": settings.bot_create_mode,
        "invite_rate_limit_window_seconds": settings.invite_rate_limit_window_seconds,
        "invite_rate_limit_max": settings.invite_rate_limit_max,
    }


@app.post("/api/rooms", dependencies=[Depends(_require_control_token)])
async def create_room(request: RoomCreateRequest) -> dict[str, Any]:
    payload = _build_room_payload(
        name=request.name,
        topic=request.topic,
        is_private=request.is_private,
        invitees=request.invitees,
        alias_localpart=request.alias_localpart,
        is_space=False,
    )
    created = await _matrix_request("POST", "/_matrix/client/v3/createRoom", json_body=payload)
    room_id = created.get("room_id")
    if not room_id:
        raise HTTPException(status_code=502, detail="Matrix did not return room_id.")

    if request.space_room_id:
        await _link_space_child(request.space_room_id, room_id)

    return {"room_id": room_id}


@app.post("/api/spaces", dependencies=[Depends(_require_control_token)])
async def create_space(request: SpaceCreateRequest) -> dict[str, Any]:
    payload = _build_room_payload(
        name=request.name,
        topic=request.topic,
        is_private=True,
        invitees=request.invitees,
        alias_localpart=request.alias_localpart,
        is_space=True,
    )
    created = await _matrix_request("POST", "/_matrix/client/v3/createRoom", json_body=payload)
    space_room_id = created.get("room_id")
    if not space_room_id:
        raise HTTPException(status_code=502, detail="Matrix did not return room_id.")

    linked_children: list[str] = []
    for child_room_id in request.child_room_ids:
        await _link_space_child(space_room_id, child_room_id)
        linked_children.append(child_room_id)

    return {"space_room_id": space_room_id, "linked_children": linked_children}


@app.post("/api/bots", dependencies=[Depends(_require_control_token)])
async def create_bot(request: BotCreateRequest) -> dict[str, Any]:
    # Keep bot account creation disabled by default to avoid depending on open registration.
    if settings.bot_create_mode != "legacy_register":
        _audit_log(
            "bot_create_api",
            "blocked",
            {
                "username": request.username,
                "reason": "bot_create_mode_disabled",
            },
        )
        raise HTTPException(
            status_code=403,
            detail="Bot creation via API is disabled for security. Use scripts/create_bot_secure.sh on the host.",
        )

    password = request.password or _generate_password()
    payload: dict[str, Any] = {
        "username": request.username,
        "password": password,
        "inhibit_login": request.inhibit_login,
    }
    created = await _matrix_request("POST", "/_matrix/client/v3/register", json_body=payload, token=await _get_admin_token())

    user_id = created.get("user_id")
    access_token = created.get("access_token", "")

    if request.display_name and user_id and access_token:
        encoded_user = quote(user_id, safe="")
        await _matrix_request(
            "PUT",
            f"/_matrix/client/v3/profile/{encoded_user}/displayname",
            json_body={"displayname": request.display_name},
            token=access_token,
        )

    response: dict[str, Any] = {"user_id": user_id}
    if settings.expose_bot_access_token and access_token:
        response["access_token"] = access_token
    _audit_log(
        "bot_create_api",
        "ok",
        {
            "username": request.username,
            "user_id": user_id,
            "mode": "legacy_register",
        },
    )
    return response


@app.post("/api/bots/invite")
async def invite_bot(
    payload: BotInviteRequest,
    http_request: Request,
    authorization: str | None = Header(default=None),
    _auth: None = Depends(_require_control_token),
) -> dict[str, Any]:
    principal_key, client_ip = _invite_principal_key(http_request, authorization)
    allowed, retry_after = _check_invite_rate_limit(principal_key)
    if not allowed:
        _audit_log(
            "bot_invite",
            "rate_limited",
            {
                "bot_user_id": payload.bot_user_id,
                "room_id": payload.room_id,
                "client_ip": client_ip,
                "retry_after_seconds": retry_after,
            },
        )
        raise HTTPException(
            status_code=429,
            detail=f"Invite rate limit exceeded. Retry in {retry_after} seconds.",
            headers={"Retry-After": str(retry_after)},
        )

    encoded_room = quote(payload.room_id, safe="")
    try:
        await _matrix_request(
            "POST",
            f"/_matrix/client/v3/rooms/{encoded_room}/invite",
            json_body={"user_id": payload.bot_user_id},
        )
    except HTTPException as exc:
        _audit_log(
            "bot_invite",
            "failed",
            {
                "bot_user_id": payload.bot_user_id,
                "room_id": payload.room_id,
                "client_ip": client_ip,
                "http_status": exc.status_code,
                "error": str(exc.detail),
            },
        )
        raise

    _audit_log(
        "bot_invite",
        "ok",
        {
            "bot_user_id": payload.bot_user_id,
            "room_id": payload.room_id,
            "client_ip": client_ip,
        },
    )
    return {"ok": True}
