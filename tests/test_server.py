from fastapi.testclient import TestClient

from bridge.config import Settings
from bridge.desktop import SendResult
from bridge.models import Message, message_key
from bridge.server import create_app
from bridge.storage import Store

from test_sync import SyntheticSource, snapshot

TOKEN = "a" * 30
MCP_TOKEN = "m" * 30
AUTH = {"Authorization": f"Bearer {TOKEN}"}
MCP_AUTH = {
    "Authorization": f"Bearer {MCP_TOKEN}",
    "Accept": "application/json, text/event-stream",
}


class SyntheticSender:
    def __init__(self):
        self.calls = []

    async def send(self, text, *, quote=None, before_submit=None):
        self.calls.append(text if quote is None else (text, quote))
        return SendResult("sent")


def create_client(tmp_path, *, mcp=False, send_mode="manual"):
    sender = SyntheticSender()
    app = create_app(
        Settings(chat="test", data_dir=str(tmp_path), mcp_enabled=mcp, send_mode=send_mode),
        TOKEN, MCP_TOKEN, source=SyntheticSource(snapshot("ABC")), sender=sender,
    )
    return TestClient(app, base_url="http://127.0.0.1:8765"), sender


def test_manual_confirmation_and_authorization(tmp_path):
    client, sender = create_client(tmp_path)
    with client:
        assert client.get("/health").json() == {"ok": True}
        assert client.get("/status").status_code == 401
        assert client.get("/status", headers=AUTH).json()["chat"] == "test"
        assert client.get("/messages/new", headers=AUTH).json() == {"messages": []}
        assert client.get("/messages/search?q=测试", headers=AUTH).json() == {"messages": []}
        assert client.get("/media/12", headers=AUTH).status_code == 404

        body = {"text": "中文🙂\n再来一行", "request_id": "test-1"}
        assert client.post("/drafts", json={**body, "chat": "other"}, headers=AUTH).status_code == 409
        assert client.post("/drafts", json={**body, "auto_send": True}, headers=AUTH).status_code == 409
        response = client.post("/drafts", json=body, headers=AUTH).json()
        assert response["status"] == "pending"
        assert not sender.calls
        draft_id = response["id"]
        assert client.post("/drafts", json=body, headers=AUTH).json()["id"] == draft_id
        assert client.get(f"/drafts/{draft_id}", headers=AUTH).json()["status"] == "pending"
        for _ in range(2):
            assert client.post(f"/drafts/{draft_id}/confirm", headers=AUTH).json()["status"] == "sent"
        assert sender.calls == [body["text"]]


def test_idempotent_auto_and_memory(tmp_path):
    client, sender = create_client(tmp_path, send_mode="auto")
    with client:
        body = {"text": "test", "auto_send": True, "request_id": "auto-1"}
        assert client.post("/drafts", json=body, headers=AUTH).json()["status"] == "sent"
        assert client.post("/drafts", json=body, headers=AUTH).json()["status"] == "sent"
        assert sender.calls == ["test"]
        body = {"text": "散步", "kind": "memory", "request_id": "memory-1"}
        for _ in range(2):
            assert client.post("/memories", json=body, headers=AUTH).json()["id"] == 1
        assert client.get("/memories?q=散", headers=AUTH).json()["memories"][0]["text"] == "散步"
        assert not client.get("/memories?q=夜", headers=AUTH).json()["memories"]


def test_mcp_tools_with_separate_read_draft_token(tmp_path):
    client, sender = create_client(tmp_path, mcp=True, send_mode="auto")
    with client:
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        assert client.post("/mcp", json=request, headers=AUTH).status_code == 401
        response = client.post("/mcp", json=request, headers=MCP_AUTH)
        assert response.status_code == 200
        assert {tool["name"] for tool in response.json()["result"]["tools"]} == {
            "get_status", "get_recent_messages", "get_new_messages", "search_messages",
            "get_media", "draft_message",
        }
        request["method"] = "tools/call"
        request["params"] = {
            "name": "draft_message",
            "arguments": {"text": "synthetic MCP draft", "quote_message_id": 2},
        }
        response = client.post("/mcp", json=request, headers=MCP_AUTH)
        assert response.status_code == 200
        assert response.json()["result"]["structuredContent"]["status"] == "pending"
        assert response.json()["result"]["structuredContent"]["quote_message_id"] == 2
        assert client.post("/drafts/1/confirm", headers=MCP_AUTH).status_code == 401
        assert not sender.calls


def test_quote_confirm_passes_stored_sequence_and_keeps_draft_idempotent(tmp_path):
    first = Message(chat="test", type="text", text="待引用消息", is_self=False)
    following = Message(chat="test", type="text", text="之后的消息")
    store = Store(tmp_path, "test")
    store.save_sync("tracking", messages=(first, following))
    store.close()

    client, sender = create_client(tmp_path)
    with client:
        body = {"text": "回复", "request_id": "quote-1", "quote_message_id": 1}
        draft = client.post("/drafts", json=body, headers=AUTH).json()
        assert draft["status"] == "pending"
        assert draft["quote_message_id"] == 1
        assert not sender.calls
        assert client.post("/drafts", json={**body, "quote_message_id": 2}, headers=AUTH).status_code == 409
        finished = client.post(f"/drafts/{draft['id']}/confirm", headers=AUTH).json()
        assert finished["status"] == "sent"
        assert len(sender.calls) == 1
        text, quote = sender.calls[0]
        assert text == "回复" and quote.text == first.text
        assert quote.keys == (message_key(first), message_key(following))
        invalid = client.post("/drafts", json={"text": "再试", "quote_message_id": 9000}, headers=AUTH).json()
        unavailable = client.post(f"/drafts/{invalid['id']}/confirm", headers=AUTH).json()
        assert unavailable["status"] == "failed"
        assert len(sender.calls) == 1


def test_model_projection_filters_before_limit_and_keeps_human_account_text(tmp_path):
    store = Store(tmp_path, "test")
    try:
        store.save_sync("tracking", messages=(
            Message(chat="test", text="同文 手机", is_self=True, author_kind="human",
                    author_source="same_account_outside_bridge"),
            Message(chat="test", text="同文 模型", is_self=True, author_kind="llm"),
            Message(chat="test", text="同文 待核验", author_source="unverified_bridge_submission"),
            Message(chat="test", text="同文 历史未知", is_self=True),
        ))
        assert [row["id"] for row in store.messages(limit=10)] == [1, 2, 3, 4]
        assert [row["id"] for row in store.messages(limit=1, for_model=True)] == [1]
        assert [row["id"] for row in store.messages(limit=1, for_model=True, q="同文")] == [1]
    finally:
        store.close()


def test_model_projection_keeps_human_quote_even_when_agent_message_is_filtered(tmp_path):
    store = Store(tmp_path, "test")
    model = "旧回复 @阿桥\n第二行"
    raw = f"我说的是这段\nQuote 本账号's message: {model}"
    try:
        store.save_sync("tracking", messages=(
            Message(chat="test", type="text", text=model, is_self=True, author_kind="llm"),
            Message(chat="test", type="text", text=raw, is_self=True, author_kind="human",
                    author_source="same_account_outside_bridge"),
        ))
        assert [row["id"] for row in store.messages(limit=10)] == [1, 2]
        human, = store.messages(limit=10, for_model=True, q="旧回复")
        assert human["id"] == 2
        assert human["text"] == raw
        assert human["reply_text"] == "我说的是这段"
        assert human["quote"] == {"author_label": "本账号", "text": model}
    finally:
        store.close()


def test_album_media_index_preserves_default_first_image(tmp_path):
    store = Store(tmp_path, "test")
    (store.media_dir / "first.png").write_bytes(b"first")
    (store.media_dir / "second.png").write_bytes(b"second")
    store.save_sync("tracking", messages=(
        Message(chat="test", type="image", media_path="first.png",
                metadata={"media_paths": ["first.png", "second.png"]}),
    ))
    store.close()

    client, _ = create_client(tmp_path)
    with client:
        assert client.get("/media/1", headers=AUTH).content == b"first"
        assert client.get("/media/1?index=1", headers=AUTH).content == b"second"
        assert client.get("/media/1?index=2", headers=AUTH).status_code == 422
