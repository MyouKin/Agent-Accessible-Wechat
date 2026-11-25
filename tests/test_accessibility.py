import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from bridge.accessibility import (
    AccessibilitySource, Profile, ReadError, Selector, _merge_history, _wechat_login_state, parse_message, urls_in,
)
from bridge.models import Message
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
