"""Synthetic desktop states exercise the send interlocks without sending to WeChat."""

import asyncio
from contextlib import asynccontextmanager

from bridge import desktop
from bridge.models import Message, QuoteTarget, Snapshot, message_key


class Composer:
    def __init__(self, text):
        self.draft = text
        self.current = ""
        self.submitted = False
        self.before_rows = ("anchor",)
        self.after_rows = None

    async def read_snapshot(self, *, capture_media=True):
        rows = self.before_rows
        if self.submitted:
            rows = self.before_rows + (self.draft,) if self.after_rows is None else self.after_rows
        return Snapshot("test", tuple(Message(chat="test", text=text) for text in rows), True)

    async def read_input(self):
        return self.current

    async def focus_input(self):
        return True

    async def input_focused(self):
        return True


def replace_desktop(monkeypatch, composer):
    keys = []

    @asynccontextmanager
    async def locked():
        yield

    @asynccontextmanager
    async def clipboard(text):
        assert text == composer.draft
        yield

    async def command(*args):
        if args[:2] == ("xdotool", "key"):
            keys.append(args[-1])
            if args[-1] == "ctrl+v":
                composer.current = composer.draft.replace("\n", "\u2029")
            elif args[-1] == "Return":
                composer.current = ""
                composer.submitted = True

    async def active_target(self):
        return "verified"

    monkeypatch.setattr(desktop, "pause_vnc_input", locked)
    monkeypatch.setattr(desktop, "clipboard_text", clipboard)
    monkeypatch.setattr(desktop, "command", command)
    monkeypatch.setattr(desktop.MessageSender, "active_target", active_target)
    return keys


async def test_sender_checks_pasted_multiline_and_new_row(monkeypatch):
    source = Composer("中文🙂\n第二行")
    keys = replace_desktop(monkeypatch, source)
    sender = desktop.MessageSender("test", source, asyncio.Lock(), "Return")
    outcome = await sender.send(source.draft)
    assert outcome.state == "sent"
    assert outcome.submission_attempted is True
    assert outcome.witness.after.messages[outcome.witness.after_index].text == source.draft
    assert keys == ["ctrl+v", "Return"]


async def test_sender_does_not_touch_an_existing_manual_draft(monkeypatch):
    source = Composer("要发送的消息")
    source.current = "人工编辑中的草稿"
    keys = replace_desktop(monkeypatch, source)
    sender = desktop.MessageSender("test", source, asyncio.Lock(), "Return")
    assert (await sender.send(source.draft)).state == "failed"
    assert keys == []
    assert source.current == "人工编辑中的草稿"


def test_quote_requires_a_unique_visible_row_and_ordered_suffix():
    target = Message(chat="test", text="wanted")
    next_row = Message(chat="test", text="next")
    quote = QuoteTarget("wanted", (message_key(target), message_key(next_row)))
    assert desktop.visible_quote_index(Snapshot("test", (target, next_row), True), quote) == 0
    assert desktop.visible_quote_index(Snapshot("test", (target, next_row, target), True), quote) is None
    assert desktop.visible_quote_index(Snapshot("test", (next_row, target), True), quote) is None


def test_quoted_reply_accepts_only_the_native_label_with_the_exact_original_body():
    quote = QuoteTarget("原文", ("key",))
    assert desktop.matches_sent_text("回应\nQuote member's message: 原文", "回应", quote)
    assert not desktop.matches_sent_text("回应", "回应", quote)
    assert not desktop.matches_sent_text("回应\nQuote member's message: 另一个原文", "回应", quote)
    assert not desktop.matches_sent_text("回应 plus\nQuote member's message: 原文", "回应", quote)


async def test_failed_native_quote_never_degrades_to_plain_send(monkeypatch):
    class UnsupportedQuote(Composer):
        async def quote_clear(self):
            return True

        async def select_quote(self, snapshot, index, *, allow_own=False):
            assert allow_own is False
            return False

    source = UnsupportedQuote("reply")
    keys = replace_desktop(monkeypatch, source)
    quote = QuoteTarget("anchor", (message_key((await source.read_snapshot()).messages[0]),))
    sender = desktop.MessageSender("test", source, asyncio.Lock(), "Return")
    assert (await sender.send(source.draft, quote=quote)).state == "failed"
    assert keys == []


async def test_witness_distinguishes_one_repeated_occurrence_from_earlier_phone_text(monkeypatch):
    source = Composer("Repeated text")
    source.before_rows = ("Distinct context", source.draft)
    keys = replace_desktop(monkeypatch, source)
    sender = desktop.MessageSender("test", source, asyncio.Lock(), "Return")
    checkpoints = []

    async def checkpoint(snapshot):
        assert keys == ["ctrl+v"]
        assert source.current == source.draft
        checkpoints.append(snapshot)

    outcome = await sender.send(source.draft, before_submit=checkpoint)
    assert outcome.state == "sent"
    assert outcome.witness.before is checkpoints[0]
    assert outcome.witness.after_index == 2
    assert outcome.witness.after_keys == outcome.witness.before_keys + (outcome.witness.before_keys[-1],)
    assert keys == ["ctrl+v", "Return"]


async def test_duplicate_new_rows_produce_no_occurrence_witness(monkeypatch):
    source = Composer("Repeated text")
    source.after_rows = ("anchor", source.draft, source.draft)
    keys = replace_desktop(monkeypatch, source)
    sender = desktop.MessageSender("test", source, asyncio.Lock(), "Return")

    outcome = await sender.send(source.draft)
    assert outcome.state == "uncertain"
    assert outcome.submission_attempted is True
    assert outcome.witness is None
    assert keys == ["ctrl+v", "Return"]


async def test_witness_declines_repeated_anchor_after_viewport_changes(monkeypatch):
    source = Composer("Reply")
    source.before_rows = ("Earlier context", "Repeated anchor")
    source.after_rows = ("Repeated anchor", "Repeated anchor", source.draft)
    replace_desktop(monkeypatch, source)
    sender = desktop.MessageSender("test", source, asyncio.Lock(), "Return")

    outcome = await sender.send(source.draft)
    assert outcome.state == "uncertain"
    assert outcome.submission_attempted is True
    assert outcome.witness is None


async def test_changed_pre_submit_rows_stop_submission(monkeypatch):
    source = Composer("Reply")
    keys = replace_desktop(monkeypatch, source)
    sender = desktop.MessageSender("test", source, asyncio.Lock(), "Return")

    async def checkpoint(_snapshot):
        source.before_rows += ("New human message",)

    outcome = await sender.send(source.draft, before_submit=checkpoint)
    assert outcome.submission_attempted is False
    assert outcome.witness is None
    assert keys == ["ctrl+v"]


async def test_checkpoint_error_stops_submission(monkeypatch):
    source = Composer("Reply")
    keys = replace_desktop(monkeypatch, source)
    sender = desktop.MessageSender("test", source, asyncio.Lock(), "Return")

    async def checkpoint(_snapshot):
        raise RuntimeError("Fixture persistence failure")

    outcome = await sender.send(source.draft, before_submit=checkpoint)
    assert outcome.submission_attempted is False
    assert outcome.witness is None
    assert keys == ["ctrl+v"]


async def test_sender_can_quote_eligible_phone_message_from_same_account(monkeypatch):
    target = Message(chat="test", text="Message from phone", is_self=True)

    class PhoneQuoteComposer(Composer):
        quoted = False

        async def read_snapshot(self, *, capture_media=True):
            messages = (target,)
            if self.submitted:
                messages += (Message(chat="test", text=self.draft + "\nQuote account's message: " + target.text),)
            return Snapshot("test", messages, at_tail=True)

        async def quote_clear(self):
            return not self.quoted or self.submitted

        async def select_quote(self, snapshot, index, *, allow_own=False):
            assert snapshot.messages[index] == target
            self.quoted = allow_own and index == 0
            return self.quoted

        async def quote_matches(self, text):
            return self.quoted and text == target.text

    source = PhoneQuoteComposer("Reply")
    keys = replace_desktop(monkeypatch, source)
    quote = QuoteTarget(target.text, (message_key(target),), allow_own=True)
    sender = desktop.MessageSender("test", source, asyncio.Lock(), "Return")
    outcome = await sender.send(source.draft, quote=quote)
    assert outcome.state == "sent"
    assert outcome.witness.after_index == 1
    assert outcome.witness.before.messages == (target,)
    assert keys == ["ctrl+v", "Return"]


async def test_verified_witness_survives_interruption_restoring_vnc(monkeypatch):
    source = Composer("Reply")
    replace_desktop(monkeypatch, source)

    @asynccontextmanager
    async def interrupted_restore():
        yield
        raise desktop.DesktopError("Fixture VNC restore failure")

    monkeypatch.setattr(desktop, "pause_vnc_input", interrupted_restore)
    sender = desktop.MessageSender("test", source, asyncio.Lock(), "Return")
    outcome = await sender.send(source.draft)
    assert outcome.state == "sent"
    assert outcome.submission_attempted is True
    assert outcome.witness is not None
