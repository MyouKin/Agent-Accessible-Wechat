import asyncio
import json
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from bridge import desktop
from bridge.accessibility import (
    AccessibilitySource, Profile, ReadError, Selector, _merge_history, _wechat_login_state, _wechat_time_label,
    parse_message, urls_in,
)
from bridge.models import Message, message_key
from tools import inspect_atspi


@dataclass
class FixtureNode:
    role: str
    name: str = ""
    description: str = ""
    text: str | None = None
    uris: tuple = ()
    children_nodes: list = field(default_factory=list)
    bounds: tuple[int, int, int, int] | None = None
    showing: bool = True
    states: frozenset[str] = frozenset({"enabled", "showing", "visible"})
    native: object = field(default_factory=object)

    @property
    def label(self):
        return self.text or self.name or self.description or None

    def child(self, index):
        return self.children_nodes[index] if index < len(self.children_nodes) else None

    def children(self):
        return iter(self.children_nodes)

    def refresh(self):
        pass


def profile_fields(**fields):
    return {
        "package_version": "fixture",
        "window": {"role": "frame"},
        "chat_header": {"path": [0], "role": "label"},
        "message_list": {"role": "list"},
        "message_row": {"role": "list item"},
        "input": {"role": "text"},
        **fields,
    }


def profile(**fields):
    return Profile.parse(profile_fields(**fields))


OBSERVED = datetime(2026, 11, 24, 8, tzinfo=timezone.utc)


def test_sender_refuses_same_title_exposed_in_two_sidebar_rows(monkeypatch):
    source = AccessibilitySource("合成测试群", profile="wechat-4.1.13.29-arm64-en")
    source._target_kind = "group"
    first = FixtureNode("list item", name="合成测试群\n首个会话")
    second = FixtureNode("list item", name="合成测试群\n另一个会话")
    sidebar = FixtureNode("list", name="Chats", children_nodes=[first, second])
    window = FixtureNode("frame", children_nodes=[sidebar], bounds=(0, 0, 800, 600))
    field = FixtureNode("text", bounds=(300, 500, 500, 100))
    monkeypatch.setattr(source, "_chat", lambda: SimpleNamespace(window=window, input=field, pid=100))
    monkeypatch.setattr(source, "_verify_chat", lambda chat: None)
    assert source._target() is None
    sidebar.children_nodes.remove(second)
    assert source._target().chat == "合成测试群"


def test_text_message_preserves_emoji_newlines_and_separate_sender():
    row = FixtureNode("list item", children_nodes=[
        FixtureNode("label", name="阿杰"),
        FixtureNode("text", text="你好 🌱\n链接 (https://example.org/a_(b))，继续"),
        FixtureNode("label", name="10:55"),
    ])
    msg, media = parse_message(row, profile(
        sender={"path": [0]}, body={"path": [1]}, timestamp={"path": [2]},
    ), "验收群", OBSERVED)
    assert msg.chat == "验收群"
    assert msg.sender == "阿杰"
    assert msg.text == row.children_nodes[1].text
    assert msg.url == "https://example.org/a_(b)"
    assert msg.observed_at == OBSERVED
    assert msg.timestamp is None
    assert msg.metadata["accessible_timestamp"] == "10:55"
    assert msg.is_self is None
    assert msg.type == "text"
    assert media is None


def test_profile_can_read_hyperlink_without_visiting_external_urls():
    row = FixtureNode("list item", children_nodes=[
        FixtureNode("link", name="聊天链接", uris=("file:///tmp/chat", "https://example.org")),
        FixtureNode("label", name="我发的"),
        FixtureNode("label", name="2026-11-24 19:30 +0800"),
    ])
    msg, media = parse_message(row, profile(
        link={"role": "link"}, own={"name": "我发的"}, timestamp={"path": [2]},
        timestamp_format="%Y-%m-%d %H:%M %z",
    ), "验收群", OBSERVED)
    assert msg.type == "link"
    assert msg.text == "聊天链接"
    assert msg.url == "https://example.org"
    assert msg.timestamp.isoformat() == "2026-11-24T19:30:00+08:00"
    assert msg.is_self is True
    assert media is None


def test_unknown_keeps_real_description_and_no_invented_sender():
    row = FixtureNode("list item", description="无法访问的附件")
    msg, media = parse_message(row, profile(), "验收群", OBSERVED)
    assert msg.type == "unknown"
    assert msg.metadata["raw_description"] == "无法访问的附件"
    assert msg.sender is None
    assert msg.timestamp is None
    assert msg.is_self is None
    assert media is None


def test_sticker_reports_unavailable_media_until_a_screenshot_exists():
    sticker = FixtureNode("image", name="表情")
    row = FixtureNode("list item", children_nodes=[sticker, FixtureNode("label", name="对方")])
    msg, media = parse_message(row, profile(
        sticker={"name": "表情"}, other={"name": "对方"},
    ), "验收群", OBSERVED)
    assert msg.type == "sticker"
    assert msg.is_self is False
    assert msg.media_path is None
    assert msg.metadata["capture_quality"] == "unavailable"
    assert media is sticker


@pytest.mark.parametrize(("label", "kind"), [
    ('[语音] 13"', "voice"), ("[文件] report.pdf", "file"), ("[视频]", "video"),
    ("[聊天记录] Topic", "chat_record"), ("[动画表情]", "sticker"),
    ("[小程序] Program", "mini_program"), ("[名片] Contact", "contact_card"),
    ("[位置] Place", "location"), ("[红包]", "money"),
    ("[链接] Page https://example.org/link", "link"),
])
def test_reported_native_label_markers_preserve_old_observation_keys(label, kind):
    row = FixtureNode("list item", name=label)
    legacy, _ = parse_message(row, profile(body={"path": []}), "fixture", OBSERVED)
    message, media = parse_message(row, profile(body={"path": []}, wechat_label_hints=True), "fixture", OBSERVED)
    assert message.type == kind and message.metadata["type_basis"] == "wechat_accessible_label"
    assert message.metadata["raw_description"] == label
    assert message.metadata["type_hint"] == kind
    assert message_key(message) == message_key(legacy)
    assert media is None  # Label information does not expose a dedicated media control.
    if kind == "link":
        assert message.url == "https://example.org/link"


@pytest.mark.parametrize(("label", "expected", "hint"), [
    ("Video", "unknown", "video"), ('13"', "unknown", "voice"), ("文件", "unknown", "file"),
    ("文件传输好了", "text", None), ("语音识别好用", "text", None),
    ("[动画表情] [just the text]", "text", None), ("[TODO] ordinary text", "text", None),
    ("[Photo]", "unknown", "image"),
])
def test_bare_ambiguous_labels_are_not_asserted_to_be_media(label, expected, hint):
    row = FixtureNode("list item", name=label, bounds=(0, 0, 500, 75))
    message, media = parse_message(row, profile(body={"path": []}, wechat_label_hints=True,
                                               image_minimum_row_height=100), "fixture", OBSERVED)
    assert message.type == expected
    assert message.metadata.get("type_hint") == hint
    assert message.text == label and media is None


def test_profile_type_selectors_can_use_native_evidence_without_a_label_heuristic():
    row = FixtureNode("list item", name="attachment", children_nodes=[FixtureNode("audio"), FixtureNode("audio")])
    voice = profile(body={"path": []}, message_type_selectors={"voice": {"role": "audio"}})
    message, _ = parse_message(row, voice, "fixture", OBSERVED)
    assert message.type == "voice" and message.metadata["type_basis"] == "atspi_selector"
    row.children_nodes.append(FixtureNode("document frame"))
    conflict = profile(body={"path": []}, message_type_selectors={
        "voice": {"role": "audio"}, "file": {"role": "document frame"},
    })
    unknown, _ = parse_message(row, conflict, "fixture", OBSERVED)
    assert unknown.type == "unknown" and unknown.metadata["type_hints"] == ["voice", "file"]
    with pytest.raises(ValueError, match="message_type_selectors"):
        profile(message_type_selectors={"unsupported": {"role": "widget"}})


def test_link_card_descendants_supply_native_uri_before_displayed_url_without_changing_anchor():
    card = FixtureNode("panel", name="Read https://example.org/preview", children_nodes=[
        FixtureNode("label", name="https://example.org/display"),
        FixtureNode("filler", children_nodes=[
            FixtureNode("link", uris=("file:///tmp/secret", "https://example.org/actual")),
        ]),
    ])
    row = FixtureNode("list item", children_nodes=[card])
    configured = profile(link={"role": "panel"})
    message, _ = parse_message(row, configured, "fixture", OBSERVED)
    assert message.type == "link" and message.url == "https://example.org/actual"
    assert message.metadata["urls"] == ["https://example.org/actual", "https://example.org/display",
                                        "https://example.org/preview"]
    card.children_nodes = []
    previous, _ = parse_message(row, configured, "fixture", OBSERVED)
    assert previous.url == "https://example.org/preview"
    assert message_key(previous) == message_key(message)


@pytest.mark.parametrize("label", ["15:20", "8:10 PM", "Yesterday 8:10 PM", "Mon. 09:01",
                                   "11/24/2026 08:10", "2026-11-24 8:10", "昨天 下午8:10", "11月24日 8:10"])
def test_recognizes_known_timestamp_label_families(label):
    assert _wechat_time_label(label)


def test_only_compact_clock_rows_are_used_as_context_never_as_message_timestamp(monkeypatch):
    label = "Yesterday 8:10 PM"
    rows = [FixtureNode("list item", name=label, bounds=(20, 40, 100, 41)),
            FixtureNode("list item", name=label, bounds=(20, 81, 100, 75))]
    panel = FixtureNode("list", bounds=(20, 40, 100, 116), children_nodes=rows)
    chat = SimpleNamespace(panel=panel, scrollbar=None)
    source = AccessibilitySource("fixture", profile=profile_fields(body={"path": []}, time_separator_height=41,
                                                                   tail_from_last_row=True))
    monkeypatch.setattr(source, "_verify_chat", lambda _chat: None)
    snapshot, _ = source._read_chat(chat)
    assert snapshot.at_tail and len(snapshot.messages) == 1
    assert snapshot.messages[0].text == label and snapshot.messages[0].timestamp is None
    assert snapshot.messages[0].metadata["preceding_time_label"] == label
    assert not _wechat_time_label("by 8:10") and not _wechat_time_label("Yesterday 99:99")


def test_ambiguous_selector_and_conflicting_ownership_fail_closed():
    root = FixtureNode("window", children_nodes=[FixtureNode("label"), FixtureNode("label")])
    with pytest.raises(ReadError, match="ambiguous"):
        Selector.parse({"role": "label"}).one(root)
    row = FixtureNode("list item", children_nodes=[FixtureNode("label", name="甲")])
    with pytest.raises(ReadError, match="ownership"):
        parse_message(row, profile(own={"name": "甲"}, other={"name": "甲"}), "验收群", OBSERVED)


def test_profile_requires_complete_date_to_parse_an_original_timestamp():
    with pytest.raises(ValueError, match="full date"):
        profile(timestamp_format="%H:%M")


def test_url_extraction_removes_separating_punctuation_and_keeps_ipv6():
    assert urls_in("访问 https://[::1]:444/now.\nhttps://example.org，前往") == (
        "https://[::1]:444/now", "https://example.org",
    )


def test_history_pages_merge_only_with_sufficient_anchor():
    def history(values):
        return tuple(Message(sender="Aria", text=letter, type="text") for letter in values)

    assert [msg.text for msg in _merge_history(history("abcd"), history("cdeef"))] == list("abcdeef")
    assert _merge_history(history("abc"), history("cddd")) is None
    assert _merge_history(history("aaaa"), history("aab")) is None


def test_recognizes_only_visible_documented_wechat_login_states():
    confirm = FixtureNode("label", name="Confirm on Phone", showing=False)
    login = FixtureNode("push button", name="Log In")
    switch = FixtureNode("push button", name="Switch Account")
    window = FixtureNode("frame", bounds=(500, 260, 280, 380), children_nodes=[confirm, login, switch])
    assert _wechat_login_state(window) == "login_required"
    confirm.showing, login.showing, switch.showing = True, False, False
    assert _wechat_login_state(window) == "login_confirmation_required"
    window.bounds = (0, 0, 1280, 900)
    assert _wechat_login_state(window) is None


def selectable_chat(monkeypatch, *, preset=False, **selectors):
    header = FixtureNode("label", name="旧目标")
    count = FixtureNode("label", name="(3)")
    title = FixtureNode("filler", children_nodes=[header, count])
    row = FixtureNode("list item", name="原会话消息", bounds=(20, 65, 100, 25))
    panel = FixtureNode("list", bounds=(20, 40, 100, 50), children_nodes=[row])
    editor = FixtureNode("text", text="", bounds=(20, 105, 100, 30))
    window = FixtureNode("frame", bounds=(0, 0, 200, 200), children_nodes=[title, panel, editor])
    app = FixtureNode("application", name="wechat", children_nodes=[window],
                      native=SimpleNamespace(get_process_id=lambda: 42))
    fields = profile_fields(chat_header={"path": [0, 0], "role": "label"}, body={"path": []},
                            tail_from_last_row=True, **selectors)
    source = AccessibilitySource("旧目标", profile=fields)
    if preset:
        source._preset = "wechat-4.1.13.29-arm64-en"
        monkeypatch.setattr("bridge.accessibility._WECHAT_TITLE", [0])
        monkeypatch.setattr("bridge.accessibility.platform.machine", lambda: "aarch64")
    source._atspi, source._version = object(), "fixture"
    monkeypatch.setattr("bridge.accessibility.desktop_nodes", lambda _: iter((app,)))

    async def local_call(method, *args):
        return method(*args)

    monkeypatch.setattr(source, "_call", local_call)
    return source, title, header, count, row


async def test_rebind_clears_provenance_and_sender_refuses_old_or_unmatched_title(monkeypatch):
    source, _, header, _, row = selectable_chat(
        monkeypatch, group_count={"path": [0, 1], "role": "label"},
    )
    old = await source.read_snapshot(capture_media=False)
    source._last_read = old
    assert old.ready and old.at_tail
    assert source._capture_origin is not None

    sender = desktop.MessageSender(old.chat, source, asyncio.Lock(), "Return")
    await source.set_target("新目标", "group")
    status = await source.get_status()
    assert status["state"] == "waiting_for_target" and status["target_kind"] == "group"
    assert status["chat_kind"] is None and not status["at_tail"]
    assert source._capture_origin is None and source._last_read is None and source._app_pid is None
    assert not (await source.read_snapshot(capture_media=False)).ready
    with pytest.raises(ReadError, match="different WeChat target"):
        await source.capture_messages(old, [0])

    header.name = "新目标 "
    assert await source.get_target() is None
    header.name, row.name = "新目标", "新会话消息"
    current = await source.read_snapshot(capture_media=False)
    assert current.ready and current.chat == "新目标" and current.messages[0].text == "新会话消息"
    assert (await source.get_status())["chat_kind"] == "group"

    calls = []

    async def command(*args):
        calls.append(args)
        return {
            "getactivewindow": "1000",
            "getwindowpid": "42",
            "getwindowgeometry": "X=0\nY=0\nWIDTH=200\nHEIGHT=200",
        }[args[1]]

    monkeypatch.setattr(desktop, "command", command)
    with pytest.raises(desktop.DesktopError, match="target chat"):
        await sender.active_target()
    assert not calls
    sender.set_target("新目标")
    assert await sender.active_target() == "1000"


async def test_builtin_requires_consistent_type_evidence_from_the_title_container(monkeypatch):
    source, title, header, count, _ = selectable_chat(
        monkeypatch, preset=True, group_count={"path": [0, 1], "role": "label"},
    )
    assert (await source.read_snapshot(capture_media=False)).ready
    await source.set_target(header.name, "private")
    assert not (await source.read_snapshot(capture_media=False)).ready
    assert (await source.get_status())["state"] == "chat_kind_mismatch"

    count.showing = False
    count.states = frozenset({"enabled"})
    assert not (await source.read_snapshot(capture_media=False)).ready  # stale count still present
    count.name = ""
    assert (await source.read_snapshot(capture_media=False)).ready
    assert (await source.get_status())["chat_kind"] == "private"
    title.children_nodes.pop()
    assert (await source.read_snapshot(capture_media=False)).ready

    title.children_nodes.append(FixtureNode("filler"))
    assert not (await source.read_snapshot(capture_media=False)).ready
    assert (await source.get_status())["state"] == "chat_kind_unverified"


async def test_explicit_custom_target_requires_positive_unambiguous_type_evidence(monkeypatch):
    source, title, header, _, _ = selectable_chat(monkeypatch)
    assert (await source.read_snapshot(capture_media=False)).ready  # legacy title-only settings
    for kind in ("group", "private"):
        await source.set_target(header.name, kind)
        assert not (await source.read_snapshot(capture_media=False)).ready
        assert (await source.get_status())["state"] == "chat_kind_unverified"

    title.children_nodes.append(FixtureNode("push button", name="Private chat detail marker"))
    source.profile = Profile.parse(profile_fields(
        chat_header={"path": [0, 0], "role": "label"}, body={"path": []}, tail_from_last_row=True,
        private_header={"path": [0, 2], "role": "push button", "name": "Private chat detail marker"},
    ))
    assert (await source.read_snapshot(capture_media=False)).ready
    assert (await source.get_status())["chat_kind"] == "private"
    source.profile = Profile.parse(profile_fields(
        chat_header={"path": [0, 0], "role": "label"}, body={"path": []}, tail_from_last_row=True,
        group_count={"path": [0, 1], "role": "label"},
        private_header={"path": [0, 2], "role": "push button", "name": "Private chat detail marker"},
    ))
    assert not (await source.read_snapshot(capture_media=False)).ready
    assert (await source.get_status())["state"] == "chat_kind_unverified"
    with pytest.raises(ValueError, match="verified private-chat control"):
        profile(private_header={"path": [0], "role": "label"})


async def test_prepare_target_scrolls_only_verified_chat_and_reads_fresh_tail(monkeypatch):
    source, _, header, _, row = selectable_chat(
        monkeypatch, group_count={"path": [0, 1], "role": "label"}, recovery_wheel_ticks=2,
    )
    events = []

    @asynccontextmanager
    async def pause():
        events.append("paused")
        yield
        events.append("resumed")

    def restore_tail():
        assert source.desktop_lock.locked()
        events.append("scrolled")
        row.bounds = (20, 65, 100, 25)

    monkeypatch.setattr(desktop, "pause_vnc_input", pause)
    monkeypatch.setattr(source, "_restore_tail", restore_tail)
    row.bounds = (20, 80, 100, 25)
    await source.set_target("新目标", "group")
    assert not (await source.prepare_target()).ready
    assert events == []

    header.name = "新目标"
    snapshot = await source.prepare_target()
    assert snapshot.ready and snapshot.at_tail and snapshot.chat == "新目标"
    assert events == ["paused", "scrolled", "resumed"]


def test_custom_send_requires_unique_observed_quote_panel_and_proof_it_is_hidden(monkeypatch):
    hidden = FixtureNode("filler", name="quote panel", showing=False, states=frozenset({"enabled"}))
    window = FixtureNode("frame", children_nodes=[hidden])
    source = AccessibilitySource("fixture", profile=profile_fields())
    monkeypatch.setattr(source, "_chat", lambda: SimpleNamespace(window=window))
    with pytest.raises(ReadError, match="quote-preview state"):
        source._quote_clear()

    source.profile = profile(quote_preview={"role": "filler", "name": "quote panel"})
    assert source._quote_clear() is True
    hidden.states = frozenset({"enabled", "visible"})
    assert source._quote_clear() is False
    hidden.states = frozenset({"defunct"})
    with pytest.raises(ReadError, match="quote-preview state"):
        source._quote_clear()
    window.children_nodes = []
    with pytest.raises(ReadError, match="quote-preview state"):
        source._quote_clear()


def test_amd64_requires_custom_selectors_and_reports_read_and_send_preconditions(monkeypatch):
    private_text = "private fixture row"
    panel = FixtureNode("list", bounds=(20, 40, 100, 50), children_nodes=[
        FixtureNode("list item", name=private_text, bounds=(20, 65, 100, 25)),
    ])
    editor = FixtureNode("text", text="", bounds=(20, 105, 100, 30))
    quote = FixtureNode("filler", states=frozenset({"enabled"}), showing=False)
    window = FixtureNode("frame", bounds=(0, 0, 200, 200), children_nodes=[
        FixtureNode("label", name="fixture group"), panel, editor, quote,
    ])
    app = FixtureNode("application", name="wechat", children_nodes=[window],
                      native=SimpleNamespace(get_process_id=lambda: 42))
    monkeypatch.setattr("bridge.accessibility.platform.machine", lambda: "x86_64")
    monkeypatch.setattr("bridge.accessibility.desktop_nodes", lambda _atspi: iter((app,)))
    connection = SimpleNamespace(close=lambda: None)
    monkeypatch.setitem(sys.modules, "Xlib", SimpleNamespace(display=SimpleNamespace(Display=lambda: connection)))
    monkeypatch.setattr("bridge.accessibility._active_x11", lambda *_: (object(), 123))
    preset = AccessibilitySource("fixture group", profile="wechat-4.1.13.29-arm64-en")
    assert preset.inspect_profile(object(), "4.1.13.29")["state"] == "profile_architecture_mismatch"

    mapping = profile_fields(package_version="4.1.13.29", body={"path": []}, tail_from_last_row=True,
                             quote_preview={"path": [3], "role": "filler"})
    source = AccessibilitySource("fixture group", profile=mapping)
    result = source.inspect_profile(object(), "4.1.13.29")
    assert result["state"] == "ready"
    assert result["visible_rows"] == 1
    assert result["message_types"] == ["text"]
    assert result["composer"] == "empty" and result["quote_preview"] == "clear"
    assert result["manual_send_preconditions"] is True
    assert private_text not in str(result)
    editor.text = "private unsent draft"
    occupied = source.inspect_profile(object(), "4.1.13.29")
    assert occupied["composer"] == "occupied" and occupied["manual_send_preconditions"] is False
    assert editor.text not in str(occupied)


def test_profile_check_cli_keeps_output_separate_from_tree_contents(monkeypatch, tmp_path, capsys):
    config = tmp_path / "config.yaml"
    config.write_text("chat: ''\n", encoding="utf-8")
    atspi = SimpleNamespace(init=lambda: 0, set_timeout=lambda *_: None, exit=lambda: None)
    monkeypatch.setattr(inspect_atspi, "runtime", lambda: (atspi, None))
    monkeypatch.setattr(inspect_atspi, "client_version", lambda: "fixture")
    monkeypatch.setattr(sys, "argv", ["inspect_atspi", "--check-profile", str(config), "--json", "-"])
    assert inspect_atspi.main() == 2
    result = json.loads(capsys.readouterr().out)
    assert result["profile_check"]["state"] == "unconfigured"
    assert result["trees"] == []
