import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

APP_DIR = Path(__file__).resolve().parent
INDEX_HTML = APP_DIR / "static" / "index.html"


class Settings(BaseModel):
    matrix_base_url: str = os.getenv("MATRIX_BASE_URL", "http://matrix-conduwuit:6167").rstrip("/")
    matrix_server_name: str = os.getenv("MATRIX_SERVER_NAME", "matrix.biglone.tech")
    matrix_admin_user: str = os.getenv("MATRIX_ADMIN_USER", "")
    matrix_admin_password: str = os.getenv("MATRIX_ADMIN_PASSWORD", "")
    matrix_admin_token: str = os.getenv("MATRIX_ADMIN_TOKEN", "")
    control_api_token: str = os.getenv("CONTROL_API_TOKEN", "")
    expose_bot_access_token: bool = os.getenv("EXPOSE_BOT_ACCESS_TOKEN", "false").lower() == "true"


settings = Settings()
app = FastAPI(title="Matrix Control API", version="0.1.0")
_token_cache: str | None = None


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
    password: str = Field(..., min_length=8, max_length=128)
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

    payload = {
        "type": "m.login.password",
        "user": settings.matrix_admin_user,
        "password": settings.matrix_admin_password,
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(f"{settings.matrix_base_url}/_matrix/client/v3/login", json=payload)

    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Matrix login failed: {_matrix_error_message(response)}")

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
    payload: dict[str, Any] = {
        "username": request.username,
        "password": request.password,
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
    return response


@app.post("/api/bots/invite", dependencies=[Depends(_require_control_token)])
async def invite_bot(request: BotInviteRequest) -> dict[str, Any]:
    encoded_room = quote(request.room_id, safe="")
    await _matrix_request(
        "POST",
        f"/_matrix/client/v3/rooms/{encoded_room}/invite",
        json_body={"user_id": request.bot_user_id},
    )
    return {"ok": True}
