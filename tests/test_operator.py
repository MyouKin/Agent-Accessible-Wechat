import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient


source = Path(__file__).parents[1] / "docker" / "operator" / "bridge_operator.py"
spec = importlib.util.spec_from_file_location("bridge_operator", source)
operator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(operator)
sessions_spec = importlib.util.spec_from_file_location("wechat_sessions", source.with_name("wechat_sessions.py"))
sessions = importlib.util.module_from_spec(sessions_spec)
sessions_spec.loader.exec_module(sessions)
TOKEN = "test-operator-token-" + "a" * 24


def test_operator_requires_loopback_and_authenticates_sensitive_routes():
    async def route(_request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[
        Route("/health", route), Route("/login", route), Route("/login.js", route),
        Route("/login/state", route), Route("/login/screen", route),
        Route("/login/confirm", route, methods=["POST"]), Route("/tools", route),
        Route("/tools/{name}", route, methods=["POST"]),
        Route("/sessions", route), Route("/sessions/open", route, methods=["POST"]),
    ])
    bearer = {"Authorization": f"Bearer {TOKEN}"}
    with TestClient(operator.OperatorAccess(app, TOKEN), base_url="http://127.0.0.1:8765") as client:
        for path in ("/health", "/login", "/login.js"):
            response = client.get(path)
            assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        for path in ("/login/state", "/login/screen"):
            assert client.get(path).status_code == 401
            assert client.get(path, headers=bearer).status_code == 200
        assert client.post("/login/confirm").status_code == 401
        assert client.get("/tools").status_code == 401
        assert client.get("/tools", headers=bearer).status_code == 200
        assert client.post("/tools/wechat_status").status_code == 401
        assert client.post("/tools/wechat_status", headers={**bearer, "Origin": "http://127.0.0.1:8765"}).status_code == 200
        assert client.post("/tools/wechat_status", headers={**bearer, "Origin": "http://example.com"}).status_code == 403
        assert client.post("/mcp", headers=bearer).status_code == 404
        assert client.get("/sessions").status_code == 401
        assert client.get("/sessions", headers=bearer).status_code == 200
        assert client.post("/sessions/open").status_code == 401
        assert client.post("/sessions/open", headers=bearer).status_code == 200
        assert client.get("/health", headers={"Host": "127.0.0.1.example.com"}).status_code == 403
        assert client.post(f"/mcp/{TOKEN}").status_code == 401


class FakeWechat:
    def send_files(self, chat, paths, caption):
        return {"chat": chat, "paths": paths, "caption": caption}


def test_attachment_tools_only_materialize_or_send_inside_operator_directories(tmp_path):
    inbox, outbox = tmp_path / "inbox", tmp_path / "outbox"
    inbox.mkdir()
    outbox.mkdir()
    file = inbox / "image.png"
    file.touch()
    foreign = tmp_path / "private.dat"
    foreign.touch()
    symlink = inbox / "outside"
    symlink.symlink_to(foreign)
    wx = FakeWechat()
    operator.restrict_attachments(wx, inbox, outbox)
    assert wx._materialize(str(file), None, None, None) == str(file.resolve())
    with pytest.raises(ValueError):
        wx._materialize(str(symlink), None, None, None)
    with pytest.raises(ValueError):
        wx._materialize(None, "http://example.test/image.png", None, None)
    for unsafe in ("../../escape", "/tmp/escape", "..", "escape\\file", ""):
        with pytest.raises(ValueError):
            wx._materialize(None, None, "test", unsafe)
    result = wx._materialize(None, None, "Zmlyc3Q=", "photo.png")
    second = wx._materialize(None, None, "c2Vjb25k", "photo.png")
    assert result != second and Path(result).name == Path(second).name == "photo.png"
    assert Path(result).read_bytes() == b"first" and Path(second).read_bytes() == b"second"
    assert wx.send_files("test", [str(file), result])["paths"] == [str(file.resolve()), result]
    assert "error" in wx.send_files("test", [str(foreign)])
    assert "error" in wx.send_files("test", [str(symlink)])


def test_session_search_requires_one_exact_result_and_proves_its_kind():
    group, other, history = object(), object(), object()
    rows = [("完整群名", "group", group), ("完整群名记录", "group", other), ("完整群名", None, history)]
    assert sessions.select_result(rows, "完整群名", "unknown") == ("group", group)
    assert sessions.select_result(rows, "完整群名", "group") == ("group", group)
    assert sessions.select_result([("联系人", "private", other)], "联系人", "unknown") == ("private", other)
    with pytest.raises(sessions.SessionSearchError):
        sessions.select_result(rows, "完整群名", "private")
    with pytest.raises(sessions.SessionSearchError):
        sessions.select_result([("完整群名", "unknown", other)], "完整群名", "unknown")
    for competing_kind in ("group", "private", "unknown"):
        with pytest.raises(sessions.SessionSearchError):
            sessions.select_result([*rows, ("完整群名", competing_kind, other)], "完整群名", "group")


def test_session_inventory_waits_for_login_before_inspecting_sidebar(monkeypatch):
    monkeypatch.setattr(sessions, "client_version", lambda: None)
    navigator = sessions.SessionNavigator(SimpleNamespace(main_frame=lambda: None), None, None)
    with pytest.raises(sessions.SessionSearchError, match="signed out|loading"):
        navigator.inventory()


def test_session_inventory_preserves_duplicate_rows_without_guessing_kind(monkeypatch):
    monkeypatch.setattr(sessions, "client_version", lambda: None)
    rows = [
        SimpleNamespace(name="同名\n预览", role="list item", states={"selected"}),
        SimpleNamespace(name="同名\n预览", role="list item", states=set()),
        SimpleNamespace(name="某联系人\n预览", role="list item", states=set()),
    ]
    sidebar = SimpleNamespace(name="Chats", childCount=len(rows), getChildAtIndex=rows.__getitem__)
    wx = SimpleNamespace(main_frame=object, session_list=lambda: sidebar,
                         current_chat=lambda: {"name": "同名", "is_group": True})
    a11y = SimpleNamespace(name=lambda node: node.name, role=lambda node: node.role,
                           states=lambda node: node.states, is_showing=lambda _node: True)
    result = sessions.SessionNavigator(wx, a11y, None).inventory()
    assert result == {
        "sessions": [
            {"name": "同名", "kind": "group", "native_id": None},
            {"name": "同名", "kind": "unknown", "native_id": None},
            {"name": "某联系人", "kind": "unknown", "native_id": None},
        ],
        "complete": False,
    }
