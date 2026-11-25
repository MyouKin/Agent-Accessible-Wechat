"""Communicate with the WeChat SDK worker over the local Unix socket."""

import re

import httpx
from fastapi import HTTPException, Request
from starlette.responses import StreamingResponse

NATIVE_NAME = re.compile(r"wechat_[A-Za-z0-9_]{1,96}")
PUBLIC_ROUTES = frozenset({"health", "login", "login.js"})
TOOLS_ROUTES = PUBLIC_ROUTES | {"login/state", "login/screen", "login/refresh", "login/confirm"}
REQUEST_HEADERS = frozenset({"accept", "content-type", "host", "origin"})
RESPONSE_HEADERS = frozenset({"content-type", "www-authenticate", "cache-control", "referrer-policy", "x-content-type-options",
                              "content-security-policy", "content-encoding"})


async def _native_request(client: httpx.AsyncClient, token: str, method: str, path: str, *,
                          arguments: dict | None = None, timeout: float | httpx.Timeout = 5) -> dict:
    try:
        response = await client.request(
            method, path, json=arguments, timeout=timeout,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        response.raise_for_status()
        result = response.json()
    except (httpx.HTTPError, ValueError) as error:
        raise ValueError("The native WeChat tools worker is unavailable") from error
    if not isinstance(result, dict):
        raise ValueError("The native WeChat tools worker returned an invalid result")
    return result


async def list_native_tools(client: httpx.AsyncClient, token: str) -> list[dict]:
    result = await _native_request(client, token, "GET", "/tools")
    tools = result.get("tools")
    if not isinstance(tools, list):
        raise ValueError("The native WeChat tools worker returned an invalid catalog")
    names = set()
    for tool in tools:
        name = tool.get("name") if isinstance(tool, dict) else None
        if (not isinstance(name, str) or not NATIVE_NAME.fullmatch(name) or name in names
                or not isinstance(tool.get("inputSchema"), dict)):
            raise ValueError("The native WeChat tools worker returned an invalid tool")
        names.add(name)
    return tools


async def call_native_tool(client: httpx.AsyncClient, token: str, name: str, arguments: dict) -> dict:
    result = await _native_request(client, token, "POST", f"/tools/{name}",
                                   arguments=arguments, timeout=client.timeout)
    if not isinstance(result.get("content"), list):
        raise ValueError("The native WeChat tools worker returned an invalid tool result")
    return result


async def read_attached_chat(client: httpx.AsyncClient, token: str, chat: str, limit: int) -> dict | None:
    try:
        response = await client.post("/agent/read-current-chat", headers={"Authorization": f"Bearer {token}"},
                                     json={"chat": chat, "limit": limit}, timeout=5)
        response.raise_for_status()
        result = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    if (isinstance(result, dict) and result.get("chat") == chat and result.get("source") == "wechat_mcp"
            and isinstance(result.get("messages"), list) and len(result["messages"]) <= limit):
        return result
    return None


async def forward_wechat_tools(client: httpx.AsyncClient, request: Request, path: str, token: str) -> StreamingResponse:
    headers = {name: value for name, value in request.headers.items() if name.lower() in REQUEST_HEADERS}
    headers["Authorization"] = f"Bearer {token}"
    kwargs = {"content": request.stream()} if request.method == "POST" else {}
    upstream = client.build_request(request.method, f"/{path}", headers=headers, **kwargs)
    try:
        response = await client.send(upstream, stream=True)
    except httpx.RequestError as error:
        raise HTTPException(503, "The WeChat tools worker is unavailable") from error
    forwarded = {name: value for name, value in response.headers.items() if name.lower() in RESPONSE_HEADERS}

    async def body():
        try:
            async for chunk in response.aiter_raw():
                yield chunk
        finally:
            await response.aclose()

    return StreamingResponse(body(), status_code=response.status_code, headers=forwarded)
