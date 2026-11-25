"""Discover and activate sessions exposed by the official client's UI."""

from __future__ import annotations

import re
import time

from bridge.accessibility import Node, client_version, runtime, wechat_english_profile


SEARCH_TYPES = {
    "群聊": "group",
    "Groups": "group",
    "Group Chats": "group",
    "联系人": "private",
    "Contacts": "private",
}
NON_SESSION_SECTIONS = {
    "聊天记录", "搜索网络结果", "搜一搜", "功能",
    "Chat History", "Messages", "Web Results", "Search the Web", "Features",
    "最常使用", "Most Used", "Most used",
}
INVENTORY_LIMIT = 200
SEARCH_ROW_LIMIT = 150


class SessionSearchError(RuntimeError):
    """The client did not provide enough evidence to identify the requested session."""


def select_result(rows, name: str, kind: str):
    """Pick a single typed, exact row from the same bounded search snapshot."""
    candidates = [
        (result_kind, node)
        for title, result_kind, node in rows
        if title == name and result_kind is not None
    ]
    if len(candidates) != 1:
        raise SessionSearchError("No unique exact session match")
    result_kind, node = candidates[0]
    if result_kind not in {"group", "private"} or (kind != "unknown" and kind != result_kind):
        raise SessionSearchError("The exact result does not expose the requested session kind")
    return result_kind, node


class SessionNavigator:
    def __init__(self, wx, a11y, xinput):
        self.wx, self.a11y, self.xinput = wx, a11y, xinput
        self.profile = wechat_english_profile(client_version())

    def _current(self, main):
        if self.profile is not None and self.a11y.name(main) == "Weixin":
            atspi, _ = runtime()
            root = Node(main, atspi)
            title, count = self.profile.chat_header.one(root), self.profile.group_count.one(root)
            if title is not None and title.showing and count is not None and count.showing:
                name = (title.text or title.name or "").strip()
                if name and re.fullmatch(r"\(\d+\)", count.name):
                    return name, "group"
        current = self.wx.current_chat()
        if isinstance(current, dict) and current.get("name"):
            return current["name"], "group" if current.get("is_group") is True else "unknown"
        return "", "unknown"

    def inventory(self) -> dict:
        main = self.wx.main_frame()
        if main is None:
            raise SessionSearchError("WeChat is signed out or its main window is still loading")
        sidebar = self.wx.session_list()
        sessions = []
        if sidebar is None or self.a11y.name(sidebar) not in {"Chats", "会话"} or not self.a11y.is_showing(sidebar):
            return {"sessions": sessions, "complete": False}
        active_name, active_kind = self._current(main)
        for index in range(min(sidebar.childCount, INVENTORY_LIMIT)):
            row = sidebar.getChildAtIndex(index)
            if row is None or self.a11y.role(row) != "list item":
                continue
            name = self.a11y.name(row).partition("\n")[0].strip()
            if not name or name.startswith(("折叠的聊天", "Folded Chats", "Folded chats")):
                continue
            kind = active_kind if name == active_name and "selected" in self.a11y.states(row) else "unknown"
            sessions.append({"name": name, "kind": kind, "native_id": None})
        # Qt can materialize only a viewport, and folded chats are not examined.
        return {"sessions": sessions, "complete": False}

    def _active_pid(self) -> bool:
        window = self.xinput.active_window()
        return bool(window and self.xinput.run(["xdotool", "getwindowpid", window], timeout=3).strip()
                    == str(self.wx.app().get_process_id()))

    def _popups(self) -> list:
        return [popup for popup in self.wx.popups() if self.a11y.is_showing(popup)]

    def _search_field(self, main):
        window = self.a11y.box(main)

        def usable(node):
            box = self.a11y.box(node)
            return (
                self.a11y.is_showing(node) and box.visible() and "editable" in self.a11y.states(node)
                and window.x <= box.x < window.x + 320 and window.y <= box.y < window.y + 96
            )

        fields = self.a11y.find_all(main, role_name="text", name_in=("搜索", "Search"), pred=usable, limit=2)
        if len(fields) != 1:
            raise SessionSearchError("The client's search field could not be verified")
        return fields[0]

    def _search_results(self, popup) -> list:
        items = self.a11y.find_all(popup, role_name="list item", limit=SEARCH_ROW_LIMIT + 1)
        if len(items) > SEARCH_ROW_LIMIT:
            raise SessionSearchError("The search result list is too large to disambiguate")
        section = ""
        rows = []
        for item in items:
            label = self.a11y.name(item)
            box = self.a11y.box(item)
            if not label:
                continue
            title, separator, _ = label.partition("\n")
            title = title.strip()
            if 0 < box.h <= 34 and not separator:
                section = title
                if title not in SEARCH_TYPES and title not in NON_SESSION_SECTIONS:
                    rows.append((title, "unknown", item))
            else:
                kind = None if section in NON_SESSION_SECTIONS else SEARCH_TYPES.get(section, "unknown")
                rows.append((title, kind, item))
        return rows

    def _activate(self, popup, item) -> bool:
        if not self._active_pid() or not self.a11y.is_showing(item):
            return False
        try:
            action = item.queryAction()
        except NotImplementedError:
            action = None
        if action is not None:
            choices = [
                index for index in range(action.nActions)
                if action.getName(index).casefold() in {"press", "click", "activate"}
            ]
            if len(choices) == 1:
                return bool(action.doAction(choices[0]))
            if choices:
                return False

        # Some Qt rows expose coordinates rather than Action. Accept only unambiguous
        # screen coordinates that resolve back to this exact row in the same popup.
        import pyatspi

        bounds, row = self.a11y.box(popup), self.a11y.box(item)
        x, y = row.cx, row.cy
        if ((bounds.x, bounds.y) == (0, 0) or not bounds.visible() or not row.visible()
                or not (bounds.x <= x < bounds.x + bounds.w and bounds.y <= y < bounds.y + bounds.h)):
            return False
        hit = popup.queryComponent().getAccessibleAtPoint(x, y, pyatspi.DESKTOP_COORDS)
        for _ in range(8):
            if hit == item:
                self.xinput.click(x, y)
                return True
            if hit is None or hit == popup:
                break
            hit = hit.parent
        return False

    def open(self, name: str, kind: str) -> dict:
        main = self.wx.main_frame()
        if main is None or not self._active_pid() or self._popups():
            raise SessionSearchError("The active WeChat window is not ready for session search")
        field = self._search_field(main)
        try:
            text, editable = field.queryText(), field.queryEditableText()
        except NotImplementedError as error:
            raise SessionSearchError("The client does not expose an editable search field") from error
        if text.getText(0, -1):
            raise SessionSearchError("The WeChat search field is already in use")
        if not field.queryComponent().grabFocus() or not self.a11y.wait_for(
            lambda: "focused" in self.a11y.states(field), timeout=0.6,
        ):
            raise SessionSearchError("The client's search field did not receive focus")
        try:
            if not editable.setTextContents(name) or not self.a11y.wait_for(
                lambda: text.getText(0, -1) == name, timeout=0.6,
            ):
                raise SessionSearchError("The client's search field did not accept the title")

            popups = self.a11y.wait_for(lambda: self._popups(), timeout=3)
            if not popups or len(popups) != 1:
                raise SessionSearchError("The native search results could not be verified")
            popup = popups[0]
            last_rows = None
            changed_at = time.monotonic()

            def settled_results():
                nonlocal last_rows, changed_at
                if self._popups() != [popup] or text.getText(0, -1) != name:
                    return False
                rows = self._search_results(popup)
                fingerprint = [(title, candidate_kind) for title, candidate_kind, _ in rows]
                if fingerprint != last_rows:
                    last_rows, changed_at = fingerprint, time.monotonic()
                return rows if time.monotonic() - changed_at >= 0.5 else False

            rows = self.a11y.wait_for(settled_results, timeout=3)
            result_kind, item = select_result(rows or (), name, kind)
            if self._popups() != [popup] or select_result(self._search_results(popup), name, kind) != (result_kind, item):
                raise SessionSearchError("The native search results changed before activation")
            if not self._activate(popup, item):
                raise SessionSearchError("The exact result could not be safely activated")

            def opened():
                if self._popups() or not self._active_pid() or self.wx.main_frame() != main:
                    return False
                current, verified_kind = self._current(main)
                return current == name and (
                    verified_kind == "group" if result_kind == "group" else verified_kind != "group"
                )

            if not self.a11y.wait_for(opened, timeout=3):
                raise SessionSearchError("The opened session could not be verified")
            return {"name": name, "kind": result_kind, "native_id": None}
        finally:
            try:
                if text.getText(0, -1) == name and self.wx.main_frame() == main and self._active_pid():
                    if editable.setTextContents("") and "focused" in self.a11y.states(field):
                        self.xinput.key("Escape")
            except (RuntimeError, NotImplementedError):
                pass
