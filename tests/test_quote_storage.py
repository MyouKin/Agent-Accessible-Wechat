import json
from contextlib import closing
from dataclasses import asdict

from bridge.models import Message
from bridge.storage import Store


def quote_reply(chat: str, body: str) -> Message:
    return Message(
        chat=chat, type="text", text=f"我回复这一条\nQuote 本账号's message: {body}",
        is_self=True, author_kind="human", author_source="same_account_outside_bridge",
    )


def test_quote_uses_a_confirmed_earlier_agent_row_in_the_configured_chat(tmp_path):
    body = "完整模型回复\n保留第二行"
    with closing(Store(tmp_path, "other")) as other:
        other.save_sync("tracking", messages=(Message(chat="other", type="text", text=body),))
    with closing(Store(tmp_path, "test")) as store:
        draft = store.create_draft(body, "model", "http", False, author_kind="llm")
        assert store.claim_draft(draft["id"])
        store.save_sync(
            "tracking", finished_draft=(draft["id"], None), messages=(
                Message(chat="test", type="text", text=body.replace("\n", "\u2029"), is_self=True,
                        author_kind="llm", author_source="confirmed_bridge_submission", outbound_draft_id=draft["id"]),
                quote_reply("test", body),
                Message(chat="test", type="text", text=body, author_kind="human"),
            ),
        )
        quoted, = store.messages(after_id=2, limit=1, for_model=True)
        assert quoted["reply_text"] == "我回复这一条"
        assert quoted["quote"] == {"text": body, "author_label": "本账号", "author_kind": "llm", "message_id": 2}


def test_legacy_audited_quoted_model_row_matches_its_original_reply_body(tmp_path):
    body = "历史模型回复"
    with closing(Store(tmp_path, "test")) as store:
        legacy = Message(
            chat="test", type="text", text=f"{body}\nQuote 群成员's message: 更早的原文",
            author_kind="llm", author_source="verified_live_test_audit",
        )
        with store.transaction() as db:
            db.execute("INSERT INTO messages(chat,record) VALUES (?,?)",
                       ("test", json.dumps(asdict(legacy), default=lambda dt: dt.isoformat())))
        store.save_sync("tracking", messages=(quote_reply("test", body),))
        quoted, = store.messages(for_model=True, limit=20)
        assert quoted["quote"]["message_id"] == 1
        assert quoted["quote"]["author_kind"] == "llm"
        assert quoted["quote"]["text"] == body


def test_ambiguous_earlier_exact_matches_and_truncated_quotes_stay_unresolved(tmp_path):
    body = "相同内容完整正文"
    with closing(Store(tmp_path, "test")) as store:
        store.save_sync("tracking", messages=(
            Message(chat="test", type="text", text=body, author_kind="llm", author_source="verified_live_test_audit"),
            Message(chat="test", type="text", text=body, author_kind="human"),
            quote_reply("test", body),
            quote_reply("test", body[:-2] + "…"),
        ))
        rows = store.messages(after_id=2, limit=5)
        for index, quote in enumerate((row["quote"] for row in rows)):
            assert "author_kind" not in quote
            assert "message_id" not in quote
            assert quote["text"] == (body if index == 0 else body[:-2] + "…")


def test_valid_human_quote_does_not_trust_a_prepopulated_llm_attribution(tmp_path):
    body = "人工发送内容"
    with closing(Store(tmp_path, "test")) as store:
        store.save_sync("tracking", messages=(
            Message(chat="test", type="text", text=body, author_kind="human"),
            Message(chat="test", type="text", text=f"确实如此\nQuote 群成员's message: {body}",
                    reply_text="确实如此", quote={"text": body, "author_label": "群成员",
                                                "author_kind": "llm", "message_id": 666}),
        ))
        quoted, = store.messages(after_id=1, limit=1)
        assert quoted["quote"] == {"text": body, "author_label": "群成员", "author_kind": "human", "message_id": 1}


def test_quote_rejects_an_unrelated_sent_llm_receipt(tmp_path):
    body = "有人发送了相同内容"
    with closing(Store(tmp_path, "test")) as store:
        draft = store.create_draft("实际模型草稿", "model", "http", False, author_kind="llm")
        assert store.claim_draft(draft["id"])
        store.save_sync(
            "tracking", finished_draft=(draft["id"], None), messages=(
                Message(chat="test", type="text", text=body, author_kind="llm",
                        author_source="confirmed_bridge_submission", outbound_draft_id=draft["id"]),
                quote_reply("test", body),
            ),
        )
        quoted, = store.messages(after_id=1, limit=1)
        assert quoted["quote"] == {"text": body, "author_label": "本账号"}


def test_media_album_indices_preserve_old_first_path_and_honor_containment(tmp_path):
    with closing(Store(tmp_path, "test")) as store:
        first = store.media_dir / "first.png"
        second = store.media_dir / "second.png"
        escaped = tmp_path / "outside.png"
        for path in (first, second, escaped):
            path.write_bytes(b"media fixture")
        (store.media_dir / "link.png").symlink_to(escaped)
        store.save_sync("tracking", messages=(
            Message(chat="test", type="image", media_path="first.png",
                    metadata={"media_paths": ["first.png", "second.png", "../outside.png",
                                              str(escaped), "link.png", None, "missing.png"]}),
            Message(chat="test", type="image", media_path="second.png"),
        ))
        assert store.media(1) == first
        assert store.media(1, 1) == second
        assert all(store.media(1, index) is None for index in (-1, True, 2, 3, 4, 5, 6, 7))
        assert store.media(2, 0) == second
        assert store.media(2, 1) is None
