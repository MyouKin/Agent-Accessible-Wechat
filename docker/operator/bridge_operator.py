"""Attach the upstream WeChat tools to the Bridge's existing desktop."""

import asyncio
import base64
import binascii
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
from hmac import compare_digest
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn
from starlette.applications import Starlette
from starlette.requests import HTTPConnection, Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from bridge.desktop import pause_vnc_input
from bridge.desktop_gate import desktop_flock


LOOPBACK = {"127.0.0.1", "localhost", "::1"}
PUBLIC_ROUTES = {("GET", "/health"), ("GET", "/login"), ("GET", "/login.js")}
OPERATOR_TOKEN_FORMAT = re.compile(r"[A-Za-z0-9_-]{24,256}")
MAX_TOOL_BODY_BYTES = 32 * 1024 * 1024
LOGIN_SCREEN_STATES = {"qr", "confirm_needed", "waiting_phone_confirm"}
LOGIN_HTML = """<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agent-Accessible-Wechat 操作端</title>
<h1>微信登录</h1>
<p id="status" aria-live="polite">输入 Bridge 令牌查看登录状态。</p>
<form id="access">
  <label>Bridge 令牌 <input id="token" type="password" required autocomplete="off"></label>
  <button>连接</button>
</form>
<p><button id="refresh" disabled>刷新二维码</button> <button id="confirm" disabled>请求手机确认</button></p>
<img id="screen" alt="微信登录画面" style="max-width:100%" hidden>
<script src="/wechat/login.js" defer></script>
</html>
"""
LOGIN_SCRIPT = """
const form = document.querySelector('#access');
const screen = document.querySelector('#screen');
const status = document.querySelector('#status');
const tokenInput = document.querySelector('#token');
const refresh = document.querySelector('#refresh');
const confirm = document.querySelector('#confirm');
let token = '', image = '', connected = false, busy = false;
async function request(path, method = 'GET') {
  const result = await fetch(path, { method, headers: { Authorization: `Bearer ${token}` } });
  if (!result.ok) throw new Error(result.status === 401 ? '令牌无效' : '微信操作端暂时不可用');
  return result;
}
function clearPicture() {
  if (image) URL.revokeObjectURL(image);
  image = '';
  screen.hidden = true;
  screen.removeAttribute('src');
}
async function update(action) {
  if (!token || busy) return;
  busy = true;
  try {
    if (action) await request(`/wechat/login/${action}`, 'POST');
    const data = await (await request('/wechat/login/state')).json();
    status.textContent = data.message || data.state;
    connected = true;
    refresh.disabled = Boolean(data.logged_in);
    confirm.disabled = data.state !== 'confirm_needed';
    clearPicture();
    if (!data.logged_in) {
      const picture = await request('/wechat/login/screen');
      if (picture.status !== 204) {
        image = URL.createObjectURL(await picture.blob());
        screen.src = image;
        screen.hidden = false;
      }
    }
  } catch (error) {
    connected = false;
    refresh.disabled = confirm.disabled = true;
    status.textContent = error.message;
    clearPicture();
  } finally {
    busy = false;
  }
}
form.addEventListener('submit', event => {
  event.preventDefault();
  if (busy) return;
  token = tokenInput.value;
  tokenInput.value = '';
  update();
});
refresh.onclick = () => update('refresh');
confirm.onclick = () => update('confirm');
setInterval(() => { if (connected) update(); }, 3000);
"""


def loopback_authority(value: str) -> tuple[str, int | None] | None:
    if not value or re.search(r"[\s/@?#]", value):
        return None
    try:
        url = urlsplit(f"//{value}")
        if url.hostname in LOOPBACK:
            return url.hostname, url.port
    except ValueError:
        pass
    return None


class OperatorAccess:
    def __init__(self, app, token: str):
        if not OPERATOR_TOKEN_FORMAT.fullmatch(token):
            raise ValueError("WECHAT_TOOLS_TOKEN must use 24–256 ASCII letters, digits, underscores or hyphens")
        self.app = app
        self.authorization = f"Bearer {token}".encode("ascii")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = HTTPConnection(scope)

        async def add_security_headers(message):
            if message["type"] == "http.response.start":
                headers = [item for item in message.get("headers", []) if item[0].lower() != b"cache-control"]
                headers.extend([(b"cache-control", b"no-store"), (b"referrer-policy", b"no-referrer"),
                                (b"x-content-type-options", b"nosniff")])
                await send({**message, "headers": headers})
            else:
                await send(message)

        authority = loopback_authority(request.headers.get("host", ""))
        origin = request.headers.get("origin")
        if authority is None or (origin is not None and origin != f"http://{request.headers['host']}"):
            await Response(status_code=403)(scope, receive, add_security_headers)
            return
        route = (scope["method"], scope["path"])
        supplied = request.headers.get("authorization", "").encode("latin-1")
        if route not in PUBLIC_ROUTES and not compare_digest(supplied, self.authorization):
            await Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})(
                scope, receive, add_security_headers,
            )
            return
        await self.app(scope, receive, add_security_headers)


def restrict_attachments(wx, inbox: Path, outbox: Path) -> None:
    """Keep native file tools usable without exposing the mounted account directory."""
    send_files = wx.send_files

    def allowed_path(value, roots: tuple[Path, ...]) -> str:
        path = Path(value).resolve(strict=True)
        if not path.is_file() or not any(path.is_relative_to(root) for root in roots):
            raise ValueError("Use a regular attachment file in the operator inbox")
        return str(path)

    def materialize_attachment(path, url, data_b64, filename):
        if url:
            raise ValueError("Use a file in the operator inbox or base64 data")
        if path:
            return allowed_path(path, (inbox,))
        name = filename if filename is not None else "attachment.dat"
        if (not 1 <= len(name.encode("utf-8")) <= 200 or name in {".", ".."}
                or any(character in name for character in ("/", "\\", "\0"))):
            raise ValueError("Use a plain attachment filename")
        if not isinstance(data_b64, str) or not data_b64 or len(data_b64) > 32 * 1024 * 1024:
            raise ValueError("Provide valid base64 attachment data")
        if data_b64.startswith("data:"):
            header, _, data_b64 = data_b64.partition(",")
            if not header.endswith(";base64"):
                raise ValueError("Use a base64 data URL or raw base64")
        try:
            data = base64.b64decode(data_b64, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ValueError("Invalid base64 attachment data") from error
        if not data:
            raise ValueError("The attachment is empty")
        directory = Path(tempfile.mkdtemp(prefix="attachment-", dir=outbox))
        file = directory / name
        with file.open("xb") as destination:
            destination.write(data)
        return str(file)

    def send_attachments(chat, paths, caption=None):
        try:
            verified = [allowed_path(path, (inbox, outbox)) for path in paths]
        except (OSError, ValueError, RuntimeError, TypeError):
            return {"error": "Attachment path is outside the operator inbox or outbox"}
        return send_files(chat, verified, caption)

    wx._materialize = materialize_attachment
    wx.send_files = send_attachments


def gate_upstream_ui() -> None:
    from wechat_mcp import ui as upstream_ui

    original_ui, original_sync = upstream_ui.ui, upstream_ui.ui_sync

    def run_locked(fn, args, kwargs, cancelled=None):
        async def invoke():
            async with pause_vnc_input():
                return fn(*args, **kwargs)

        with desktop_flock(cancel=cancelled):
            return asyncio.run(invoke())

    async def ui(fn, *args, timeout=120, **kwargs):
        cancelled = threading.Event()
        try:
            return await original_ui(run_locked, fn, args, kwargs, cancelled, timeout=timeout)
        finally:
            # The executor owns the lock until a started operation really finishes.
            # An expired queued operation must never run when the lock later becomes free.
            cancelled.set()

    def ui_sync(fn, *args, **kwargs):
        return original_sync(run_locked, fn, args, kwargs)

    upstream_ui.ui, upstream_ui.ui_sync = ui, ui_sync


def build_app(token: str, public_url: str):
    from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
    from mcp.shared.exceptions import MCPError
    from mcp.types import CallToolResult, TextContent
    from wechat_mcp import a11y, xinput
    from wechat_mcp.server import build_server
    from wechat_mcp.state import STATE
    from wechat_mcp.ui import ui
    from wechat_mcp.wechat import WX

    from wechat_sessions import SessionNavigator, SessionSearchError

    registry = build_server()
    sessions = SessionNavigator(WX, a11y, xinput)
    STATE.public_base = public_url
    STATE.login_url = f"{public_url}/wechat/login"

    async def health(_request: Request) -> Response:
        return JSONResponse({"ok": True})

    async def login(_request: Request) -> Response:
        return HTMLResponse(LOGIN_HTML, headers={"Content-Security-Policy": (
            "default-src 'none'; script-src 'self'; img-src blob:; connect-src 'self'; "
            "style-src 'unsafe-inline'; frame-ancestors 'none'; form-action 'none'"
        )})

    async def login_script(_request: Request) -> Response:
        return Response(LOGIN_SCRIPT, media_type="text/javascript")

    async def login_state(_request: Request) -> Response:
        return JSONResponse(await ui(WX.login_state))

    def capture_login():
        status = WX.login_state()
        labels = set(status.get("visible_text", []))
        if (status.get("logged_in") or (status.get("state") not in LOGIN_SCREEN_STATES
                and not {"Scan to log in", "QR Code"} <= labels)):
            return None
        WX.calibrate()
        frames = [frame for frame in a11y.frames(WX.app()) if a11y.is_showing(frame)]
        if len(frames) != 1 or WX.main_frame() is not None:
            return None
        frame = frames[0]
        box = a11y.box(frame)
        x, y = WX.screen_xy(frame, 0, 0)
        width, height = round(box.w * WX.R), round(box.h * WX.R)
        screen_width, screen_height = WX.screen_size()
        if 0 <= x < x + width <= screen_width and 0 <= y < y + height <= screen_height and width < 900:
            return xinput.screenshot(crop=(x, y, width, height))
        return None

    async def login_screen(_request: Request) -> Response:
        image = await ui(capture_login)
        return Response(image, media_type="image/png") if image is not None else Response(status_code=204)

    async def login_refresh(_request: Request) -> Response:
        def refresh():
            state = WX.login_state()
            if state.get("logged_in") or state.get("state") not in {"qr", "confirm_needed", "network_error"}:
                return state
            return WX.refresh_qr()

        return JSONResponse(await ui(refresh))

    async def login_confirm(_request: Request) -> Response:
        def confirm():
            state = WX.login_state()
            if state.get("logged_in") or state.get("state") != "confirm_needed":
                return state
            return WX.confirm_login()

        return JSONResponse(await ui(confirm))

    async def agent_read(request: Request) -> Response:
        payload = await request.json()
        if not isinstance(payload, dict):
            return JSONResponse({"detail": "Invalid tool arguments"}, status_code=422)
        chat, limit = payload.get("chat"), payload.get("limit")
        if not isinstance(chat, str) or not chat or type(limit) is not int or not 1 <= limit <= 20:
            return JSONResponse({"detail": "Invalid tool arguments"}, status_code=422)

        def current_group_is_expected():
            current = WX.current_chat()
            return isinstance(current, dict) and current.get("is_group") is True and current.get("name") == chat

        def read():
            if not current_group_is_expected():
                return None
            result = WX.read_messages(None, limit, False, 0, max_scrolls=0)
            if not current_group_is_expected():
                return None
            messages = []
            for row in result.get("messages", [])[-limit:]:
                if not isinstance(row, dict):
                    continue
                messages.append({key: value[:360] for key in ("type", "text", "sender", "direction", "time")
                                 if isinstance(value := row.get(key), str)})
            return messages

        try:
            messages = await ui(read, timeout=4)
        except (RuntimeError, TimeoutError):
            messages = None
        if messages is None:
            return JSONResponse({"detail": "The current group could not be verified by the attached driver"}, status_code=409)
        return JSONResponse({"chat": chat, "source": "wechat_mcp", "messages": messages})

    async def list_sessions(_request: Request) -> Response:
        try:
            return JSONResponse(await ui(sessions.inventory, timeout=8))
        except SessionSearchError as error:
            return JSONResponse({"detail": str(error)}, status_code=409)
        except Exception:
            logging.debug("Native session inventory was unavailable", exc_info=True)
            return JSONResponse({"detail": "The native session inventory is temporarily unavailable"}, status_code=503)

    async def open_session(request: Request) -> Response:
        try:
            payload = await request.json()
        except (ValueError, RecursionError):
            payload = None
        if not isinstance(payload, dict) or set(payload) != {"name", "kind"}:
            return JSONResponse({"detail": "A session name and kind are required"}, status_code=422)
        name, kind = payload["name"], payload["kind"]
        if (not isinstance(name, str) or not 1 <= len(name) <= 256 or name != name.strip()
                or any(ord(character) < 32 or ord(character) == 127 for character in name)
                or not isinstance(kind, str) or kind not in {"group", "private", "unknown"}):
            return JSONResponse({"detail": "Invalid session name or kind"}, status_code=422)
        try:
            return JSONResponse(await ui(sessions.open, name, kind, timeout=20))
        except SessionSearchError as error:
            return JSONResponse({"detail": str(error)}, status_code=409)
        except Exception:
            logging.debug("Native session activation was unavailable", exc_info=True)
            return JSONResponse({"detail": "The requested session could not be safely verified"}, status_code=409)

    async def list_tools(_request: Request) -> Response:
        tools = await registry.list_tools()
        return JSONResponse({"tools": [tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in tools]})

    async def call_tool(request: Request) -> Response:
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_TOOL_BODY_BYTES:
                return JSONResponse({"detail": "Tool arguments are too large"}, status_code=413)
            body.extend(chunk)
        try:
            arguments = json.loads(body)
        except (ValueError, RecursionError):
            arguments = None
        if not isinstance(arguments, dict):
            return JSONResponse({"detail": "Tool arguments must be a JSON object"}, status_code=400)

        name = request.path_params["name"]
        error_message = None
        try:
            result = await registry.call_tool(name, arguments)
        except UnexpectedToolError:
            logging.exception("Native tool %r failed", name)
            error_message = "The native WeChat tool failed"
        except (ToolError, MCPError) as error:
            error_message = str(error)
        else:
            if not isinstance(result, CallToolResult):
                error_message = "This WeChat tool requires additional input"
        if error_message is not None:
            result = CallToolResult(content=[TextContent(type="text", text=error_message)], is_error=True)
        return JSONResponse(result.model_dump(mode="json", by_alias=True, exclude_none=True))

    application = Starlette(routes=[
        Route("/health", health),
        Route("/login", login),
        Route("/login.js", login_script),
        Route("/login/state", login_state),
        Route("/login/screen", login_screen),
        Route("/login/refresh", login_refresh, methods=["POST"]),
        Route("/login/confirm", login_confirm, methods=["POST"]),
        Route("/agent/read-current-chat", agent_read, methods=["POST"]),
        Route("/sessions", list_sessions),
        Route("/sessions/open", open_session, methods=["POST"]),
        Route("/tools", list_tools),
        Route("/tools/{name}", call_tool, methods=["POST"]),
    ])
    return OperatorAccess(application, token)


def main() -> None:
    # Keep the operator credential out of WeChat's child environment.
    token = os.environ.pop("WECHAT_TOOLS_TOKEN", "")
    if not OPERATOR_TOKEN_FORMAT.fullmatch(token):
        raise ValueError("WECHAT_TOOLS_TOKEN does not match the required format")
    socket_path = Path(os.environ["AAW_WECHAT_TOOLS_SOCKET"])
    public_url = os.getenv("WECHAT_TOOLS_PUBLIC_URL", "http://127.0.0.1:8765").rstrip("/")
    url = urlsplit(public_url)
    if url.scheme != "http" or loopback_authority(url.netloc) is None or url.path or url.query or url.fragment:
        raise ValueError("WECHAT_TOOLS_PUBLIC_URL must be a local HTTP origin")
    auto_login = os.getenv("WECHAT_TOOLS_AUTO_LOGIN", "false").lower()
    if auto_login not in {"true", "false"}:
        raise ValueError("WECHAT_TOOLS_AUTO_LOGIN must be true or false")
    if not all(os.getenv(name) for name in ("AAW_DESKTOP_LOCK", "DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "AT_SPI_BUS_ADDRESS")):
        raise ValueError("The WeChat tools require the Bridge desktop session")
    subprocess.run(["xdpyinfo"], stdout=subprocess.DEVNULL, check=True, timeout=5)

    from wechat_mcp.runtime import Runtime
    from wechat_mcp.server import start_login_watcher
    from wechat_mcp.state import STATE
    from wechat_mcp import ui as driver_ui

    data = Path(os.getenv("WECHAT_MCP_HOME", "/data")).resolve()
    inbox, outbox = data / "inbox", data / "outbox"
    for directory in (inbox, outbox):
        if directory.is_symlink():
            raise ValueError("Attachment directories must not be symlinks")
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    gate_upstream_ui()
    runtime = STATE.runtime = Runtime()
    # Use the same locale as the Bridge's direct client path. Upstream defaults to Chinese
    # when LC_ALL is absent, which would silently change an existing profile's desktop.
    runtime.env["LC_ALL"] = os.getenv("LC_ALL") or os.getenv("LANG", "C.UTF-8")

    def restart_client_if_needed():
        if not runtime._stop.is_set() and not runtime.paused and not runtime.alive("wechat"):
            runtime.start_wechat()
            runtime.wechat_restarts += 1

    def watch_client():
        while not runtime._stop.wait(5):
            if not runtime.paused and not runtime.alive("wechat"):
                try:
                    driver_ui.ui_sync(restart_client_if_needed)
                except Exception:
                    logging.warning("WeChat client restart did not complete")

    try:
        runtime.start_wechat()
        from wechat_mcp.wechat import WX

        application = build_app(token, public_url)
        restrict_attachments(WX, inbox, outbox)
        if auto_login == "true":
            start_login_watcher()
        threading.Thread(target=watch_client, daemon=True, name="watchdog").start()
        socket_path.unlink(missing_ok=True)
        uvicorn.run(application, uds=str(socket_path), access_log=False, proxy_headers=False)
    finally:
        runtime.paused = True
        runtime._stop.set()
        runtime.stop_wechat()


if __name__ == "__main__":
    main()
