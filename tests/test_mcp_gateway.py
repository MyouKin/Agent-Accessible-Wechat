"""Exercise the HTTP/MCP permission boundary with a private synthetic Unix worker."""

import json
import socketserver
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Thread

import pytest
from fastapi.testclient import TestClient

from bridge.config import Settings
from bridge.server import create_app
from test_server import SyntheticSender
from test_sync import SyntheticSource, snapshot


CONTROL_TOKEN = "test-control-" + "c" * 24
MCP_TOKEN = "test-mcp-" + "m" * 24
WORKER_TOKEN = "test-private-" + "p" * 24
CONFIGURED_CHAT_READ = "get_configured_chat_messages"
BRIDGE_TOOLS = {
    "get_status", "get_recent_messages", "get_new_messages", "search_messages",
    "get_media", "draft_message", "wait_for_messages", "get_message", "list_drafts",
    "get_draft_status", "search_memories", "get_sync_gaps", CONFIGURED_CHAT_READ,
}
CONVERSATION_TOOLS = {
    "get_message", "get_recent_messages", "get_new_messages", "search_messages",
    "search_memories", CONFIGURED_CHAT_READ,
}
NATIVE_SEND = "wechat_send_text"
NATIVE_IMAGE = "wechat_get_screenshot"
NATIVE_CHAT_READ = "wechat_read_current_chat"
IMAGE_DATA = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jG2sAAAAASUVORK5CYII="
SEND_SCHEMA = {
    "type": "object", "properties": {"chat": {"type": "string"}, "text": {"type": "string"}},
    "required": ["chat", "text"],
}
NATIVE_TOOLS = {NATIVE_SEND, NATIVE_IMAGE, NATIVE_CHAT_READ, *(f"wechat_fixture_{index:02}" for index in range(62))}
CONVERSATION = {"scope": "conversation", "chat": "test"}


def authorization(token):
    return {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"}


class NativeHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, status, data=None, content_type="application/json"):
        if content_type == "application/json" and data is not None:
            data = json.dumps(data).encode()
        data = data or b""
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def record(self, body=None):
        supplied = self.headers.get("Authorization")
        self.server.requests.append((self.command, self.path, supplied, body))
        return supplied == f"Bearer {WORKER_TOKEN}"

    def do_GET(self):
        authorized = self.record()
        if self.path == "/login":
            self.reply(200, b"<html>native login fixture</html>", "text/html")
        elif self.path == "/health":
            self.reply(200, {"ok": True})
        elif not authorized:
            self.reply(401)
        elif self.path == "/login/state":
            self.reply(200, {"state": "confirm_needed"})
        elif self.path == "/tools":
            self.reply(200, {"tools": [
                {"name": name, "description": "Synthetic native tool", "inputSchema": (
                    SEND_SCHEMA if name == NATIVE_SEND else {"type": "object"}
                )} for name in sorted(NATIVE_TOOLS)
            ]})
        else:
            self.reply(404)

    def do_POST(self):
        size = int(self.headers.get("Content-Length", 0))
        arguments = json.loads(self.rfile.read(size)) if size else None
        if not self.record(arguments):
            self.reply(401)
            return
        if self.path == "/login/confirm":
            self.reply(200, {"state": "waiting_phone_confirm"})
            return
        if self.path == "/agent/read-current-chat":
            if self.server.fail_live_read:
                self.reply(503)
            else:
                self.reply(200, {"chat": arguments["chat"], "source": "wechat_mcp",
                                 "messages": [{"text": "native fixture live row"}]})
            return
        name = self.path.removeprefix("/tools/")
        if name not in NATIVE_TOOLS or not isinstance(arguments, dict):
            self.reply(404)
        elif self.server.fail_tool:
            self.reply(503)
        elif name == NATIVE_IMAGE:
            self.reply(200, {"content": [{"type": "image", "data": IMAGE_DATA, "mimeType": "image/png"}],
                             "structuredContent": {"width": 1, "height": 1}})
        else:
            forwarded = {"name": name, "arguments": arguments}
            self.reply(200, {"content": [{"type": "text", "text": "fixture native reply"}],
                             "structuredContent": {"forwarded": forwarded}})


@contextmanager
def fake_native_worker():
    if not hasattr(socketserver, "UnixStreamServer"):
        pytest.skip("The attached worker requires a Unix socket")

    # Keep AF_UNIX paths short on macOS even when pytest's cache path is long.
    with TemporaryDirectory(prefix="aaw-mcp-", dir="/tmp") as directory:
        path = str(Path(directory) / "worker.sock")
        with socketserver.UnixStreamServer(path, NativeHandler) as server:
            server.requests = []
            server.fail_live_read = False
            server.fail_tool = False
            thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
            thread.start()
            try:
                yield path, server
            finally:
                server.shutdown()
                thread.join(timeout=2)


def client_for(tmp_path, *, mcp=True, socket_path=None):
    app = create_app(
        Settings(chat="test", data_dir=str(tmp_path), mcp_enabled=mcp), CONTROL_TOKEN, MCP_TOKEN,
        source=SyntheticSource(snapshot("AB")), sender=SyntheticSender(),
        wechat_tools_socket=socket_path, wechat_tools_token=WORKER_TOKEN if socket_path else "",
    )
    return TestClient(app, base_url="http://127.0.0.1:8765")


@pytest.fixture
def gateway(tmp_path):
    with fake_native_worker() as (socket_path, worker), client_for(tmp_path, socket_path=socket_path) as client:
        yield client, worker


def rpc(client, token, method, parameters=None):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": parameters or {}}
    return client.post("/mcp", json=payload, headers=authorization(token))


def http_call(client, token, name, arguments=None, *, query=None):
    return client.post(f"/tools/{name}", json=arguments if arguments is not None else {},
                       headers=authorization(token), params=query)


def tool_entries(response, *, mcp=False):
    assert response.status_code == 200, response.text
    envelope = response.json()
    tools = (envelope["result"] if mcp else envelope)["tools"]
    assert len({tool["name"] for tool in tools}) == len(tools)
    return {tool["name"]: tool for tool in tools}


def result_body(response, *, mcp=False):
    assert response.status_code == 200, response.text
    data = response.json()
    return data["result"] if mcp else data


def assert_tool_refused(response, *, statuses=(403, 404)):
    if response.status_code in statuses:
        return
    assert response.status_code == 200, response.text
    body = response.json()
    assert "error" in body or body.get("result", body).get("isError") is True


def test_mcp_and_trusted_http_expose_one_catalog_and_raw_native_calls(gateway):
    client, worker = gateway
    http_catalog = tool_entries(client.get("/tools", headers=authorization(CONTROL_TOKEN)))
    mcp_catalog = tool_entries(rpc(client, MCP_TOKEN, "tools/list"), mcp=True)
    assert set(http_catalog) == set(mcp_catalog) == BRIDGE_TOOLS | NATIVE_TOOLS
    assert len(http_catalog) == 78
    assert http_catalog[NATIVE_SEND]["inputSchema"] == mcp_catalog[NATIVE_SEND]["inputSchema"] == SEND_SCHEMA
    assert any((method, path, auth) == ("GET", "/tools", f"Bearer {WORKER_TOKEN}")
               for method, path, auth, _ in worker.requests)

    arguments = {"chat": "fixture chat", "text": "fixture text"}
    parameters = {"name": NATIVE_SEND, "arguments": arguments}
    native_http = result_body(http_call(client, CONTROL_TOKEN, NATIVE_SEND, arguments))
    native_mcp = result_body(rpc(client, MCP_TOKEN, "tools/call", parameters), mcp=True)
    assert native_http["structuredContent"] == native_mcp["structuredContent"] == {"forwarded": parameters}
    assert [r for r in worker.requests if r[0] == "POST"] == [
        ("POST", f"/tools/{NATIVE_SEND}", f"Bearer {WORKER_TOKEN}", arguments),
        ("POST", f"/tools/{NATIVE_SEND}", f"Bearer {WORKER_TOKEN}", arguments),
    ]
    assert result_body(http_call(client, CONTROL_TOKEN, "get_status")).get("isError") is not True


def test_upstream_chat_read_is_forwarded_through_the_trusted_catalog(gateway):
    client, worker = gateway
    arguments = {"limit": 1}
    parameters = {"name": NATIVE_CHAT_READ, "arguments": arguments}
    by_http = result_body(http_call(client, CONTROL_TOKEN, NATIVE_CHAT_READ, arguments))
    by_mcp = result_body(rpc(client, MCP_TOKEN, "tools/call", parameters), mcp=True)
    assert by_http["structuredContent"] == by_mcp["structuredContent"] == {"forwarded": parameters}
    assert [record for record in worker.requests if record[0] == "POST"] == [
        ("POST", f"/tools/{NATIVE_CHAT_READ}", f"Bearer {WORKER_TOKEN}", arguments),
        ("POST", f"/tools/{NATIVE_CHAT_READ}", f"Bearer {WORKER_TOKEN}", arguments),
    ]


def test_native_image_and_structured_result_across_both_transports(gateway):
    client, _ = gateway
    for data in (result_body(http_call(client, CONTROL_TOKEN, NATIVE_IMAGE)),
                 result_body(rpc(client, MCP_TOKEN, "tools/call", {"name": NATIVE_IMAGE}), mcp=True)):
        assert data["content"] == [{"type": "image", "data": IMAGE_DATA, "mimeType": "image/png"}]
        assert data["structuredContent"] == {"width": 1, "height": 1}


def test_conversation_scope_remains_same_group_and_cannot_dispatch_native_actions(gateway):
    client, worker = gateway
    start = Barrier(2, timeout=3)

    def discover(full):
        start.wait()
        if full:
            return set(tool_entries(rpc(client, MCP_TOKEN, "tools/list"), mcp=True))
        return set(tool_entries(client.get("/tools", headers=authorization(CONTROL_TOKEN), params=CONVERSATION)))

    with ThreadPoolExecutor(max_workers=2) as clients:
        conversation = clients.submit(discover, False)
        trusted = clients.submit(discover, True)
        assert trusted.result(timeout=5) == BRIDGE_TOOLS | NATIVE_TOOLS
        assert conversation.result(timeout=5) == CONVERSATION_TOOLS

    arguments = {"chat": "fixture chat", "text": "fixture text"}
    assert_tool_refused(http_call(client, CONTROL_TOKEN, NATIVE_SEND, arguments, query=CONVERSATION))
    assert_tool_refused(http_call(client, CONTROL_TOKEN, NATIVE_CHAT_READ, {"limit": 1}, query=CONVERSATION))
    assert_tool_refused(http_call(client, CONTROL_TOKEN, "draft_message", {"text": "fixture"}, query=CONVERSATION))
    wrong_chat = {**CONVERSATION, "chat": "another chat"}
    assert_tool_refused(http_call(client, CONTROL_TOKEN, "get_recent_messages", query=wrong_chat), statuses=(403, 409))
    assert not any(method == "POST" for method, _, _, _ in worker.requests)


def test_conversation_live_read_identifies_worker_and_local_sources(gateway):
    client, worker = gateway

    def read():
        response = http_call(client, CONTROL_TOKEN, CONFIGURED_CHAT_READ, {"limit": 1}, query=CONVERSATION)
        return json.loads(result_body(response)["content"][0]["text"])

    assert read() == {"chat": "test", "source": "wechat_mcp", "messages": [{"text": "native fixture live row"}]}
    assert any(path == "/agent/read-current-chat" and auth == f"Bearer {WORKER_TOKEN}"
               and args == {"chat": "test", "limit": 1} for _, path, auth, args in worker.requests)
    worker.fail_live_read = True
    local = read()
    assert local["chat"] == "test" and local["source"] == "accessibility"
    assert [row["text"] for row in local["messages"]] == ["B"]


def test_native_worker_error_surfaces_without_retry_or_local_fallback(gateway):
    client, worker = gateway
    worker.fail_tool = True
    arguments = {"chat": "fixture chat", "text": "fixture text"}
    assert_tool_refused(http_call(client, CONTROL_TOKEN, NATIVE_SEND, arguments), statuses=(502, 503))
    assert_tool_refused(rpc(client, MCP_TOKEN, "tools/call", {"name": NATIVE_SEND, "arguments": arguments}),
                        statuses=(502, 503))
    assert [request for request in worker.requests if request[0] == "POST"] == [
        ("POST", f"/tools/{NATIVE_SEND}", f"Bearer {WORKER_TOKEN}", arguments),
        ("POST", f"/tools/{NATIVE_SEND}", f"Bearer {WORKER_TOKEN}", arguments),
    ]


def test_control_mcp_and_private_worker_credentials_do_not_cross(gateway):
    client, worker = gateway
    initial_requests = worker.requests.copy()
    parameters = {"name": NATIVE_SEND, "arguments": {"chat": "fixture chat", "text": "fixture text"}}
    for token in (CONTROL_TOKEN, WORKER_TOKEN):
        assert rpc(client, token, "tools/list").status_code == 401
        assert rpc(client, token, "tools/call", parameters).status_code == 401
    for token in (MCP_TOKEN, WORKER_TOKEN):
        assert client.get("/tools", headers=authorization(token)).status_code == 401
        assert http_call(client, token, NATIVE_SEND, parameters["arguments"]).status_code == 401
    for token in (MCP_TOKEN, CONTROL_TOKEN, WORKER_TOKEN):
        response = client.post("/wechat/mcp", headers=authorization(token), json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {},
        })
        assert response.status_code in {401, 404, 405, 410}
    assert worker.requests == initial_requests


def test_public_login_shell_and_health_keep_private_worker_auth_inside_bridge(gateway):
    client, worker = gateway
    page = client.get("/wechat/login")
    assert page.status_code == 200 and "native login fixture" in page.text
    assert client.get("/wechat/health").json() == {"ok": True}
    for token in (MCP_TOKEN, WORKER_TOKEN):
        assert client.get("/wechat/login/state", headers=authorization(token)).status_code == 401
        assert client.post("/wechat/login/confirm", headers=authorization(token)).status_code == 401
    assert client.get("/wechat/login/state").status_code == 401

    state = client.get("/wechat/login/state", headers=authorization(CONTROL_TOKEN))
    assert state.status_code == 200 and state.json()["state"] == "confirm_needed"
    confirm = client.post("/wechat/login/confirm", headers=authorization(CONTROL_TOKEN))
    assert confirm.status_code == 200 and confirm.json()["state"] == "waiting_phone_confirm"
    assert {auth for _, path, auth, _ in worker.requests if path in {"/login/state", "/login/confirm"}} == {
        f"Bearer {WORKER_TOKEN}",
    }


@pytest.mark.parametrize("mcp_enabled", [True, False])
def test_local_http_registry_does_not_depend_on_worker_or_mcp_setting(tmp_path, mcp_enabled):
    with client_for(tmp_path, mcp=mcp_enabled, socket_path=str(tmp_path / "stopped.sock")) as client:
        assert set(tool_entries(client.get("/tools", headers=authorization(CONTROL_TOKEN)))) == BRIDGE_TOOLS
        assert result_body(http_call(client, CONTROL_TOKEN, "get_status")).get("isError") is not True
        listed = rpc(client, MCP_TOKEN, "tools/list")
        if mcp_enabled:
            assert set(tool_entries(listed, mcp=True)) == BRIDGE_TOOLS
        else:
            assert listed.status_code in {401, 404, 405}
