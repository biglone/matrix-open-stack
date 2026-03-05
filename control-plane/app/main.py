import asyncio
import os
import secrets
import string
import hashlib
import json
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request
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


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


class Settings(BaseModel):
    matrix_base_url: str = os.getenv("MATRIX_BASE_URL", "http://matrix:6167").rstrip("/")
    matrix_server_name: str = os.getenv("MATRIX_SERVER_NAME", "matrix.example.com")
    matrix_admin_user: str = os.getenv("MATRIX_ADMIN_USER", "")
    matrix_admin_password: str = os.getenv("MATRIX_ADMIN_PASSWORD", "")
    matrix_admin_token: str = os.getenv("MATRIX_ADMIN_TOKEN", "")
    control_api_token: str = os.getenv("CONTROL_API_TOKEN", "")
    expose_bot_access_token: bool = os.getenv("EXPOSE_BOT_ACCESS_TOKEN", "false").lower() == "true"
    expose_user_access_token: bool = os.getenv("EXPOSE_USER_ACCESS_TOKEN", "false").lower() == "true"
    bot_create_mode: str = os.getenv("BOT_CREATE_MODE", "disabled").strip().lower()
    user_create_mode: str = os.getenv("USER_CREATE_MODE", "disabled").strip().lower()
    audit_log_path: str = os.getenv("AUDIT_LOG_PATH", "/var/log/matrix-control/audit.log")
    full_users_snapshot_path: str = os.getenv("FULL_USERS_SNAPSHOT_PATH", "/var/log/matrix-control/full-users-snapshot.json")
    bot_state_path: str = os.getenv("BOT_STATE_PATH", "/var/log/matrix-control/bot-state.json")
    bot_credentials_path: str = os.getenv("BOT_CREDENTIALS_PATH", "/var/log/matrix-control/bot-credentials.json")
    user_state_path: str = os.getenv("USER_STATE_PATH", "/var/log/matrix-control/user-state.json")
    invite_rate_limit_window_seconds: int = _env_int("INVITE_RATE_LIMIT_WINDOW_SECONDS", 60)
    invite_rate_limit_max: int = _env_int("INVITE_RATE_LIMIT_MAX", 12)
    restart_api_mode: str = os.getenv("RESTART_API_MODE", "disabled").strip().lower()
    docker_socket_path: str = os.getenv("DOCKER_SOCKET_PATH", "/var/run/docker.sock")
    compose_project_name: str = os.getenv("COMPOSE_PROJECT_NAME", "matrix-open-stack")
    restart_timeout_seconds: int = _env_int("RESTART_TIMEOUT_SECONDS", 20)
    registration_window_api_mode: str = os.getenv("REGISTRATION_WINDOW_API_MODE", "disabled").strip().lower()
    registration_window_default_minutes: int = _env_int("REGISTRATION_WINDOW_DEFAULT_MINUTES", 10)
    registration_window_max_minutes: int = _env_int("REGISTRATION_WINDOW_MAX_MINUTES", 60)
    stack_host_path: str = os.getenv("STACK_HOST_PATH", "")
    host_helper_image: str = os.getenv("HOST_HELPER_IMAGE", "local/matrix-control-api:0.1.0")
    registration_window_state_path: str = os.getenv(
        "REGISTRATION_WINDOW_STATE_PATH",
        "/var/log/matrix-control/registration-window-state.json",
    )


settings = Settings()
app = FastAPI(title="Matrix Control API", version="0.1.0")
_token_cache: str | None = None
_invite_rate_lock = threading.Lock()
_invite_rate_hits: dict[str, list[float]] = {}
_bot_password_cache_lock = threading.Lock()
_bot_password_cache: dict[str, str] = {}
_bot_access_token_cache: dict[str, str] = {}
_registration_lock = threading.Lock()
_registration_timer: threading.Timer | None = None
_overview_cache_lock = threading.Lock()
_overview_cache: dict[str, Any] | None = None
_registration_state: dict[str, Any] = {
    "active": False,
    "opened_at": "",
    "expires_at": "",
    "scope_users": False,
    "scope_bots": False,
    "reason": "",
    "client_ip": "",
}


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


class RoomConfigUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    topic: str | None = Field(default=None, max_length=500)
    join_rule: str | None = Field(default=None, pattern="^(private|invite|public|knock|restricted|knock_restricted)$")


class RoomMemberUpdateRequest(BaseModel):
    user_id: str = Field(..., min_length=3, max_length=255)
    reason: str | None = Field(default=None, max_length=200)


class BotCreateRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str | None = Field(default=None, min_length=8, max_length=128)
    display_name: str | None = Field(default=None, max_length=100)
    inhibit_login: bool = False


class UserCreateRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str | None = Field(default=None, min_length=8, max_length=128)
    display_name: str | None = Field(default=None, max_length=100)
    inhibit_login: bool = False


class BotInviteRequest(BaseModel):
    bot_user_id: str = Field(..., min_length=3, max_length=255)
    room_id: str = Field(..., min_length=3, max_length=255)
    bot_password: str | None = Field(default=None, min_length=1, max_length=256)
    auto_join: bool = True


class UserInviteRequest(BaseModel):
    user_id: str = Field(..., min_length=3, max_length=255)
    room_id: str = Field(..., min_length=3, max_length=255)


class ResourceArchiveRequest(BaseModel):
    note: str | None = Field(default=None, max_length=200)


class BotStatusUpdateRequest(BaseModel):
    status: str = Field(..., pattern="^(active|archived|deleted)$")


class UserStatusUpdateRequest(BaseModel):
    status: str = Field(..., pattern="^(active|archived|deleted)$")


class OpsRestartRequest(BaseModel):
    target: str = Field(..., pattern="^(matrix|control_api|stack)$")
    reason: str | None = Field(default=None, max_length=200)


class OpsRegistrationWindowOpenRequest(BaseModel):
    minutes: int = Field(default=10, ge=1, le=240)
    scope_users: bool = True
    scope_bots: bool = True
    reason: str | None = Field(default=None, max_length=200)


class OpsRegistrationWindowCloseRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=200)


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
    last_error: str = ""
    async with httpx.AsyncClient(timeout=15.0) as client:
        for candidate in candidates:
            payload = {
                "type": "m.login.password",
                "user": candidate,
                "password": settings.matrix_admin_password,
            }
            try:
                response = await client.post(f"{settings.matrix_base_url}/_matrix/client/v3/login", json=payload)
            except httpx.HTTPError as exc:
                last_error = str(exc)
                response = None
                continue
            if response.status_code < 400:
                break

    if response is None:
        raise HTTPException(status_code=502, detail=f"Matrix login request failed: {last_error or 'network error'}")

    if response.status_code >= 400:
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
        try:
            response = await client.request(method, url, headers=headers, json=json_body)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"Matrix API request failed: {exc}") from exc

    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=_matrix_error_message(response))

    if not response.content:
        return {}
    return response.json()


async def _matrix_register_request(payload: dict[str, Any]) -> dict[str, Any]:
    url = f"{settings.matrix_base_url}/_matrix/client/v3/register"
    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            response = await client.post(url, json=payload)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"Matrix register request failed: {exc}") from exc
        if response.status_code == 401:
            try:
                body = response.json()
            except Exception:
                body = {}
            if isinstance(body, dict):
                flows = body.get("flows", [])
                session = body.get("session")
                supports_dummy = False
                if isinstance(flows, list):
                    for flow in flows:
                        if not isinstance(flow, dict):
                            continue
                        stages = flow.get("stages", [])
                        if isinstance(stages, list) and "m.login.dummy" in stages:
                            supports_dummy = True
                            break
                if supports_dummy and isinstance(session, str) and session:
                    second_payload = dict(payload)
                    second_payload["auth"] = {"type": "m.login.dummy", "session": session}
                    try:
                        response = await client.post(url, json=second_payload)
                    except httpx.HTTPError as exc:
                        raise HTTPException(status_code=502, detail=f"Matrix register request failed: {exc}") from exc

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


def _normalize_invitees(invitees: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in invitees:
        user_id = _normalize_local_user_id(raw)
        if not user_id or user_id in seen:
            continue
        seen.add(user_id)
        normalized.append(user_id)
    return normalized


def _cache_bot_password(user_id: str, password: str) -> None:
    key = _normalize_local_user_id(user_id)
    secret = password.strip()
    if not key or not secret:
        return
    with _bot_password_cache_lock:
        _bot_password_cache[key] = secret
        _save_bot_credentials_cache_locked()


def _get_cached_bot_password(user_id: str) -> str:
    key = _normalize_local_user_id(user_id)
    if not key:
        return ""
    with _bot_password_cache_lock:
        return _bot_password_cache.get(key, "")


def _cache_bot_access_token(user_id: str, access_token: str) -> None:
    key = _normalize_local_user_id(user_id)
    token = access_token.strip()
    if not key or not token:
        return
    with _bot_password_cache_lock:
        _bot_access_token_cache[key] = token
        _save_bot_credentials_cache_locked()


def _drop_cached_bot_access_token(user_id: str) -> None:
    key = _normalize_local_user_id(user_id)
    if not key:
        return
    with _bot_password_cache_lock:
        if key in _bot_access_token_cache:
            _bot_access_token_cache.pop(key, None)
            _save_bot_credentials_cache_locked()


def _get_cached_bot_access_token(user_id: str) -> str:
    key = _normalize_local_user_id(user_id)
    if not key:
        return ""
    with _bot_password_cache_lock:
        return _bot_access_token_cache.get(key, "")


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


def _restart_api_enabled() -> bool:
    return settings.restart_api_mode == "docker_socket"


def _registration_window_api_enabled() -> bool:
    return settings.registration_window_api_mode == "docker_socket"


def _restart_target_containers(target: str) -> list[str]:
    matrix_container = f"{settings.compose_project_name}-matrix-1"
    control_container = f"{settings.compose_project_name}-matrix-control-api-1"
    if target == "matrix":
        return [matrix_container]
    if target == "control_api":
        return [control_container]
    return [matrix_container, control_container]


def _docker_api_request(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
) -> httpx.Response:
    transport = httpx.HTTPTransport(uds=settings.docker_socket_path)
    with httpx.Client(base_url="http://docker", transport=transport, timeout=10.0) as client:
        return client.request(method, path, params=params, json=json_body)


def _docker_error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except Exception:
        payload = response.text
    if isinstance(payload, dict):
        if isinstance(payload.get("message"), str):
            return payload["message"]
        return json.dumps(payload, ensure_ascii=True)
    return str(payload)


def _ensure_docker_runtime_ready() -> None:
    socket_path = Path(settings.docker_socket_path)
    if not socket_path.exists():
        raise HTTPException(
            status_code=503,
            detail=f"Docker socket not found: {settings.docker_socket_path}",
        )

    try:
        response = _docker_api_request("GET", "/_ping")
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "Docker API is unreachable. Ensure /var/run/docker.sock is mounted and DOCKER_GID "
                f"matches host docker group. ({exc})"
            ),
        ) from exc

    if response.status_code >= 400:
        raise HTTPException(
            status_code=503,
            detail=f"Docker API ping failed: HTTP {response.status_code} {_docker_error_message(response)}",
        )


def _ensure_restart_runtime_ready() -> None:
    if not _restart_api_enabled():
        raise HTTPException(
            status_code=403,
            detail="Restart API is disabled. Set RESTART_API_MODE=docker_socket to enable.",
        )
    _ensure_docker_runtime_ready()


def _ensure_registration_window_runtime_ready() -> None:
    if not _registration_window_api_enabled():
        raise HTTPException(
            status_code=403,
            detail="Registration window API is disabled. Set REGISTRATION_WINDOW_API_MODE=docker_socket to enable.",
        )
    if not settings.stack_host_path.strip():
        raise HTTPException(
            status_code=500,
            detail="STACK_HOST_PATH is required for registration window operations.",
        )
    if not settings.host_helper_image.strip():
        raise HTTPException(
            status_code=500,
            detail="HOST_HELPER_IMAGE is required for registration window operations.",
        )
    _ensure_docker_runtime_ready()


def _registration_state_path() -> Path:
    return Path(settings.registration_window_state_path)


def _default_registration_state() -> dict[str, Any]:
    return {
        "active": False,
        "opened_at": "",
        "expires_at": "",
        "scope_users": False,
        "scope_bots": False,
        "reason": "",
        "client_ip": "",
    }


def _save_registration_state_locked() -> None:
    state_path = _registration_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(_registration_state, ensure_ascii=True, indent=2), encoding="utf-8")


def _load_registration_state() -> None:
    state_path = _registration_state_path()
    if not state_path.exists():
        return
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(payload, dict):
        return
    merged = _default_registration_state()
    merged.update(payload)
    with _registration_lock:
        _registration_state.update(merged)


def _parse_iso_utc(value: str) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def _registration_snapshot_locked(now_utc: datetime | None = None) -> dict[str, Any]:
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    expires_at = _parse_iso_utc(str(_registration_state.get("expires_at", "")))
    remaining_seconds = 0
    active = bool(_registration_state.get("active", False))
    if active and expires_at is not None:
        remaining_seconds = max(0, int((expires_at - now_utc).total_seconds()))
        if remaining_seconds <= 0:
            active = False
    return {
        "active": active,
        "opened_at": str(_registration_state.get("opened_at", "")),
        "expires_at": str(_registration_state.get("expires_at", "")),
        "scope_users": bool(_registration_state.get("scope_users", False)),
        "scope_bots": bool(_registration_state.get("scope_bots", False)),
        "reason": str(_registration_state.get("reason", "")),
        "client_ip": str(_registration_state.get("client_ip", "")),
        "remaining_seconds": remaining_seconds,
    }


def _registration_snapshot() -> dict[str, Any]:
    with _registration_lock:
        return _registration_snapshot_locked()


def _registration_window_allows(scope: str) -> bool:
    snapshot = _registration_snapshot()
    if not snapshot.get("active"):
        return False
    if scope == "users":
        return bool(snapshot.get("scope_users"))
    if scope == "bots":
        return bool(snapshot.get("scope_bots"))
    return False


def _cancel_registration_timer_locked() -> None:
    global _registration_timer
    if _registration_timer is not None:
        _registration_timer.cancel()
        _registration_timer = None


def _run_host_helper(command: str) -> None:
    host_stack_path = settings.stack_host_path.strip()
    helper_image = settings.host_helper_image.strip()
    bind_entry = f"{host_stack_path}:/stack"
    create_payload = {
        "Image": helper_image,
        "Cmd": ["/bin/sh", "-lc", command],
        "WorkingDir": "/stack",
        "HostConfig": {
            "AutoRemove": True,
            "Binds": [bind_entry],
        },
    }
    create_response = _docker_api_request("POST", "/containers/create", json_body=create_payload)
    if create_response.status_code >= 400:
        raise RuntimeError(
            f"Failed to create helper container: HTTP {create_response.status_code} {_docker_error_message(create_response)}"
        )
    container_id = str(create_response.json().get("Id", "")).strip()
    if not container_id:
        raise RuntimeError("Helper container creation returned empty container ID.")

    encoded_id = quote(container_id, safe="")
    start_response = _docker_api_request("POST", f"/containers/{encoded_id}/start")
    if start_response.status_code >= 400:
        raise RuntimeError(
            f"Failed to start helper container {container_id}: "
            f"HTTP {start_response.status_code} {_docker_error_message(start_response)}"
        )

    wait_response = _docker_api_request("POST", f"/containers/{encoded_id}/wait")
    if wait_response.status_code >= 400:
        raise RuntimeError(
            f"Failed waiting helper container {container_id}: "
            f"HTTP {wait_response.status_code} {_docker_error_message(wait_response)}"
        )
    wait_payload = wait_response.json() if wait_response.content else {}
    status_code = int(wait_payload.get("StatusCode", 1))
    if status_code != 0:
        logs_response = _docker_api_request(
            "GET",
            f"/containers/{encoded_id}/logs",
            params={"stdout": "1", "stderr": "1", "tail": "100"},
        )
        logs_text = ""
        if logs_response.status_code < 400:
            logs_text = logs_response.text.strip()
        raise RuntimeError(f"Helper container failed with exit code {status_code}. logs={logs_text}")


def _set_matrix_registration_flags(open_enabled: bool) -> None:
    bool_text = "true" if open_enabled else "false"
    command = (
        "set -eu; "
        "conf='/stack/conf/conduwuit.toml'; "
        "if [ ! -f \"$conf\" ]; then echo \"missing $conf\" >&2; exit 1; fi; "
        "sed -i -E "
        f"'s|^([[:space:]]*allow_registration[[:space:]]*=[[:space:]]*).*$|\\1{bool_text}|' \"$conf\"; "
        "sed -i -E "
        f"'s|^([[:space:]]*yes_i_am_very_very_sure_i_want_an_open_registration_server_prone_to_abuse[[:space:]]*=[[:space:]]*).*$|\\1{bool_text}|' \"$conf\""
    )
    _run_host_helper(command)


def _registration_target_container() -> str:
    return _restart_target_containers("matrix")[0]


def _schedule_registration_auto_close_locked(expires_at: datetime) -> None:
    global _registration_timer
    _cancel_registration_timer_locked()
    delay = max(1, int((expires_at - datetime.now(timezone.utc)).total_seconds()))
    timer = threading.Timer(delay, _registration_auto_close_callback)
    timer.daemon = True
    _registration_timer = timer
    timer.start()


def _close_registration_window_internal(reason: str, client_ip: str, *, force: bool) -> dict[str, Any]:
    with _registration_lock:
        snapshot = _registration_snapshot_locked()
        if not force and not snapshot.get("active"):
            return snapshot
        _cancel_registration_timer_locked()

    _set_matrix_registration_flags(False)
    _restart_containers_via_docker([_registration_target_container()], settings.restart_timeout_seconds)

    with _registration_lock:
        _registration_state.clear()
        _registration_state.update(_default_registration_state())
        _registration_state["reason"] = reason
        _registration_state["client_ip"] = client_ip
        _save_registration_state_locked()
        return _registration_snapshot_locked()


def _registration_auto_close_callback() -> None:
    try:
        result = _close_registration_window_internal("auto_expired", "system", force=True)
        _audit_log("registration_window", "auto_closed", result)
    except Exception as exc:
        _audit_log("registration_window", "auto_close_failed", {"error": str(exc)})


def _open_registration_window_internal(
    *,
    minutes: int,
    scope_users: bool,
    scope_bots: bool,
    reason: str,
    client_ip: str,
) -> dict[str, Any]:
    capped_minutes = min(minutes, settings.registration_window_max_minutes)
    now_utc = datetime.now(timezone.utc)
    expires_at = now_utc + timedelta(minutes=capped_minutes)

    _set_matrix_registration_flags(True)
    _restart_containers_via_docker([_registration_target_container()], settings.restart_timeout_seconds)

    with _registration_lock:
        _registration_state["active"] = True
        _registration_state["opened_at"] = now_utc.isoformat()
        _registration_state["expires_at"] = expires_at.isoformat()
        _registration_state["scope_users"] = scope_users
        _registration_state["scope_bots"] = scope_bots
        _registration_state["reason"] = reason
        _registration_state["client_ip"] = client_ip
        _save_registration_state_locked()
        _schedule_registration_auto_close_locked(expires_at)
        return _registration_snapshot_locked()

def _restart_containers_via_docker(container_names: list[str], timeout_seconds: int) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for name in container_names:
        encoded = quote(name, safe="")
        inspect_response = _docker_api_request("GET", f"/containers/{encoded}/json")
        if inspect_response.status_code == 404:
            raise RuntimeError(f"Container not found: {name}")
        if inspect_response.status_code >= 400:
            raise RuntimeError(
                f"Failed to inspect container {name}: "
                f"HTTP {inspect_response.status_code} {_docker_error_message(inspect_response)}"
            )

        restart_response = _docker_api_request(
            "POST",
            f"/containers/{encoded}/restart",
            params={"t": str(timeout_seconds)},
        )
        if restart_response.status_code >= 400:
            raise RuntimeError(
                f"Failed to restart container {name}: "
                f"HTTP {restart_response.status_code} {_docker_error_message(restart_response)}"
            )

        results.append({"container": name, "status": "restarted"})
    return results


def _restart_containers_task(target: str, container_names: list[str], reason: str, client_ip: str) -> None:
    details = {
        "target": target,
        "containers": container_names,
        "reason": reason,
        "client_ip": client_ip,
    }
    try:
        result = _restart_containers_via_docker(container_names, settings.restart_timeout_seconds)
        _audit_log("ops_restart", "ok", {**details, "result": result})
    except Exception as exc:
        _audit_log("ops_restart", "failed", {**details, "error": str(exc)})


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


async def _invite_user_to_room(user_id: str, room_id: str) -> None:
    encoded_room = quote(room_id, safe="")
    await _matrix_request(
        "POST",
        f"/_matrix/client/v3/rooms/{encoded_room}/invite",
        json_body={"user_id": user_id},
    )


async def _login_with_password(user_id: str, password: str) -> str:
    payload = {
        "type": "m.login.password",
        "identifier": {"type": "m.id.user", "user": user_id},
        "password": password,
    }
    url = f"{settings.matrix_base_url}/_matrix/client/v3/login"
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(url, json=payload)
    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"Bot login failed while auto-joining: {_matrix_error_message(response)}",
        )
    body = response.json() if response.content else {}
    token = str(body.get("access_token", "")).strip() if isinstance(body, dict) else ""
    if not token:
        raise HTTPException(status_code=502, detail="Bot login succeeded but no access token was returned.")
    return token


async def _join_room_with_token(room_id: str, token: str) -> None:
    encoded_room = quote(room_id, safe="")
    await _matrix_request(
        "POST",
        f"/_matrix/client/v3/rooms/{encoded_room}/join",
        json_body={},
        token=token,
    )


async def _auto_join_bot_if_possible(bot_user_id: str, room_id: str) -> str:
    if not _is_probable_bot_user(bot_user_id):
        return "skipped_not_bot"
    access_token = _get_cached_bot_access_token(bot_user_id)
    if access_token:
        try:
            await _join_room_with_token(room_id, access_token)
            return "joined_by_cached_token"
        except HTTPException as exc:
            if exc.status_code in {401, 403}:
                _drop_cached_bot_access_token(bot_user_id)
            else:
                raise
    password = _get_cached_bot_password(bot_user_id)
    if not password:
        return "skipped_no_cached_credential"
    token = await _login_with_password(bot_user_id, password)
    _cache_bot_access_token(bot_user_id, token)
    await _join_room_with_token(room_id, token)
    return "joined_by_cached_password"


async def _get_room_membership(user_id: str, room_id: str) -> str:
    encoded_room = quote(room_id, safe="")
    encoded_user = quote(user_id, safe="")
    try:
        payload = await _matrix_request(
            "GET",
            f"/_matrix/client/v3/rooms/{encoded_room}/state/m.room.member/{encoded_user}",
        )
    except HTTPException as exc:
        if exc.status_code == 404:
            return "none"
        return "unknown"
    if not isinstance(payload, dict):
        return "unknown"
    membership = str(payload.get("membership", "")).strip().lower()
    if membership in {"join", "invite", "leave", "ban", "knock"}:
        return membership
    return "unknown"


def _extract_localpart(user_id: str) -> str:
    if not user_id.startswith("@"):
        return ""
    return user_id[1:].split(":", 1)[0]


def _is_local_user(user_id: str) -> bool:
    return user_id.endswith(f":{settings.matrix_server_name}")


def _normalize_local_user_id(user_id: str) -> str:
    raw = user_id.strip()
    if not raw:
        return ""
    if raw.startswith("@") and ":" in raw:
        return raw
    if raw.startswith("@"):
        raw = raw[1:]
    if ":" in raw:
        return f"@{raw}"
    return f"@{raw}:{settings.matrix_server_name}"


def _is_probable_bot_user(user_id: str) -> bool:
    localpart = _extract_localpart(user_id).lower()
    return "bot" in localpart


def _parse_room_snapshot(room_id: str, events: list[dict[str, Any]]) -> dict[str, Any]:
    info: dict[str, Any] = {
        "room_id": room_id,
        "name": "",
        "topic": "",
        "canonical_alias": "",
        "join_rule": "private",
        "member_count": 0,
        "child_count": 0,
        "is_space": False,
    }

    for event in events:
        event_type = event.get("type")
        state_key = event.get("state_key")
        content = event.get("content")
        if not isinstance(content, dict):
            continue

        if event_type == "m.room.create" and state_key == "":
            info["is_space"] = content.get("type") == "m.space"
        elif event_type == "m.room.name" and state_key == "":
            info["name"] = content.get("name", "")
        elif event_type == "m.room.topic" and state_key == "":
            info["topic"] = content.get("topic", "")
        elif event_type == "m.room.canonical_alias" and state_key == "":
            info["canonical_alias"] = content.get("alias", "")
        elif event_type == "m.room.join_rules" and state_key == "":
            info["join_rule"] = content.get("join_rule", "private")
        elif event_type == "m.room.member" and content.get("membership") == "join":
            info["member_count"] += 1
        elif event_type == "m.space.child":
            info["child_count"] += 1

    if not info["name"]:
        info["name"] = info["canonical_alias"] or room_id
    info["kind"] = "space" if info["is_space"] else "room"
    return info


async def _fetch_room_snapshot(room_id: str) -> dict[str, Any]:
    encoded_room = quote(room_id, safe="")
    try:
        state = await _matrix_request("GET", f"/_matrix/client/v3/rooms/{encoded_room}/state")
    except HTTPException as exc:
        return {"room_id": room_id, "kind": "unknown", "error": str(exc.detail)}
    if not isinstance(state, list):
        return {"room_id": room_id, "kind": "unknown", "error": "unexpected room state payload"}
    return _parse_room_snapshot(room_id, state)


async def _list_joined_room_snapshots() -> list[dict[str, Any]]:
    data = await _matrix_request("GET", "/_matrix/client/v3/joined_rooms")
    room_ids = data.get("joined_rooms", []) if isinstance(data, dict) else []
    if not isinstance(room_ids, list):
        return []

    snapshots = await asyncio.gather(*[_fetch_room_snapshot(room_id) for room_id in room_ids])
    snapshots.sort(key=lambda item: ((item.get("name") or "").lower(), item.get("room_id", "")))
    return snapshots


def _with_archive_prefix(topic: str, note: str | None = None) -> str:
    marker = f"[ARCHIVED {date.today().isoformat()}]"
    if marker in topic:
        return topic
    extra = f" ({note.strip()[:120]})" if note and note.strip() else ""
    clean_topic = topic.strip()
    if clean_topic:
        return f"{marker}{extra} {clean_topic}".strip()
    return f"{marker}{extra}".strip()


async def _archive_room(room_id: str, note: str | None = None) -> dict[str, Any]:
    snapshot = await _fetch_room_snapshot(room_id)
    if snapshot.get("error"):
        raise HTTPException(status_code=404, detail=f"Room not accessible: {snapshot.get('error')}")

    current_topic = str(snapshot.get("topic", "") or "")
    updated_topic = _with_archive_prefix(current_topic, note)
    encoded_room = quote(room_id, safe="")
    await _matrix_request(
        "PUT",
        f"/_matrix/client/v3/rooms/{encoded_room}/state/m.room.topic",
        json_body={"topic": updated_topic},
    )
    return {
        "room_id": room_id,
        "topic": updated_topic,
        "kind": snapshot.get("kind", "room"),
    }


def _normalize_join_rule_for_response(join_rule: str) -> str:
    normalized = str(join_rule or "").strip().lower()
    if not normalized or normalized == "private":
        return "invite"
    return normalized


def _room_config_from_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "room_id": snapshot.get("room_id", ""),
        "kind": snapshot.get("kind", "unknown"),
        "name": str(snapshot.get("name", "") or ""),
        "topic": str(snapshot.get("topic", "") or ""),
        "join_rule": _normalize_join_rule_for_response(str(snapshot.get("join_rule", "") or "invite")),
    }


async def _load_room_snapshot_with_kind_check(room_id: str, expected_kind: str) -> dict[str, Any]:
    snapshot = await _fetch_room_snapshot(room_id)
    if snapshot.get("error"):
        raise HTTPException(status_code=404, detail=f"Room not accessible: {snapshot.get('error')}")
    kind = str(snapshot.get("kind", "unknown"))
    if kind != expected_kind:
        raise HTTPException(status_code=400, detail=f"Expected {expected_kind}, got {kind}.")
    return snapshot


async def _put_room_state_event(room_id: str, event_type: str, content: dict[str, Any]) -> None:
    encoded_room = quote(room_id, safe="")
    encoded_event_type = quote(event_type, safe="")
    await _matrix_request(
        "PUT",
        f"/_matrix/client/v3/rooms/{encoded_room}/state/{encoded_event_type}",
        json_body=content,
    )


async def _update_room_config(room_id: str, request: RoomConfigUpdateRequest) -> dict[str, Any]:
    fields_set = request.model_fields_set
    if not fields_set:
        raise HTTPException(status_code=400, detail="At least one field is required: name/topic/join_rule.")

    if "name" in fields_set:
        raw_name = str(request.name or "").strip()
        if not raw_name:
            raise HTTPException(status_code=400, detail="name cannot be empty when provided.")
        await _put_room_state_event(room_id, "m.room.name", {"name": raw_name})

    if "topic" in fields_set:
        raw_topic = str(request.topic or "").strip()
        await _put_room_state_event(room_id, "m.room.topic", {"topic": raw_topic})

    if "join_rule" in fields_set:
        join_rule = _normalize_join_rule_for_response(str(request.join_rule or "").strip().lower())
        await _put_room_state_event(room_id, "m.room.join_rules", {"join_rule": join_rule})

    updated = await _fetch_room_snapshot(room_id)
    if updated.get("error"):
        raise HTTPException(status_code=502, detail=f"Config updated but failed to read latest room state: {updated.get('error')}")
    return _room_config_from_snapshot(updated)


def _member_from_joined_payload(user_id: str, profile: dict[str, Any]) -> dict[str, Any]:
    display_name = ""
    avatar_url = ""
    if isinstance(profile, dict):
        display_name = str(profile.get("display_name", "") or "")
        avatar_url = str(profile.get("avatar_url", "") or "")
    return {
        "user_id": user_id,
        "display_name": display_name,
        "avatar_url": avatar_url,
        "is_bot": _is_probable_bot_user(user_id),
        "membership": "join",
    }


async def _list_room_members(room_id: str) -> list[dict[str, Any]]:
    encoded_room = quote(room_id, safe="")
    payload = await _matrix_request("GET", f"/_matrix/client/v3/rooms/{encoded_room}/joined_members")
    joined = payload.get("joined", {}) if isinstance(payload, dict) else {}
    if not isinstance(joined, dict):
        return []

    members: list[dict[str, Any]] = []
    for user_id, profile in joined.items():
        if not isinstance(user_id, str):
            continue
        normalized_profile = profile if isinstance(profile, dict) else {}
        members.append(_member_from_joined_payload(user_id, normalized_profile))
    members.sort(key=lambda item: ((item.get("display_name") or item.get("user_id") or "").lower(), item.get("user_id", "")))
    return members


async def _kick_user_from_room(room_id: str, user_id: str, reason: str | None = None) -> None:
    encoded_room = quote(room_id, safe="")
    payload: dict[str, Any] = {"user_id": user_id}
    clean_reason = str(reason or "").strip()
    if clean_reason:
        payload["reason"] = clean_reason
    await _matrix_request(
        "POST",
        f"/_matrix/client/v3/rooms/{encoded_room}/kick",
        json_body=payload,
    )


async def _leave_forget_room(room_id: str) -> dict[str, Any]:
    encoded_room = quote(room_id, safe="")
    await _matrix_request("POST", f"/_matrix/client/v3/rooms/{encoded_room}/leave", json_body={})
    try:
        await _matrix_request("POST", f"/_matrix/client/v3/rooms/{encoded_room}/forget", json_body={})
    except HTTPException:
        # Forget can fail for remote edge cases; leaving is sufficient for dashboard removal.
        pass
    return {"room_id": room_id, "removed_from_admin_view": True}


def _load_json_lines(path: Path, max_lines: int = 6000) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []

    records: list[dict[str, Any]] = []
    for line in lines[-max_lines:]:
        payload = line.strip()
        if not payload:
            continue
        try:
            record = json.loads(payload)
        except Exception:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _match_search(item: dict[str, Any], search: str, keys: list[str]) -> bool:
    if not search:
        return True
    needle = search.lower()
    for key in keys:
        value = item.get(key, "")
        if isinstance(value, list):
            haystack = " ".join(str(x) for x in value)
        else:
            haystack = str(value)
        if needle in haystack.lower():
            return True
    return False


def _paginate(items: list[dict[str, Any]], page: int, page_size: int) -> dict[str, Any]:
    total = len(items)
    if total == 0:
        return {
            "items": [],
            "page": page,
            "page_size": page_size,
            "total": 0,
            "total_pages": 0,
        }

    total_pages = (total + page_size - 1) // page_size
    safe_page = min(max(1, page), total_pages)
    start = (safe_page - 1) * page_size
    end = start + page_size
    return {
        "items": items[start:end],
        "page": safe_page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
    }


def _build_paginated_response(
    *,
    label: str,
    source_scope: str,
    items: list[dict[str, Any]],
    page: int,
    page_size: int,
) -> dict[str, Any]:
    paged = _paginate(items, page, page_size)
    return {
        "count": paged["total"],
        label: paged["items"],
        "page": paged["page"],
        "page_size": paged["page_size"],
        "total_pages": paged["total_pages"],
        "scope": source_scope,
    }


def _load_state_file(path_str: str, key: str) -> dict[str, str]:
    path = Path(path_str)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    raw = payload.get(key, {})
    if not isinstance(raw, dict):
        return {}

    out: dict[str, str] = {}
    for user_id, status in raw.items():
        if not isinstance(user_id, str) or not isinstance(status, str):
            continue
        if status not in {"active", "archived", "deleted"}:
            continue
        out[user_id] = status
    return out


def _save_state_file(path_str: str, key: str, state: dict[str, str]) -> None:
    path = Path(path_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        key: state,
    }
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def _load_bot_state() -> dict[str, str]:
    return _load_state_file(settings.bot_state_path, "bots")


def _save_bot_state(state: dict[str, str]) -> None:
    _save_state_file(settings.bot_state_path, "bots", state)


def _load_user_state() -> dict[str, str]:
    return _load_state_file(settings.user_state_path, "users")


def _save_user_state(state: dict[str, str]) -> None:
    _save_state_file(settings.user_state_path, "users", state)


def _load_bot_credentials_cache() -> None:
    path = Path(settings.bot_credentials_path)
    if not path.exists():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(payload, dict):
        return
    raw = payload.get("bots", {})
    if not isinstance(raw, dict):
        return

    passwords: dict[str, str] = {}
    tokens: dict[str, str] = {}
    for user_id, entry in raw.items():
        if not isinstance(user_id, str) or not isinstance(entry, dict):
            continue
        normalized = _normalize_local_user_id(user_id)
        if not normalized:
            continue
        password = str(entry.get("password", "")).strip()
        access_token = str(entry.get("access_token", "")).strip()
        if password:
            passwords[normalized] = password
        if access_token:
            tokens[normalized] = access_token

    with _bot_password_cache_lock:
        _bot_password_cache.clear()
        _bot_password_cache.update(passwords)
        _bot_access_token_cache.clear()
        _bot_access_token_cache.update(tokens)


def _save_bot_credentials_cache_locked() -> None:
    path = Path(settings.bot_credentials_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    user_ids = set(_bot_password_cache.keys()) | set(_bot_access_token_cache.keys())
    bots: dict[str, dict[str, str]] = {}
    for user_id in sorted(user_ids):
        entry: dict[str, str] = {}
        password = _bot_password_cache.get(user_id, "")
        access_token = _bot_access_token_cache.get(user_id, "")
        if password:
            entry["password"] = password
        if access_token:
            entry["access_token"] = access_token
        if entry:
            bots[user_id] = entry
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "bots": bots,
    }
    try:
        path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
    except Exception:
        pass


def _load_full_users_snapshot() -> dict[str, Any] | None:
    path = Path(settings.full_users_snapshot_path)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    users = payload.get("users", [])
    if not isinstance(users, list):
        payload["users"] = []
    return payload


def _list_managed_users(include_bots: bool = False, include_deleted: bool = False) -> list[dict[str, Any]]:
    snapshot = _load_full_users_snapshot()
    if snapshot is None:
        return []

    users = snapshot.get("users", [])
    if not isinstance(users, list):
        return []

    user_state = _load_user_state()
    inventory: list[dict[str, Any]] = []

    for item in users:
        if not isinstance(item, dict):
            continue
        user_id = _normalize_local_user_id(str(item.get("user_id", "") or ""))
        if not user_id or not _is_local_user(user_id):
            continue
        is_bot = bool(item.get("is_bot", False))
        if is_bot and not include_bots:
            continue
        status = user_state.get(user_id, "active")
        if not include_deleted and status == "deleted":
            continue
        inventory.append(
            {
                "user_id": user_id,
                "username": str(item.get("username", "") or _extract_localpart(user_id)),
                "is_bot": is_bot,
                "status": status,
            }
        )

    inventory.sort(key=lambda entry: ((entry.get("username") or "").lower(), entry.get("user_id", "")))
    return inventory


def _bots_from_audit_logs(bot_state: dict[str, str]) -> dict[str, dict[str, Any]]:
    bots: dict[str, dict[str, Any]] = {}

    def upsert(
        user_id: str,
        source: str,
        *,
        username: str | None = None,
        display_name: str | None = None,
        ts: str | None = None,
    ) -> None:
        if not user_id.startswith("@"):
            return
        entry = bots.setdefault(
            user_id,
            {
                "user_id": user_id,
                "username": _extract_localpart(user_id),
                "display_name": "",
                "sources": set(),
                "last_seen_ts": ts or "",
                "status": bot_state.get(user_id, "active"),
            },
        )
        if username:
            entry["username"] = username
        if display_name:
            entry["display_name"] = display_name
        if ts and ts > entry.get("last_seen_ts", ""):
            entry["last_seen_ts"] = ts
        entry["sources"].add(source)

    audit_path = Path(settings.audit_log_path)
    security_path = audit_path.with_name("security-audit.log")

    for record in _load_json_lines(audit_path):
        event = record.get("event")
        status = record.get("status")
        ts = str(record.get("ts", ""))
        if event == "bot_create_api" and status == "ok":
            upsert(
                str(record.get("user_id", "")),
                "api_create",
                username=str(record.get("username", "") or ""),
                ts=ts,
            )
        if event == "bot_invite":
            upsert(str(record.get("bot_user_id", "")), "invite_activity", ts=ts)

    for record in _load_json_lines(security_path):
        if record.get("event") != "create_bot_secure" or record.get("status") not in {"ok", "warn"}:
            continue
        secure_user_id = str(record.get("user_id", ""))
        secure_username = str(record.get("username", "") or "")
        is_probable_bot = _is_probable_bot_user(secure_user_id) or "bot" in secure_username.lower()
        if not is_probable_bot:
            continue
        upsert(
            secure_user_id,
            "secure_script",
            username=secure_username,
            display_name=str(record.get("display_name", "") or ""),
            ts=str(record.get("ts", "")),
        )

    for entry in bots.values():
        entry["sources"] = sorted(list(entry["sources"]))
    return bots


async def _discover_bots_from_rooms(room_ids: list[str]) -> dict[str, dict[str, Any]]:
    discovered: dict[str, dict[str, Any]] = {}
    for room_id in room_ids:
        encoded_room = quote(room_id, safe="")
        try:
            payload = await _matrix_request("GET", f"/_matrix/client/v3/rooms/{encoded_room}/joined_members")
        except HTTPException:
            continue
        joined = payload.get("joined", {}) if isinstance(payload, dict) else {}
        if not isinstance(joined, dict):
            continue
        for user_id, profile in joined.items():
            if not isinstance(user_id, str):
                continue
            if not _is_local_user(user_id) or not _is_probable_bot_user(user_id):
                continue
            display_name = ""
            if isinstance(profile, dict):
                display_name = str(profile.get("display_name", "") or "")
            discovered[user_id] = {
                "user_id": user_id,
                "username": _extract_localpart(user_id),
                "display_name": display_name,
                "sources": ["joined_members_heuristic"],
                "last_seen_ts": "",
            }
    return discovered


async def _list_known_bots(room_snapshots: list[dict[str, Any]], include_deleted: bool = False) -> list[dict[str, Any]]:
    bot_state = _load_bot_state()
    bots = _bots_from_audit_logs(bot_state)
    room_ids = [item["room_id"] for item in room_snapshots if isinstance(item.get("room_id"), str)]
    discovered = await _discover_bots_from_rooms(room_ids)

    for user_id, entry in discovered.items():
        if user_id not in bots:
            bots[user_id] = entry
            bots[user_id]["status"] = bot_state.get(user_id, "active")
            continue
        merged_sources = set(bots[user_id].get("sources", [])) | set(entry.get("sources", []))
        bots[user_id]["sources"] = sorted(list(merged_sources))
        if not bots[user_id].get("display_name"):
            bots[user_id]["display_name"] = entry.get("display_name", "")
        bots[user_id]["status"] = bot_state.get(user_id, bots[user_id].get("status", "active"))

    bot_list = list(bots.values())
    if not include_deleted:
        bot_list = [item for item in bot_list if item.get("status") != "deleted"]
    bot_list.sort(key=lambda item: ((item.get("username") or "").lower(), item.get("user_id", "")))
    return bot_list


@app.on_event("startup")
async def on_startup() -> None:
    _load_bot_credentials_cache()
    _load_registration_state()
    snapshot = _registration_snapshot()
    if not snapshot.get("active"):
        return

    expires_at = _parse_iso_utc(str(snapshot.get("expires_at", "")))
    if expires_at is None:
        return

    remaining = int(snapshot.get("remaining_seconds", 0))
    if remaining > 0:
        with _registration_lock:
            _schedule_registration_auto_close_locked(expires_at)
        return

    try:
        _close_registration_window_internal("startup_expired_reconcile", "system", force=True)
    except Exception as exc:
        _audit_log("registration_window", "startup_reconcile_failed", {"error": str(exc)})


@app.get("/", response_class=FileResponse)
async def index() -> FileResponse:
    return FileResponse(
        INDEX_HTML,
        headers={
            "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache",
        },
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/config", dependencies=[Depends(_require_control_token)])
async def api_config() -> dict[str, Any]:
    registration_window = _registration_snapshot()
    return {
        "matrix_base_url": settings.matrix_base_url,
        "matrix_server_name": settings.matrix_server_name,
        "token_protection_enabled": bool(settings.control_api_token.strip()),
        "bot_access_token_exposed": settings.expose_bot_access_token,
        "user_access_token_exposed": settings.expose_user_access_token,
        "bot_create_mode": settings.bot_create_mode,
        "user_create_mode": settings.user_create_mode,
        "bot_create_available": settings.bot_create_mode == "legacy_register" or _registration_window_allows("bots"),
        "user_create_available": settings.user_create_mode == "legacy_register" or _registration_window_allows("users"),
        "full_users_snapshot_path": settings.full_users_snapshot_path,
        "user_state_path": settings.user_state_path,
        "invite_rate_limit_window_seconds": settings.invite_rate_limit_window_seconds,
        "invite_rate_limit_max": settings.invite_rate_limit_max,
        "restart_api_mode": settings.restart_api_mode,
        "compose_project_name": settings.compose_project_name,
        "restart_targets": ["matrix", "control_api", "stack"],
        "registration_window_api_mode": settings.registration_window_api_mode,
        "registration_window_default_minutes": settings.registration_window_default_minutes,
        "registration_window_max_minutes": settings.registration_window_max_minutes,
        "registration_window_state": registration_window,
    }


@app.post("/api/ops/restart", dependencies=[Depends(_require_control_token)])
async def api_ops_restart(
    request: OpsRestartRequest,
    http_request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    _ensure_restart_runtime_ready()
    container_names = _restart_target_containers(request.target)
    client_ip = http_request.headers.get("cf-connecting-ip") or (http_request.client.host if http_request.client else "unknown")
    reason = (request.reason or "").strip()
    _audit_log(
        "ops_restart",
        "scheduled",
        {
            "target": request.target,
            "containers": container_names,
            "reason": reason,
            "client_ip": client_ip,
        },
    )
    background_tasks.add_task(_restart_containers_task, request.target, container_names, reason, client_ip)
    return {
        "scheduled": True,
        "target": request.target,
        "containers": container_names,
        "mode": settings.restart_api_mode,
        "timeout_seconds": settings.restart_timeout_seconds,
    }


@app.get("/api/ops/registration-window", dependencies=[Depends(_require_control_token)])
async def api_registration_window_status() -> dict[str, Any]:
    return {
        "mode": settings.registration_window_api_mode,
        "default_minutes": settings.registration_window_default_minutes,
        "max_minutes": settings.registration_window_max_minutes,
        "state": _registration_snapshot(),
    }


@app.post("/api/ops/registration-window/open", dependencies=[Depends(_require_control_token)])
async def api_registration_window_open(
    request: OpsRegistrationWindowOpenRequest,
    http_request: Request,
) -> dict[str, Any]:
    if not request.scope_users and not request.scope_bots:
        raise HTTPException(status_code=400, detail="At least one scope must be enabled: scope_users or scope_bots.")

    _ensure_registration_window_runtime_ready()
    if request.minutes > settings.registration_window_max_minutes:
        raise HTTPException(
            status_code=400,
            detail=f"minutes exceeds max allowed ({settings.registration_window_max_minutes}).",
        )

    reason = (request.reason or "").strip()
    client_ip = http_request.headers.get("cf-connecting-ip") or (http_request.client.host if http_request.client else "unknown")
    result = _open_registration_window_internal(
        minutes=request.minutes,
        scope_users=request.scope_users,
        scope_bots=request.scope_bots,
        reason=reason,
        client_ip=client_ip,
    )
    _audit_log(
        "registration_window",
        "opened",
        {
            "minutes": request.minutes,
            "scope_users": request.scope_users,
            "scope_bots": request.scope_bots,
            "reason": reason,
            "client_ip": client_ip,
            "state": result,
        },
    )
    return {
        "opened": True,
        "mode": settings.registration_window_api_mode,
        "state": result,
    }


@app.post("/api/ops/registration-window/close", dependencies=[Depends(_require_control_token)])
async def api_registration_window_close(
    request: OpsRegistrationWindowCloseRequest,
    http_request: Request,
) -> dict[str, Any]:
    _ensure_registration_window_runtime_ready()
    reason = (request.reason or "").strip()
    client_ip = http_request.headers.get("cf-connecting-ip") or (http_request.client.host if http_request.client else "unknown")
    result = _close_registration_window_internal(reason or "manual_close", client_ip, force=True)
    _audit_log(
        "registration_window",
        "closed",
        {
            "reason": reason,
            "client_ip": client_ip,
            "state": result,
        },
    )
    return {
        "closed": True,
        "mode": settings.registration_window_api_mode,
        "state": result,
    }


@app.get("/api/rooms", dependencies=[Depends(_require_control_token)])
async def list_rooms(
    search: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
) -> dict[str, Any]:
    snapshots = await _list_joined_room_snapshots()
    rooms = [item for item in snapshots if item.get("kind") == "room"]
    filtered = [item for item in rooms if _match_search(item, search, ["name", "room_id", "topic", "canonical_alias"])]
    return _build_paginated_response(
        label="rooms",
        source_scope="rooms joined by configured admin account",
        items=filtered,
        page=page,
        page_size=page_size,
    )


@app.get("/api/spaces", dependencies=[Depends(_require_control_token)])
async def list_spaces(
    search: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
) -> dict[str, Any]:
    snapshots = await _list_joined_room_snapshots()
    spaces = [item for item in snapshots if item.get("kind") == "space"]
    filtered = [item for item in spaces if _match_search(item, search, ["name", "room_id", "topic", "canonical_alias"])]
    return _build_paginated_response(
        label="spaces",
        source_scope="spaces joined by configured admin account",
        items=filtered,
        page=page,
        page_size=page_size,
    )


@app.get("/api/bots", dependencies=[Depends(_require_control_token)])
async def list_bots(
    search: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    include_deleted: bool = Query(default=False),
) -> dict[str, Any]:
    snapshots = await _list_joined_room_snapshots()
    bots = await _list_known_bots(snapshots, include_deleted=include_deleted)
    filtered = [item for item in bots if _match_search(item, search, ["user_id", "username", "display_name", "status", "sources"])]
    return _build_paginated_response(
        label="bots",
        source_scope="known bots from audit logs + room member heuristic",
        items=filtered,
        page=page,
        page_size=page_size,
    )


@app.get("/api/users", dependencies=[Depends(_require_control_token)])
async def list_users(
    search: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=500),
    include_bots: bool = Query(default=False),
    include_deleted: bool = Query(default=False),
) -> dict[str, Any]:
    users = _list_managed_users(include_bots=include_bots, include_deleted=include_deleted)
    filtered = [item for item in users if _match_search(item, search, ["user_id", "username", "status"])]
    return _build_paginated_response(
        label="users",
        source_scope="full local users snapshot + control-plane logical status",
        items=filtered,
        page=page,
        page_size=page_size,
    )


@app.get("/api/users/full", dependencies=[Depends(_require_control_token)])
async def list_full_users(
    search: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=500),
    bots_only: bool = Query(default=False),
) -> dict[str, Any]:
    snapshot = _load_full_users_snapshot()
    if snapshot is None:
        raise HTTPException(
            status_code=404,
            detail="Full users snapshot not found. Run scripts/refresh_full_users_snapshot.sh on the host.",
        )

    users = snapshot.get("users", [])
    if not isinstance(users, list):
        users = []
    normalized: list[dict[str, Any]] = []
    for item in users:
        if not isinstance(item, dict):
            continue
        user_id = str(item.get("user_id", "") or "")
        entry = {
            "user_id": user_id,
            "username": str(item.get("username", "") or _extract_localpart(user_id)),
            "is_bot": bool(item.get("is_bot", False)),
        }
        if bots_only and not entry["is_bot"]:
            continue
        if _match_search(entry, search, ["user_id", "username"]):
            normalized.append(entry)
    normalized.sort(key=lambda item: ((item.get("username") or "").lower(), item.get("user_id", "")))

    paged = _build_paginated_response(
        label="users",
        source_scope="full local users snapshot from conduwuit admin command",
        items=normalized,
        page=page,
        page_size=page_size,
    )
    paged["generated_at"] = snapshot.get("generated_at", "")
    paged["snapshot_path"] = settings.full_users_snapshot_path
    return paged


def _empty_overview_payload(reason: str) -> dict[str, Any]:
    return {
        "stats": {
            "spaces": 0,
            "rooms": 0,
            "bots": 0,
            "users": 0,
            "full_users": 0,
            "full_bots": 0,
        },
        "spaces": [],
        "rooms": [],
        "bots": [],
        "full_users_snapshot": {
            "generated_at": "",
            "snapshot_path": settings.full_users_snapshot_path,
        },
        "scope": {
            "rooms_spaces": "joined by configured admin account",
            "bots": "known bots from audit logs + room member heuristic",
            "users": "full local users snapshot + control-plane logical status",
            "full_users": "full local users snapshot from conduwuit admin command",
        },
        "degraded": True,
        "degraded_reason": reason,
    }


def _cache_overview_payload(payload: dict[str, Any]) -> None:
    with _overview_cache_lock:
        _overview_cache_holder = dict(payload)
        _overview_cache_holder["cached_at"] = datetime.now(timezone.utc).isoformat()
        global _overview_cache
        _overview_cache = _overview_cache_holder


def _cached_overview_with_degraded(reason: str) -> dict[str, Any] | None:
    with _overview_cache_lock:
        if _overview_cache is None:
            return None
        payload = dict(_overview_cache)
    payload["degraded"] = True
    payload["degraded_reason"] = reason
    return payload


@app.get("/api/overview", dependencies=[Depends(_require_control_token)])
async def api_overview() -> dict[str, Any]:
    try:
        snapshots = await _list_joined_room_snapshots()
        spaces = [item for item in snapshots if item.get("kind") == "space"]
        rooms = [item for item in snapshots if item.get("kind") == "room"]
        bots = await _list_known_bots(snapshots)
        full_snapshot = _load_full_users_snapshot()
        managed_users = _list_managed_users(include_bots=False, include_deleted=False)
        full_users_count = 0
        full_bot_count = 0
        if full_snapshot and isinstance(full_snapshot.get("users"), list):
            full_users_count = len(full_snapshot.get("users", []))
            full_bot_count = sum(1 for u in full_snapshot.get("users", []) if isinstance(u, dict) and u.get("is_bot"))
        payload = {
            "stats": {
                "spaces": len(spaces),
                "rooms": len(rooms),
                "bots": len(bots),
                "users": len(managed_users),
                "full_users": full_users_count,
                "full_bots": full_bot_count,
            },
            "spaces": spaces,
            "rooms": rooms,
            "bots": bots,
            "full_users_snapshot": {
                "generated_at": (full_snapshot or {}).get("generated_at", ""),
                "snapshot_path": settings.full_users_snapshot_path,
            },
            "scope": {
                "rooms_spaces": "joined by configured admin account",
                "bots": "known bots from audit logs + room member heuristic",
                "users": "full local users snapshot + control-plane logical status",
                "full_users": "full local users snapshot from conduwuit admin command",
            },
        }
        _cache_overview_payload(payload)
        return payload
    except HTTPException as exc:
        if exc.status_code >= 500:
            degraded = _cached_overview_with_degraded(str(exc.detail))
            return degraded or _empty_overview_payload(str(exc.detail))
        raise
    except Exception as exc:
        reason = str(exc) or "overview_generation_failed"
        degraded = _cached_overview_with_degraded(reason)
        return degraded or _empty_overview_payload(reason)


@app.post("/api/rooms", dependencies=[Depends(_require_control_token)])
async def create_room(request: RoomCreateRequest) -> dict[str, Any]:
    invitees = _normalize_invitees(request.invitees)
    payload = _build_room_payload(
        name=request.name,
        topic=request.topic,
        is_private=request.is_private,
        invitees=invitees,
        alias_localpart=request.alias_localpart,
        is_space=False,
    )
    created = await _matrix_request("POST", "/_matrix/client/v3/createRoom", json_body=payload)
    room_id = created.get("room_id")
    if not room_id:
        raise HTTPException(status_code=502, detail="Matrix did not return room_id.")

    if request.space_room_id:
        await _link_space_child(request.space_room_id, room_id)

    bot_auto_join_results: list[dict[str, str]] = []
    for user_id in invitees:
        if not _is_probable_bot_user(user_id):
            continue
        try:
            status = await _auto_join_bot_if_possible(user_id, room_id)
        except HTTPException as exc:
            status = f"failed:{exc.detail}"
        bot_auto_join_results.append({"bot_user_id": user_id, "status": status})

    return {"room_id": room_id, "bot_auto_join_results": bot_auto_join_results}


@app.post("/api/spaces", dependencies=[Depends(_require_control_token)])
async def create_space(request: SpaceCreateRequest) -> dict[str, Any]:
    invitees = _normalize_invitees(request.invitees)
    payload = _build_room_payload(
        name=request.name,
        topic=request.topic,
        is_private=True,
        invitees=invitees,
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

    bot_auto_join_results: list[dict[str, str]] = []
    for user_id in invitees:
        if not _is_probable_bot_user(user_id):
            continue
        try:
            status = await _auto_join_bot_if_possible(user_id, space_room_id)
        except HTTPException as exc:
            status = f"failed:{exc.detail}"
        bot_auto_join_results.append({"bot_user_id": user_id, "status": status})

    return {
        "space_room_id": space_room_id,
        "linked_children": linked_children,
        "bot_auto_join_results": bot_auto_join_results,
    }


@app.get("/api/rooms/{room_id}/config", dependencies=[Depends(_require_control_token)])
async def get_room_config(room_id: str) -> dict[str, Any]:
    snapshot = await _load_room_snapshot_with_kind_check(room_id, "room")
    return _room_config_from_snapshot(snapshot)


@app.post("/api/rooms/{room_id}/config", dependencies=[Depends(_require_control_token)])
async def update_room_config(room_id: str, request: RoomConfigUpdateRequest) -> dict[str, Any]:
    await _load_room_snapshot_with_kind_check(room_id, "room")
    return await _update_room_config(room_id, request)


@app.get("/api/rooms/{room_id}/members", dependencies=[Depends(_require_control_token)])
async def get_room_members(room_id: str) -> dict[str, Any]:
    await _load_room_snapshot_with_kind_check(room_id, "room")
    members = await _list_room_members(room_id)
    return {
        "room_id": room_id,
        "kind": "room",
        "count": len(members),
        "members": members,
    }


@app.post("/api/rooms/{room_id}/members/invite", dependencies=[Depends(_require_control_token)])
async def invite_room_member(room_id: str, request: RoomMemberUpdateRequest) -> dict[str, Any]:
    await _load_room_snapshot_with_kind_check(room_id, "room")
    normalized_user_id = _normalize_local_user_id(request.user_id)
    await _invite_user_to_room(normalized_user_id, room_id)
    membership = await _get_room_membership(normalized_user_id, room_id)
    return {
        "room_id": room_id,
        "kind": "room",
        "user_id": normalized_user_id,
        "invited": True,
        "membership": membership,
    }


@app.post("/api/rooms/{room_id}/members/remove", dependencies=[Depends(_require_control_token)])
async def remove_room_member(room_id: str, request: RoomMemberUpdateRequest) -> dict[str, Any]:
    await _load_room_snapshot_with_kind_check(room_id, "room")
    normalized_user_id = _normalize_local_user_id(request.user_id)
    await _kick_user_from_room(room_id, normalized_user_id, request.reason)
    membership = await _get_room_membership(normalized_user_id, room_id)
    return {
        "room_id": room_id,
        "kind": "room",
        "user_id": normalized_user_id,
        "removed": True,
        "membership": membership,
    }


@app.get("/api/spaces/{space_room_id}/config", dependencies=[Depends(_require_control_token)])
async def get_space_config(space_room_id: str) -> dict[str, Any]:
    snapshot = await _load_room_snapshot_with_kind_check(space_room_id, "space")
    return _room_config_from_snapshot(snapshot)


@app.post("/api/spaces/{space_room_id}/config", dependencies=[Depends(_require_control_token)])
async def update_space_config(space_room_id: str, request: RoomConfigUpdateRequest) -> dict[str, Any]:
    await _load_room_snapshot_with_kind_check(space_room_id, "space")
    return await _update_room_config(space_room_id, request)


@app.get("/api/spaces/{space_room_id}/members", dependencies=[Depends(_require_control_token)])
async def get_space_members(space_room_id: str) -> dict[str, Any]:
    await _load_room_snapshot_with_kind_check(space_room_id, "space")
    members = await _list_room_members(space_room_id)
    return {
        "room_id": space_room_id,
        "kind": "space",
        "count": len(members),
        "members": members,
    }


@app.post("/api/spaces/{space_room_id}/members/invite", dependencies=[Depends(_require_control_token)])
async def invite_space_member(space_room_id: str, request: RoomMemberUpdateRequest) -> dict[str, Any]:
    await _load_room_snapshot_with_kind_check(space_room_id, "space")
    normalized_user_id = _normalize_local_user_id(request.user_id)
    await _invite_user_to_room(normalized_user_id, space_room_id)
    membership = await _get_room_membership(normalized_user_id, space_room_id)
    return {
        "room_id": space_room_id,
        "kind": "space",
        "user_id": normalized_user_id,
        "invited": True,
        "membership": membership,
    }


@app.post("/api/spaces/{space_room_id}/members/remove", dependencies=[Depends(_require_control_token)])
async def remove_space_member(space_room_id: str, request: RoomMemberUpdateRequest) -> dict[str, Any]:
    await _load_room_snapshot_with_kind_check(space_room_id, "space")
    normalized_user_id = _normalize_local_user_id(request.user_id)
    await _kick_user_from_room(space_room_id, normalized_user_id, request.reason)
    membership = await _get_room_membership(normalized_user_id, space_room_id)
    return {
        "room_id": space_room_id,
        "kind": "space",
        "user_id": normalized_user_id,
        "removed": True,
        "membership": membership,
    }


@app.post("/api/rooms/{room_id}/archive", dependencies=[Depends(_require_control_token)])
async def archive_room(room_id: str, request: ResourceArchiveRequest) -> dict[str, Any]:
    return await _archive_room(room_id, request.note)


@app.post("/api/spaces/{space_room_id}/archive", dependencies=[Depends(_require_control_token)])
async def archive_space(space_room_id: str, request: ResourceArchiveRequest) -> dict[str, Any]:
    return await _archive_room(space_room_id, request.note)


@app.delete("/api/rooms/{room_id}", dependencies=[Depends(_require_control_token)])
async def remove_room(room_id: str) -> dict[str, Any]:
    return await _leave_forget_room(room_id)


@app.delete("/api/spaces/{space_room_id}", dependencies=[Depends(_require_control_token)])
async def remove_space(space_room_id: str) -> dict[str, Any]:
    return await _leave_forget_room(space_room_id)


@app.post("/api/bots/{user_id}/status", dependencies=[Depends(_require_control_token)])
async def update_bot_status(user_id: str, request: BotStatusUpdateRequest) -> dict[str, Any]:
    normalized = _normalize_local_user_id(user_id)
    if not normalized.startswith("@"):
        raise HTTPException(status_code=400, detail="user_id must start with '@'.")

    state = _load_bot_state()
    if request.status == "active":
        state.pop(normalized, None)
    else:
        state[normalized] = request.status
    _save_bot_state(state)

    return {
        "user_id": normalized,
        "status": request.status,
        "note": "Control-plane logical status updated. This does not deactivate Matrix account.",
    }


@app.post("/api/users/{user_id}/status", dependencies=[Depends(_require_control_token)])
async def update_user_status(user_id: str, request: UserStatusUpdateRequest) -> dict[str, Any]:
    normalized = _normalize_local_user_id(user_id)
    if not normalized.startswith("@"):
        raise HTTPException(status_code=400, detail="user_id must start with '@'.")
    if not _is_local_user(normalized):
        raise HTTPException(status_code=400, detail="only local users can be updated.")

    state = _load_user_state()
    if request.status == "active":
        state.pop(normalized, None)
    else:
        state[normalized] = request.status
    _save_user_state(state)

    return {
        "user_id": normalized,
        "status": request.status,
        "note": "Control-plane logical status updated. This does not deactivate Matrix account.",
    }


@app.post("/api/users", dependencies=[Depends(_require_control_token)])
async def create_user(request: UserCreateRequest) -> dict[str, Any]:
    if settings.user_create_mode != "legacy_register" and not _registration_window_allows("users"):
        _audit_log(
            "user_create_api",
            "blocked",
            {
                "username": request.username,
                "reason": "user_create_mode_disabled",
                "registration_window": _registration_snapshot(),
            },
        )
        raise HTTPException(
            status_code=403,
            detail=(
                "User creation via API is disabled for security. Use scripts/create_user_secure.sh on the host, "
                "set USER_CREATE_MODE=legacy_register, or open a temporary registration window in Service Ops."
            ),
        )

    password = request.password or _generate_password()
    payload: dict[str, Any] = {
        "username": request.username,
        "password": password,
        "inhibit_login": request.inhibit_login,
    }
    created = await _matrix_register_request(payload)

    user_id = _normalize_local_user_id(str(created.get("user_id", "")))
    access_token = created.get("access_token", "")

    if request.display_name and user_id and access_token:
        encoded_user = quote(user_id, safe="")
        await _matrix_request(
            "PUT",
            f"/_matrix/client/v3/profile/{encoded_user}/displayname",
            json_body={"displayname": request.display_name},
            token=access_token,
        )

    if user_id:
        bot_state = _load_bot_state()
        bot_state.pop(user_id, None)
        _save_bot_state(bot_state)

    if user_id:
        user_state = _load_user_state()
        user_state.pop(user_id, None)
        _save_user_state(user_state)

    response: dict[str, Any] = {"user_id": user_id}
    if settings.expose_user_access_token and access_token:
        response["access_token"] = access_token

    _audit_log(
        "user_create_api",
        "ok",
        {
            "username": request.username,
            "user_id": user_id,
            "mode": "legacy_register",
        },
    )
    return response


@app.post("/api/bots", dependencies=[Depends(_require_control_token)])
async def create_bot(request: BotCreateRequest) -> dict[str, Any]:
    # Keep bot account creation disabled by default to avoid depending on open registration.
    if settings.bot_create_mode != "legacy_register" and not _registration_window_allows("bots"):
        _audit_log(
            "bot_create_api",
            "blocked",
            {
                "username": request.username,
                "reason": "bot_create_mode_disabled",
                "registration_window": _registration_snapshot(),
            },
        )
        raise HTTPException(
            status_code=403,
            detail=(
                "Bot creation via API is disabled for security. Use scripts/create_bot_secure.sh on the host, "
                "set BOT_CREATE_MODE=legacy_register, or open a temporary registration window in Service Ops."
            ),
        )

    password = request.password or _generate_password()
    payload: dict[str, Any] = {
        "username": request.username,
        "password": password,
        "inhibit_login": request.inhibit_login,
    }
    created = await _matrix_register_request(payload)

    user_id = _normalize_local_user_id(str(created.get("user_id", "")))
    access_token = created.get("access_token", "")
    if user_id and password:
        _cache_bot_password(user_id, password)
    if user_id and access_token:
        _cache_bot_access_token(user_id, access_token)

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


@app.post("/api/users/invite")
async def invite_user(
    payload: UserInviteRequest,
    http_request: Request,
    authorization: str | None = Header(default=None),
    _auth: None = Depends(_require_control_token),
) -> dict[str, Any]:
    principal_key, client_ip = _invite_principal_key(http_request, authorization)
    allowed, retry_after = _check_invite_rate_limit(principal_key)
    if not allowed:
        _audit_log(
            "user_invite",
            "rate_limited",
            {
                "user_id": payload.user_id,
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

    normalized_user_id = _normalize_local_user_id(payload.user_id)
    try:
        await _invite_user_to_room(normalized_user_id, payload.room_id)
    except HTTPException as exc:
        _audit_log(
            "user_invite",
            "failed",
            {
                "user_id": normalized_user_id,
                "room_id": payload.room_id,
                "client_ip": client_ip,
                "http_status": exc.status_code,
                "error": str(exc.detail),
            },
        )
        raise

    _audit_log(
        "user_invite",
        "ok",
        {
            "user_id": normalized_user_id,
            "room_id": payload.room_id,
            "client_ip": client_ip,
        },
    )
    return {"user_id": normalized_user_id, "room_id": payload.room_id, "invited": True}


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
        normalized_user_id = _normalize_local_user_id(payload.bot_user_id)
        _audit_log(
            "bot_invite",
            "rate_limited",
            {
                "bot_user_id": normalized_user_id,
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

    normalized_user_id = _normalize_local_user_id(payload.bot_user_id)
    try:
        await _invite_user_to_room(normalized_user_id, payload.room_id)
    except HTTPException as exc:
        _audit_log(
            "bot_invite",
            "failed",
            {
                "bot_user_id": normalized_user_id,
                "room_id": payload.room_id,
                "client_ip": client_ip,
                "http_status": exc.status_code,
                "error": str(exc.detail),
            },
        )
        raise

    supplied_password = (payload.bot_password or "").strip()
    if supplied_password:
        _cache_bot_password(normalized_user_id, supplied_password)

    auto_join_attempted = bool(payload.auto_join)
    auto_join_status = "skipped_disabled"
    if auto_join_attempted:
        try:
            if supplied_password:
                token = await _login_with_password(normalized_user_id, supplied_password)
                _cache_bot_access_token(normalized_user_id, token)
                await _join_room_with_token(payload.room_id, token)
                auto_join_status = "joined"
            else:
                auto_join_status = await _auto_join_bot_if_possible(normalized_user_id, payload.room_id)
        except HTTPException as exc:
            auto_join_status = f"failed:{exc.detail}"

    _audit_log(
        "bot_invite",
        "ok",
        {
            "bot_user_id": normalized_user_id,
            "room_id": payload.room_id,
            "client_ip": client_ip,
            "auto_join_status": auto_join_status,
        },
    )
    membership_after_invite = await _get_room_membership(normalized_user_id, payload.room_id)
    return {
        "bot_user_id": normalized_user_id,
        "room_id": payload.room_id,
        "invited": True,
        "auto_join_attempted": auto_join_attempted,
        "auto_join_status": auto_join_status,
        "membership_after_invite": membership_after_invite,
        "joined": membership_after_invite == "join",
        "note": (
            "Admin-side auto-join is attempted only when a bot password is supplied now or cached from a previous run."
        ),
    }
