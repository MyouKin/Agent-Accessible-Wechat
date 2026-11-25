"""Synthetic observations exercise sequence semantics independently of WeChat."""

import asyncio

from bridge.desktop import SendResult, SendWitness
from bridge.models import Message, Snapshot, message_key
from bridge.storage import Store
from bridge.sync import SequenceMatch, Synchronizer, match_tail


def test_overlapping_tail_identifies_multiple_new_rows():
    assert match_tail(tuple("ABCDE"), tuple("CDEFG")) == SequenceMatch("matched", 3)


def test_duplicate_rows_remain_separate_occurrences():
    assert match_tail(tuple("XA"), tuple("AAA")) == SequenceMatch("matched", 1)
    assert match_tail(tuple("XAAB"), tuple("AABAAB")) == SequenceMatch("matched", 3)


def test_history_and_unanchored_window():
    assert match_tail(tuple("ABCDE"), tuple("BC")) == SequenceMatch("older")
    assert match_tail(tuple("AB"), tuple("XBYBZ")) == SequenceMatch("ambiguous")
    assert match_tail(tuple("ABCDE"), tuple("EFG")) == SequenceMatch("matched", 1)
    assert match_tail(tuple("ABCDE"), tuple("JK")) == SequenceMatch("unanchored")


def snapshot(text, *, chat="test", at_tail=True, ready=True):
    return Snapshot(chat, tuple(Message(chat=chat, text=letter, is_self=letter == "S")
                                for letter in text), at_tail, ready)


class SyntheticSource:
    def __init__(self, current):
        self.current = current
        self.recovered = None
        self.attempts = 0

    async def start(self):
        pass

    async def stop(self):
        pass

    async def get_status(self):
        return {"source": "synthetic", "state": "ready"}

    async def wait_for_change(self, timeout=None):
        await asyncio.sleep(timeout or 0.5)

    async def read_snapshot(self, *, capture_media=True):
        return self.current

    async def capture_messages(self, snapshot, indices):
        return tuple(snapshot.messages[i] for i in indices)

    async def recover(self, previous, current):
        self.attempts += 1
        return self.recovered


async def test_restart_and_off_target_do_not_reset_anchors(tmp_path):
    store = Store(tmp_path, "test")
    source = SyntheticSource(snapshot("ABC"))
    sync = Synchronizer(source, store, 0.5)
    await sync.step()
    assert store.messages(limit=10) == []
    source.current = snapshot("X", chat="other")
    await sync.step()
    source.current = snapshot("AB", at_tail=False)
    await sync.step()
    store.close()
    store = Store(tmp_path, "test")
    sync = Synchronizer(source, store, 0.5)
    source.current = snapshot("BCCS")
    await sync.step()
    rows = store.messages(limit=10)
    assert [message["text"] for message in rows] == ["C", "S"]
    assert rows[-1]["is_self"] is True
    assert all(row["timestamp"] is None for row in rows)
    store.close()


async def test_recovery_and_a_durable_gap(tmp_path):
    store = Store(tmp_path, "test")
    source = SyntheticSource(snapshot("ABC"))
    sync = Synchronizer(source, store, 0.5)
    await sync.step()
    source.current = snapshot("EF")
    source.recovered = snapshot("BCDEF")
    await sync.step()
    assert [message["text"] for message in store.messages(limit=10)] == list("DEF")

    source.current = snapshot("YZ")
    source.recovered = None
    await sync.step()
    assert store.sync_status()["state"] == "gap"
    assert store.sync_status()["last_gap"]["reason"] == "unanchored"
    source.current = snapshot("ZQ")
    await sync.step()
    assert [message["text"] for message in store.messages(limit=10)] == list("DEFQ")
    assert store.sync_status()["last_gap"]["reason"] == "unanchored"
    assert source.attempts == 2
    store.close()


async def test_identical_phone_and_model_bodies_get_distinct_authors(tmp_path):
    store = Store(tmp_path, "test")
    old, phone, model = snapshot("ASS").messages
    source = SyntheticSource(Snapshot("test", (old,), True))
    sync = Synchronizer(source, store, 0.5)
    await sync.step()
    draft = store.create_draft("S", "model-1", "http", False, author_kind="llm")
    assert store.claim_draft(draft["id"])
    before = Snapshot("test", (old, phone), True)
    after = Snapshot("test", (old, phone, model), True)
    store.checkpoint_submission(draft["id"], tuple(map(message_key, before.messages)))
    sync.commit_send(draft, SendResult("sent", witness=SendWitness(before, after, 2),
                                     submission_attempted=True))
    source.current = after
    await sync.step()
    rows = store.messages(limit=10)
    assert [(row["text"], row["is_self"], row["author_kind"]) for row in rows] == [
        ("S", True, "human"), ("S", True, "llm"),
    ]
    assert rows[0]["author_source"] == "same_account_outside_bridge"
    assert rows[1]["outbound_draft_id"] == draft["id"]
    assert store.get_draft(draft["id"])["status"] == "sent"
    store.close()


async def test_restart_classifies_unverified_model_send_as_unknown(tmp_path):
    store = Store(tmp_path, "test")
    source = SyntheticSource(snapshot("ABC"))
    sync = Synchronizer(source, store, 0.5)
    await sync.step()
    draft = store.create_draft("S", "crash-1", "http", False, author_kind="llm")
    store.claim_draft(draft["id"])
    store.checkpoint_submission(draft["id"], tuple(map(message_key, source.current.messages)))
    store.close()  # The send could have occurred by this point.
    store = Store(tmp_path, "test")
    sync = Synchronizer(source, store, 0.5)
    source.current = snapshot("BCSQ")
    await sync.step()
    assert store.get_draft(draft["id"])["status"] == "uncertain"
    rows = store.messages(limit=10)
    assert [(row["text"], row["author_kind"]) for row in rows] == [("S", "unknown"), ("Q", "human")]
    assert rows[0]["author_source"] == "unverified_bridge_submission"
    assert rows[0]["outbound_draft_id"] is None
    assert not store.unverified_model_submissions()
    store.close()
