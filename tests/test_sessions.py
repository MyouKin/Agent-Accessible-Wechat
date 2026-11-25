from time import monotonic, sleep

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from bridge.config import Settings
from bridge.models import Message
from bridge.server import create_app
from bridge.sessions import Session, SessionCatalog
from bridge.storage import Store
from test_server import AUTH, TOKEN, SyntheticSender
from test_sync import SyntheticSource, snapshot


class DesktopSource(SyntheticSource):
    def __init__(self, desktop):
        super().__init__(snapshot("", chat=""))
        self.desktop = desktop
        self.target = ("", None)

    async def set_target(self, chat, kind=None):
        self.target = (chat, kind)

    async def prepare_target(self):
        return await self.read_snapshot()

    async def read_snapshot(self, *, capture_media=True):
        name, kind = self.target
        return snapshot(self.desktop["messages"].get(name, "AB"), chat=name, ready=self.desktop["open"] == (name, kind))

    async def get_status(self):
        result = await self.read_snapshot()
        return {"ready": result.ready, "state": "ready" if result.ready else "chat_mismatch"}


class DesktopSender(SyntheticSender):
    def set_target(self, chat):
        self.chat = chat


@pytest.fixture
def desktop(monkeypatch):
    state = {
        "open": None,
        "catalog": [Session(name="项目群", kind="unknown"), Session(name="联系人", kind="private")],
        "kind": {"项目群": "group", "联系人": "private"},
        "calls": [],
        "messages": {},
    }

    async def list_sessions(client, token):
        if state.get("signed_out"):
            raise HTTPException(409, "Waiting for WeChat login")
        return SessionCatalog(sessions=state["catalog"], complete=False)

    async def open_session(client, token, target):
        state["calls"].append(target.name)
        observed = Session(name=target.name, kind=state["kind"][target.name])
        if target.kind not in ("unknown", observed.kind):
            raise HTTPException(409, "Inconsistent conversation")
        if not state.get("fail_prepare"):
            state["open"] = (observed.name, observed.kind)
        return observed

    monkeypatch.setattr("bridge.server.list_sessions", list_sessions)
    monkeypatch.setattr("bridge.server.open_session", open_session)
    return state


def client_for(path, desktop):
    source = DesktopSource(desktop)
    sender = DesktopSender()
    app = create_app(
        Settings(data_dir=str(path), poll_interval=.1), TOKEN, source=source, sender=sender,
        wechat_tools_socket="/unused-fake-wechat-worker.sock", wechat_tools_token="w" * 30,
    )
    return TestClient(app, base_url="http://127.0.0.1:8765"), sender


def test_switch_and_restart_restore_one_verified_conversation(tmp_path, desktop):
    client, sender = client_for(tmp_path, desktop)
    with client:
        assert client.get("/sessions").status_code == 401
        initial = client.get("/sessions", headers=AUTH).json()
        assert initial["active"] is None and initial["complete"] is False
        group, peer = initial["sessions"]
        assert group["native_id"] is None and peer["kind"] == "private"
        selection = client.put("/sessions/active", json={"key": group["key"]}, headers=AUTH).json()["active"]
        assert selection["kind"] == "group" and selection["name"] == sender.chat == "项目群"
        assert selection["key"] != group["key"]
        scoped = {"scope": "conversation", "session_key": selection["key"]}
        assert client.get("/tools", params=scoped, headers=AUTH).status_code == 200
        deadline = monotonic() + 3
        while monotonic() < deadline:
            status = client.get("/status", headers=AUTH).json()
            if status["sync"]["state"] == "tracking":
                break
            sleep(.02)
        assert status["sync"]["state"] == "tracking"
        desktop["messages"]["项目群"] = "ABC"
        wait = {"session_key": selection["key"], "timeout": 2}
        rows = client.get("/messages/wait", params=wait, headers=AUTH).json()["messages"]
        assert [row["text"] for row in rows] == ["C"]
        group_draft = client.post("/drafts", json={
            "text": "only in group", "chat": "项目群", "session_key": selection["key"],
        }, headers=AUTH).json()
        active = client.put("/sessions/active", json={"key": peer["key"]}, headers=AUTH).json()["active"]
        assert client.get("/status", headers=AUTH).json()["active_session"] == active
        assert sender.chat == "联系人"
        assert client.get("/tools", params=scoped, headers=AUTH).status_code == 409
        assert client.get(f"/drafts/{group_draft['id']}", headers=AUTH).status_code == 404
        assert client.get("/messages", headers=AUTH).json()["messages"] == []
        assert client.post("/drafts", json={"text": "late", "session_key": selection["key"]}, headers=AUTH).status_code == 409
        assert not sender.calls

    desktop["open"] = None
    client, sender = client_for(tmp_path, desktop)
    with client:
        deadline = monotonic() + 3
        while monotonic() < deadline:
            restored = client.get("/status", headers=AUTH).json()
            if restored["source"]["ready"]:
                break
            sleep(.02)
        assert restored["source"]["ready"] is True
        assert restored["active_session"] == active
        assert sender.chat == "联系人"
        assert desktop["calls"] == ["项目群", "联系人", "联系人"]


def test_duplicate_or_unverified_selection_does_not_replace_saved_target(tmp_path, desktop):
    client, _ = client_for(tmp_path, desktop)
    with client:
        group, peer = client.get("/sessions", headers=AUTH).json()["sessions"]
        active = client.put("/sessions/active", json={"key": group["key"]}, headers=AUTH).json()["active"]
        desktop["catalog"].append(desktop["catalog"][1])
        assert client.put("/sessions/active", json={"key": peer["key"]}, headers=AUTH).status_code == 409
        assert desktop["calls"] == ["项目群"]
        desktop["catalog"].pop()
        desktop["fail_prepare"] = True
        assert client.put("/sessions/active", json={"key": peer["key"]}, headers=AUTH).status_code == 409
        assert client.get("/status", headers=AUTH).json()["active_session"] == active


def test_saved_selection_waits_for_login_without_changing_partitions(tmp_path, desktop):
    saved = Session(name="联系人", kind="private")
    store = Store(tmp_path, "")
    store.select_session(saved.model_dump())
    store.close()
    desktop["signed_out"] = True
    client, sender = client_for(tmp_path, desktop)
    with client:
        status = client.get("/status", headers=AUTH).json()
        assert status["session_restore"] == "waiting"
        assert status["active_session"] is None and status["source"]["ready"] is False
        assert client.get("/sessions", headers=AUTH).status_code == 409
        assert client.post("/drafts", headers=AUTH, json={"text": "not during login"}).status_code == 409
        assert desktop["calls"] == sender.calls == []


def test_private_history_is_separate_from_a_group_with_the_same_title(tmp_path):
    group = Session(name="同名", kind="group")
    peer = Session(name="同名", kind="private")
    store = Store(tmp_path, group.name)
    try:
        store.save_sync("tracking", anchors=("g",), messages=(Message(chat=group.name, text="group"),))
        store.bind_target(peer.name, peer.partition)
        assert store.anchors() is None and store.messages(limit=10) == []
        store.save_sync("tracking", anchors=("p",), messages=(Message(chat=peer.name, text="private"),))
        assert [row["text"] for row in store.messages(limit=10)] == ["private"]
        store.bind_target(group.name, group.partition)
        assert store.anchors() == ("g",)
        assert [row["text"] for row in store.messages(limit=10)] == ["group"]
    finally:
        store.close()
