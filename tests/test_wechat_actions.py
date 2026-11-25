from dataclasses import dataclass, field

from bridge.wechat_actions import find_quote_action, identify_profile_popup, inspect_profile_popup
from bridge.wechat_images import find_image_viewer


@dataclass
class StubNode:
    role: str = "filler"
    name: str = ""
    bounds: tuple[int, int, int, int] | None = None
    states: set[str] = field(default_factory=lambda: {"showing", "visible", "sensitive"})
    nodes: list["StubNode"] = field(default_factory=list)
    native: object = field(default_factory=object)

    @property
    def showing(self):
        return {"showing", "visible"} <= self.states

    def children(self):
        return iter(self.nodes)

    def child(self, index):
        return self.nodes[index] if index < len(self.nodes) else None


def wechat_with_popup(popup):
    main = StubNode("frame", "Weixin", (174, 112, 880, 640))
    return StubNode("application", "wechat", nodes=[main, popup]), main


def insert_at(root, path, node):
    parent = root
    for index in path[:-1]:
        while len(parent.nodes) <= index:
            parent.nodes.append(StubNode())
        parent = parent.nodes[index]
    while len(parent.nodes) <= path[-1]:
        parent.nodes.append(StubNode())
    parent.nodes[path[-1]] = node


def quote_menu():
    quote = StubNode("menu item", "Quote", (932, 791, 156, 33))
    copy = StubNode("menu item", "Copy", (932, 525, 156, 32))
    app, main = wechat_with_popup(StubNode("popup menu", bounds=(927, 520, 166, 382), nodes=[copy, quote]))
    return app, main, quote


def test_selects_exact_native_quote_in_the_nearby_wechat_menu():
    app, main, quote = quote_menu()
    assert find_quote_action(app, main, (927, 523)) == (app.nodes[1], quote)
    assert find_quote_action(app, main, (600, 400)) is None


def test_refuses_ambiguous_mislabeled_or_foreign_quote_menus():
    app, main, quote = quote_menu()
    menu = app.nodes[1]
    menu.nodes.append(StubNode("menu item", "Quote", (932, 730, 156, 33)))
    assert find_quote_action(app, main, (927, 523)) is None
    menu.nodes.pop()
    quote.name = "Quote and send"
    assert find_quote_action(app, main, (927, 523)) is None
    quote.name = "Quote"
    app.name = "Other application"
    assert find_quote_action(app, main, (927, 523)) is None


def test_refuses_unavailable_quote_or_another_visible_popup():
    app, main, quote = quote_menu()
    quote.states.remove("sensitive")
    assert find_quote_action(app, main, (927, 523)) is None
    quote.states.add("sensitive")
    app.nodes.append(StubNode("dialog", bounds=(900, 500, 150, 150)))
    assert find_quote_action(app, main, (927, 523)) is None


def profile_card(alias):
    label = StubNode("label", "Group Alias:", (639, 437, 72, 14))
    value = StubNode("label", alias, (711, 437, 60, 14))
    pane = StubNode(nodes=[label, value])
    for index in reversed([0, 0, 0, 0, 1, 2, 3]):
        pane = StubNode(nodes=[*[StubNode() for _ in range(index)], pane])
    pane.bounds = (539, 389, 280, 244)
    pane.states.add("active")
    return wechat_with_popup(pane)


def test_profile_reads_only_the_visible_group_alias_of_the_active_card():
    app, main = profile_card("  测试成员 🌱  ")
    result = inspect_profile_popup(app, main, (513, 407))
    assert result is not None
    assert result.node is app.nodes[1]
    assert result.nickname == "测试成员 🌱"
    app.nodes[1].states.remove("active")
    assert inspect_profile_popup(app, main, (513, 407)) is None


def test_profile_needs_a_nearby_card_with_an_unambiguous_one_line_alias():
    app, main = profile_card("测试\n成员")
    assert inspect_profile_popup(app, main, (513, 407)) is None
    app, main = profile_card("测试成员")
    assert inspect_profile_popup(app, main, (700, 250)) is None


def test_identifies_card_without_alias_to_close_it_without_reading_account_id():
    card = StubNode(bounds=(539, 384, 280, 516))
    card.states.add("active")

    account = StubNode("label", "Weixin ID:", (635, 508, 72, 12))
    more = StubNode("push button", "More", (780, 400, 32, 22))
    insert_at(card, (0, 0, 0, 0, 1, 2, 4, 0), account)
    insert_at(card, (0, 0, 0, 0, 1, 0, 1, 0), more)
    app, main = wechat_with_popup(card)
    result = identify_profile_popup(app, main, (513, 407))
    assert result is not None and result.node is card and result.nickname is None
    more.name = "Unexpected control"
    assert identify_profile_popup(app, main, (513, 407)) is None


def test_recognizes_only_the_unique_active_photo_frame_with_verified_controls():
    frame = StubNode("frame", "Photos and Videos", (227, 22, 825, 855))
    frame.states.add("active")
    close = StubNode("push button", "Close", (1007, 23, 44, 40))
    zoom = StubNode("push button", "Zoom", (466, 29, 28, 28))
    insert_at(frame, (0, 1, 3), close)
    insert_at(frame, (0, 0, 0, 2, 0), StubNode("push button", "Previous", (293, 29, 28, 28)))
    insert_at(frame, (0, 0, 0, 3, 0), StubNode("push button", "Next", (333, 29, 28, 28)))
    insert_at(frame, (0, 0, 0, 7, 0), zoom)
    app, main = wechat_with_popup(frame)
    match = find_image_viewer(app, main)
    assert match is not None and match.window is frame and match.close is close
    app.nodes.append(StubNode("dialog", bounds=frame.bounds))
    assert find_image_viewer(app, main) is None
    app.nodes.pop()
    close.bounds = (1007, 0, 44, 40)
    assert find_image_viewer(app, main) is None
    close.bounds = (1007, 23, 44, 40)
    zoom.name = "Unknown control"
    assert find_image_viewer(app, main) is None
