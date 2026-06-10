import asyncio
import json
import os
from pathlib import Path
from urllib.parse import quote

import httpx
import websockets
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse

from .auth import (
    get_webui_cookie_name,
    get_webui_username,
    is_web_auth_enabled,
    parse_webui_session_token,
)


BASE_URL = "https://aistudio.xiaomimimo.com"
WS_URL = "wss://aistudio.xiaomimimo.com/ws/proxy"
ROOT_DIR = Path(__file__).resolve().parent.parent
USERS_DIR = ROOT_DIR / "users"
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "content-encoding",
    "content-length",
}
ALLOWED_PREFIXES = (
    "open-apis/bot",
    "open-apis/chat",
    "open-apis/resource",
    "open-apis/tts",
    "open-apis/contact",
    "open-apis/user/mi",
)

router = APIRouter()


def _load_user(uid: str) -> dict | None:
    safe_uid = "".join(ch for ch in uid if ch.isdigit())
    if safe_uid != uid:
        return None
    path = USERS_DIR / f"user_{safe_uid}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if str(data.get("userId", "")).strip() == uid else None


def _cookies_for_user(user: dict) -> dict[str, str]:
    ph = str(user.get("xiaomichatbot_ph", "") or "")
    return {
        "serviceToken": str(user.get("serviceToken", "") or ""),
        "userId": str(user.get("userId", "") or ""),
        "xiaomichatbot_ph": ph,
    }


def _aistudio_headers(request: Request | None = None) -> dict[str, str]:
    headers = {
        "Accept": "*/*",
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/",
        "User-Agent": "Mozilla/5.0",
        "x-timezone": "Asia/Shanghai",
    }
    if request is not None:
        content_type = request.headers.get("content-type")
        accept = request.headers.get("accept")
        if content_type:
            headers["Content-Type"] = content_type
        if accept:
            headers["Accept"] = accept
    return headers


def _sanitize_path(path: str) -> str | None:
    normalized = path.strip().lstrip("/")
    if not normalized.startswith("open-apis/"):
        normalized = f"open-apis/{normalized}"
    if ".." in normalized.split("/"):
        return None
    if not any(normalized == prefix or normalized.startswith(f"{prefix}/") for prefix in ALLOWED_PREFIXES):
        return None
    return normalized


def _query_items_with_ph(request: Request, user: dict) -> list[tuple[str, str]]:
    items = list(request.query_params.multi_items())
    if not any(key == "xiaomichatbot_ph" for key, _ in items):
        items.append(("xiaomichatbot_ph", str(user.get("xiaomichatbot_ph", "") or "")))
    return items


def _response_headers(upstream: httpx.Response) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in upstream.headers.items():
        if key.lower() not in HOP_BY_HOP_HEADERS:
            headers[key] = value
    return headers


def _is_websocket_authenticated(ws: WebSocket) -> bool:
    if not is_web_auth_enabled():
        return True
    token = ws.cookies.get(get_webui_cookie_name())
    payload = parse_webui_session_token(token)
    return bool(payload and payload.get("u") == get_webui_username())


async def _fetch_ws_ticket(user: dict) -> str | None:
    ph = str(user.get("xiaomichatbot_ph", "") or "")
    url = f"{BASE_URL}/open-apis/user/ws/ticket?xiaomichatbot_ph={quote(ph)}"
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(url, cookies=_cookies_for_user(user), headers=_aistudio_headers())
    try:
        payload = response.json()
    except json.JSONDecodeError:
        return None
    if response.status_code != 200 or not isinstance(payload, dict):
        return None
    ticket = (payload.get("data") or {}).get("ticket")
    return str(ticket) if ticket else None


async def _connect_upstream_ws(ticket: str, user: dict):
    cookie_str = "; ".join(
        f'{key}="{value}"' if " " in value or "=" in value else f"{key}={value}"
        for key, value in _cookies_for_user(user).items()
    )
    headers = {"Cookie": cookie_str, "Origin": BASE_URL}
    url = f"{WS_URL}?ticket={quote(ticket)}"
    try:
        return await websockets.connect(url, additional_headers=headers, max_size=10**8)
    except TypeError as exc:
        if "additional_headers" not in str(exc):
            raise
        return await websockets.connect(url, extra_headers=headers, max_size=10**8)


@router.api_route("/api/web-chat/{uid}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def web_chat_http_proxy(uid: str, path: str, request: Request):
    user = _load_user(uid)
    if user is None:
        return JSONResponse({"detail": "账号不存在"}, status_code=404)
    normalized_path = _sanitize_path(path)
    if normalized_path is None:
        return JSONResponse({"detail": "不允许代理该路径"}, status_code=403)

    target_url = f"{BASE_URL}/{normalized_path}"
    body = await request.body()
    client = httpx.AsyncClient(timeout=None)
    try:
        upstream = await client.send(
            client.build_request(
                request.method,
                target_url,
                params=_query_items_with_ph(request, user),
                content=body if request.method not in {"GET", "HEAD"} else None,
                cookies=_cookies_for_user(user),
                headers=_aistudio_headers(request),
            ),
            stream=True,
        )
    except httpx.HTTPError as exc:
        await client.aclose()
        return JSONResponse({"detail": f"上游请求失败: {exc}"}, status_code=502)

    async def stream_body():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        stream_body(),
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
        headers=_response_headers(upstream),
    )


@router.websocket("/api/web-chat/{uid}/ws/proxy")
async def web_chat_ws_proxy(ws: WebSocket, uid: str):
    await ws.accept()
    if not _is_websocket_authenticated(ws):
        await ws.close(code=4401, reason="unauthorized")
        return
    user = _load_user(uid)
    if user is None:
        await ws.close(code=4404, reason="account not found")
        return
    ticket = await _fetch_ws_ticket(user)
    if not ticket:
        await ws.close(code=4401, reason="failed to fetch upstream ticket")
        return

    try:
        upstream = await _connect_upstream_ws(ticket, user)
    except Exception:
        await ws.close(code=1011, reason="failed to connect upstream")
        return

    async def client_to_upstream():
        while True:
            message = await ws.receive()
            msg_type = message.get("type")
            if msg_type == "websocket.disconnect":
                break
            if "text" in message:
                await upstream.send(message["text"])
            elif "bytes" in message:
                await upstream.send(message["bytes"])

    async def upstream_to_client():
        async for message in upstream:
            if isinstance(message, bytes):
                await ws.send_bytes(message)
            else:
                await ws.send_text(str(message))

    tasks = [
        asyncio.create_task(client_to_upstream()),
        asyncio.create_task(upstream_to_client()),
    ]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    except (WebSocketDisconnect, websockets.ConnectionClosed):
        pass
    finally:
        try:
            await upstream.close()
        except Exception:
            pass
        try:
            await ws.close()
        except Exception:
            pass
