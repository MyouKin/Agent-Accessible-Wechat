"""Read-only selectors for native popups observed in the WeChat desktop client."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .accessibility import Bounds, Node

Point = tuple[int, int]


@dataclass(frozen=True)
class ProfilePopup:
    node: Node
    nickname: str | None


def _near(point: Point, bounds: Bounds, margin: int = 0) -> bool:
    x, y = point
    left, top, width, height = bounds
    return (
        left - margin <= x < left + width + margin
        and top - margin <= y < top + height + margin
    )


def _popup(app: Node, main_window: Node, point: Point, role: str, margin: int) -> Node | None:
    from .accessibility import wechat_app

    main_bounds = main_window.bounds
    if not wechat_app(app) or main_bounds is None or not _near(point, main_bounds):
        return None
    visible = [node for node in app.children() if node.showing]
    siblings = [node for node in visible if node.native != main_window.native]
    if len(visible) != 2 or len(siblings) != 1:
        return None
    popup = siblings[0]
    bounds = popup.bounds
    if popup.role != role or popup.name or bounds is None:
        return None
    return popup if _near(point, bounds, margin) else None


def find_quote_action(app: Node, main_window: Node, point: Point) -> tuple[Node, Node] | None:
    """Return the unique enabled Quote item from the nearby text-message menu."""
    from .accessibility import intersection

    popup = _popup(app, main_window, point, "popup menu", margin=12)
    if popup is None:
        return None
    items = [node for node in popup.children() if node.role == "menu item" and node.showing]
    if not any(node.name == "Copy" for node in items):
        return None
    quotes = [node for node in items if node.name == "Quote"]
    if len(quotes) != 1:
        return None
    quote = quotes[0]
    if (
        "sensitive" not in quote.states
        or quote.bounds is None
        or intersection(quote.bounds, popup.bounds) != quote.bounds
    ):
        return None
    return popup, quote


def identify_profile_popup(app: Node, main_window: Node, avatar_point: Point) -> ProfilePopup | None:
    """Recognize the nearby native card, including cards without a group alias."""
    from .accessibility import Selector, intersection

    popup = _popup(app, main_window, avatar_point, "filler", margin=48)
    if popup is None or "active" not in popup.states:
        return None

    def visible_in_card(node: Node | None) -> bool:
        return bool(node is not None and node.showing and node.bounds is not None
                    and intersection(node.bounds, popup.bounds) == node.bounds)

    field = Selector.parse({"path": [0, 0, 0, 0, 1, 2, 3]}).one(popup)
    if field is not None and field.showing:
        children = list(field.children())
        label = children[0] if children else None
        value = children[1] if len(children) == 2 else None
        if visible_in_card(label) and label.role == "label" and label.name == "Group Alias:":
            nickname = value.name.strip() if visible_in_card(value) and value.role == "label" else ""
            if nickname and len(nickname) <= 128 and len(nickname.splitlines()) == 1:
                return ProfilePopup(popup, nickname)
            return ProfilePopup(popup, None)

    account_label = Selector(path=(0, 0, 0, 0, 1, 2, 4, 0), role="label", name="Weixin ID:").one(popup)
    more_button = Selector(path=(0, 0, 0, 0, 1, 0, 1, 0), role="push button", name="More").one(popup)
    if visible_in_card(account_label) and visible_in_card(more_button):
        return ProfilePopup(popup, None)
    return None


def inspect_profile_popup(app: Node, main_window: Node, avatar_point: Point) -> ProfilePopup | None:
    """Return a profile card only when it exposes a usable group alias."""
    profile = identify_profile_popup(app, main_window, avatar_point)
    return profile if profile is not None and profile.nickname is not None else None
