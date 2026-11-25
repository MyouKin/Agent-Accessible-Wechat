"""Read a single WeChat chat through a locally verified AT-SPI profile."""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import math
import os
import platform
import re
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from functools import cached_property
from importlib import import_module
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .models import Message, Snapshot, message_key

LOG = logging.getLogger(__name__)
EVENT_TYPES = (
    "object:children-changed", "object:text-changed", "object:visible-data-changed",
    "object:property-change:accessible-name", "object:property-change:accessible-value",
    "object:state-changed:showing", "window",
)
URL_PATTERN = re.compile(r"https?://[^\s<>\u3000-\u303f\uff00-\uffef]+", re.I)
Bounds = tuple[int, int, int, int]


class ReadError(RuntimeError):
    """The current desktop does not provide a trustworthy reading."""


def runtime() -> tuple[Any, Any]:
    gi = import_module("gi")
    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi, GLib

    return Atspi, GLib


def client_version() -> str | None:
    try:
        return subprocess.check_output(
            ["dpkg-query", "-W", "-f=${Version}", "wechat"],
            stderr=subprocess.DEVNULL, text=True, timeout=3,
        ).strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def rect_bounds(rect: Any) -> Bounds | None:
    if rect is None or rect.width <= 0 or rect.height <= 0:
        return None
    # AT-SPI uses the minimum int as a sentinel for unknown positions.
    if min(rect.x, rect.y) <= -(2**30):
        return None
    return rect.x, rect.y, rect.width, rect.height


def intersection(first: Bounds | None, second: Bounds | None) -> Bounds | None:
    if first is None or second is None:
        return None
    ax, ay, aw, ah = first
    bx, by, bw, bh = second
    left, top = max(ax, bx), max(ay, by)
    right, bottom = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    return (left, top, right - left, bottom - top) if right > left and bottom > top else None


class Node:
    def __init__(self, native: Any, atspi: Any):
        self.native = native
        self.atspi = atspi

    def refresh(self) -> None:
        clear = getattr(self.native, "clear_cache_single", self.native.clear_cache)
        clear()
        for name in ("name", "description", "interfaces", "states"):
            self.__dict__.pop(name, None)

    @cached_property
    def role(self) -> str:
        return self.native.get_role_name() or ""

    @cached_property
    def name(self) -> str:
        return self.native.get_name() or ""

    @cached_property
    def description(self) -> str:
        return self.native.get_description() or ""

    @cached_property
    def accessible_id(self) -> str:
        return self.native.get_accessible_id() or ""

    @cached_property
    def attributes(self) -> dict[str, str]:
        return self.native.get_attributes() or {}

    @cached_property
    def interfaces(self) -> frozenset[str]:
        return frozenset(name.rsplit(".", 1)[-1] for name in self.native.get_interfaces() or ())

    @cached_property
    def states(self) -> frozenset[str]:
        return frozenset(state.value_nick for state in self.native.get_state_set().get_states())

    @property
    def showing(self) -> bool:
        return {"showing", "visible"} <= self.states and "defunct" not in self.states

    def child(self, index: int) -> Node | None:
        child = self.native.get_child_at_index(index)
        return Node(child, self.atspi) if child is not None else None

    def children(self) -> Iterator[Node]:
        count = self.native.get_child_count()
        if count < 0:
            raise ReadError("accessible child count unavailable")
        for index in range(count):
            child = self.child(index)
            if child is None:
                raise ReadError("accessible child disappeared")
            yield child

    @property
    def bounds(self) -> Bounds | None:
        if "Component" in self.interfaces:
            return rect_bounds(self.native.get_extents(self.atspi.CoordType.SCREEN))
        return None

    @property
    def image_bounds(self) -> Bounds | None:
        if "Image" in self.interfaces:
            bounds = rect_bounds(self.native.get_image_extents(self.atspi.CoordType.SCREEN))
            if bounds is not None:
                return bounds
        return self.bounds

    @property
    def text(self) -> str | None:
        if "Text" not in self.interfaces:
            return None
        count = self.atspi.Text.get_character_count(self.native)
        if count < 0 or count > 100_000:
            return None
        return self.atspi.Text.get_text(self.native, 0, count) if count else ""

    @property
    def label(self) -> str | None:
        text = self.text
        return text if text and text.strip() else self.name or self.description or None

    @property
    def uris(self) -> tuple[str, ...]:
        links = []
        if "Hypertext" in self.interfaces:
            count = self.atspi.Hypertext.get_n_links(self.native)
            for index in range(min(count, 32)):
                links.append(self.atspi.Hypertext.get_link(self.native, index))
        if "Hyperlink" in self.interfaces:
            links.append(self.native.get_hyperlink())
        return tuple(dict.fromkeys(
            uri for link in links if link is not None and link.is_valid()
            for index in range(min(link.get_n_anchors(), 8)) if (uri := link.get_uri(index))
        ))

    @property
    def value(self) -> Any:
        return self.native.get_value_iface() if "Value" in self.interfaces else None


def desktop_nodes(atspi: Any) -> Iterator[Node]:
    desktop = atspi.get_desktop(0)
    if desktop is None:
        raise ReadError("AT-SPI desktop unavailable")
    return Node(desktop, atspi).children()


def wechat_app(node: Node) -> bool:
    return node.name.strip().casefold() in {"wechat", "weixin", "微信"}


@dataclass(frozen=True)
class Selector:
    path: tuple[int, ...] | None = None
    role: str | None = None
    name: str | None = None
    description: str | None = None
    accessible_id: str | None = None
    interfaces: frozenset[str] = frozenset()
    attributes: tuple[tuple[str, str], ...] = ()

    @classmethod
    def parse(cls, fields: Any) -> Selector:
        if not isinstance(fields, dict) or not fields:
            raise ValueError("a selector must contain at least one criterion")
        unknown = fields.keys() - cls.__dataclass_fields__.keys()
        if unknown:
            raise ValueError(f"unknown selector criteria: {', '.join(sorted(unknown))}")
        options = fields.copy()
        for key in ("role", "name", "description", "accessible_id"):
            if key in options and (not isinstance(options[key], str) or not options[key]):
                raise ValueError(f"selector {key} must be a nonempty string")
        if "path" in options:
            path = options["path"]
            if not isinstance(path, list) or any(type(i) is not int or i < 0 for i in path):
                raise ValueError("selector path must contain nonnegative indices")
            options["path"] = tuple(path)
        if "interfaces" in options:
            interfaces = options["interfaces"]
            if not isinstance(interfaces, list) or not interfaces or any(
                not isinstance(value, str) or not value for value in interfaces
            ):
                raise ValueError("selector interfaces must contain names")
            options["interfaces"] = frozenset(interfaces)
        if "attributes" in options:
            attributes = options["attributes"]
            if not isinstance(attributes, dict) or not attributes or any(
                not isinstance(key, str) or not key or not isinstance(value, str)
                for key, value in attributes.items()
            ):
                raise ValueError("selector attributes must map names to strings")
            options["attributes"] = tuple(attributes.items())
        return cls(**options)

    def matches(self, node: Node) -> bool:
        return (
            all(expected is None or getattr(node, key) == expected for key, expected in (
                ("role", self.role), ("name", self.name), ("description", self.description),
                ("accessible_id", self.accessible_id),
            ))
            and (not self.interfaces or self.interfaces <= node.interfaces)
            and all(node.attributes.get(key) == value for key, value in self.attributes)
        )

    def find(self, root: Node) -> Iterator[Node]:
        if self.path is not None:
            node = root
            for index in self.path:
                node = node.child(index)
                if node is None:
                    return
            if self.matches(node):
                yield node
            return
        pending = [iter([root])]
        visited = 0
        while pending:
            node = next(pending[-1], None)
            if node is None:
                pending.pop()
                continue
            visited += 1
            if visited > 2500 or len(pending) > 60:
                raise ReadError("accessible selector search exceeded its bound")
            if self.matches(node):
                yield node
            pending.append(iter(node.children()))

    def one(self, root: Node, *, required: bool = False) -> Node | None:
        matches = self.find(root)
        result = next(matches, None)
        if next(matches, None) is not None:
            raise ReadError("ambiguous accessible selector")
        if result is None and required:
            raise ReadError("required accessible node unavailable")
        return result


@dataclass(frozen=True)
class Profile:
    package_version: str
    window: Selector
    chat_header: Selector
    message_list: Selector
    message_row: Selector
    input: Selector
    sender: Selector | None = None
    body: Selector | None = None
    image: Selector | None = None
    sticker: Selector | None = None
    link: Selector | None = None
    timestamp: Selector | None = None
    self_marker: Selector | None = None
    other: Selector | None = None
    non_message: Selector | None = None
    tail: Selector | None = None
    scrollbar: Selector | None = None
    group_count: Selector | None = None
    quote_preview: Selector | None = None
    timestamp_format: str | None = None
    timestamp_timezone: str | None = None
    sender_id_attribute: str | None = None
    source_id_attribute: str | None = None
    reverse: bool = False
    tail_from_last_row: bool = False
    tail_is_minimum: bool = False
    bubble_fallback: bool = True
    time_separator_height: int = 0
    image_minimum_row_height: int = 0
    recovery_step: float = 0
    recovery_wheel_ticks: int = 0
    recovery_pages: int = 4
    max_rows: int = 250

    @classmethod
    def parse(cls, fields: dict) -> Profile:
        if not isinstance(fields, dict):
            raise ValueError("AT-SPI profile must be a mapping")
        options = fields.copy()
        if "own" in options:
            options["self_marker"] = options.pop("own")
        required = {"package_version", "window", "chat_header", "message_list", "message_row", "input"}
        if required - options.keys():
            raise ValueError(f"missing profile settings: {', '.join(sorted(required - options.keys()))}")
        if options.keys() - cls.__dataclass_fields__.keys():
            raise ValueError("unrecognized setting in AT-SPI profile")
        if not isinstance(options["package_version"], str) or not options["package_version"]:
            raise ValueError("package_version must exactly identify the installed WeChat package")
        for key in ("window", "chat_header", "message_list", "message_row", "input", "sender",
                    "body", "image", "sticker", "link", "timestamp", "self_marker", "other",
                    "non_message", "tail", "scrollbar", "group_count", "quote_preview"):
            if key in options and (options[key] is not None or key in required):
                options[key] = Selector.parse(options[key])
        for key in ("reverse", "tail_from_last_row", "tail_is_minimum", "bubble_fallback"):
            if key in options and type(options[key]) is not bool:
                raise ValueError(f"{key} must be a boolean")
        for key, maximum in (("max_rows", 500), ("recovery_pages", 10)):
            if key in options and (type(options[key]) is not int or not 1 <= options[key] <= maximum):
                raise ValueError(f"{key} must be an integer between 1 and {maximum}")
        for key, maximum in (("time_separator_height", 2000), ("image_minimum_row_height", 2000),
                             ("recovery_wheel_ticks", 6)):
            if key in options and (type(options[key]) is not int or not 0 <= options[key] <= maximum):
                raise ValueError(f"{key} must be an integer between 0 and {maximum}")
        if "recovery_step" in options and (
            type(options["recovery_step"]) not in (int, float)
            or not math.isfinite(options["recovery_step"]) or options["recovery_step"] < 0
        ):
            raise ValueError("recovery_step must be finite and nonnegative")
        for key in ("timestamp_format", "timestamp_timezone", "sender_id_attribute", "source_id_attribute"):
            if options.get(key) is not None and (
                not isinstance(options[key], str) or not options[key]
            ):
                raise ValueError(f"{key} must be a nonempty string")
        if options.get("timestamp_timezone"):
            ZoneInfo(options["timestamp_timezone"])
        if options.get("timestamp_format") and any(
            part not in options["timestamp_format"] for part in ("%Y", "%m", "%d")
        ):
            raise ValueError("timestamp_format must include the original full date")
        return cls(**options)


_WECHAT_CHAT = [0, 0, 0, 1, 0, 0, 1, 0, 0, 0, 0, 1]
_WECHAT_TITLE = [*_WECHAT_CHAT, 0, 1, 0, 0, 0, 1, 1, 1, 0]
_WECHAT_QUOTE_PREVIEW = [*_WECHAT_CHAT, 1, 0, 2, 2, 1]
_WECHAT_ARM64_EN_PROFILE = {
    "package_version": "4.1.13.29",
    "window": {"role": "frame", "name": "Weixin", "path": [0]},
    "chat_header": {"role": "label", "path": [*_WECHAT_TITLE, 0]},
    "group_count": {"role": "label", "path": [*_WECHAT_TITLE, 1]},
    "message_list": {"role": "list", "name": "Messages", "path": [*_WECHAT_CHAT, 1, 1, 0, 0]},
    "message_row": {"role": "list item"},
    "input": {"role": "text", "interfaces": ["Text", "EditableText"],
              "path": [*_WECHAT_CHAT, 1, 0, 2, 2, 0, 0]},
    "body": {"path": []},
    "image": {"path": [], "name": "Image"},
    "image_minimum_row_height": 100,
    "time_separator_height": 41,
    "bubble_fallback": False,
    "tail_from_last_row": True,
    "recovery_wheel_ticks": 2,
    "recovery_pages": 6,
}


@dataclass(frozen=True)
class Target:
    pid: int
    window_bounds: Bounds
    input_bounds: Bounds


@dataclass
class _Chat:
    window: Node
    header: Node
    panel: Node
    input: Node
    scrollbar: Node | None
    pid: int


def _selected(root: Node, selector: Selector | None) -> Node | None:
    return selector.one(root) if selector else None


def wechat_quote_preview(window: Node) -> Node | None:
    panel = Selector(path=tuple(_WECHAT_QUOTE_PREVIEW), role="filler").one(window)
    return panel if panel is not None and panel.showing else None


def _wechat_login_state(window: Node) -> str | None:
    bounds = window.bounds
    if bounds is None or bounds[2:] != (280, 380):
        return None
    confirmation = Selector(role="label", name="Confirm on Phone")
    if any(node.showing for node in confirmation.find(window)):
        return "login_confirmation_required"
    buttons = Selector(role="push button")
    labels = {node.name for node in buttons.find(window) if node.showing}
    if {"Log In", "Switch Account"} <= labels:
        return "login_required"
    return None


def _label(node: Node | None) -> str | None:
    if node is None:
        return None
    try:
        return node.label
    except Exception:
        LOG.debug("An optional accessible Text value could not be read", exc_info=True)
        return node.name or node.description or None


def _original_time(raw: str | None, profile: Profile) -> datetime | None:
    if not raw:
        return None
    raw = raw.strip()
    try:
        if profile.timestamp_format:
            stamp = datetime.strptime(raw, profile.timestamp_format)
        elif re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", raw):
            stamp = datetime.fromisoformat(raw)
        else:
            return None
        if stamp.tzinfo:
            return stamp
        if profile.timestamp_timezone:
            zone = ZoneInfo(profile.timestamp_timezone)
            localized = stamp.replace(tzinfo=zone)
            if localized.utcoffset() == stamp.replace(tzinfo=zone, fold=1).utcoffset():
                return localized
    except ValueError:
        return None
    return None


def _http_url(value: str) -> bool:
    try:
        parts = urlsplit(value)
        return parts.scheme.lower() in {"http", "https"} and bool(parts.hostname)
    except ValueError:
        return False


def urls_in(text: str) -> tuple[str, ...]:
    links = []
    for match in URL_PATTERN.finditer(text):
        url = match[0].rstrip(".'\",;:!?")
        for closing, opening in ((")", "("), ("]", "["), ("}", "{")):
            while url.endswith(closing) and url.count(closing) > url.count(opening):
                url = url[:-1]
        if _http_url(url):
            links.append(url)
    return tuple(dict.fromkeys(links))


def parse_message(
    row: Node, profile: Profile, chat: str, observed_at: datetime,
) -> tuple[Message, Node | None]:
    sender = _selected(row, profile.sender)
    body = _selected(row, profile.body)
    sticker = _selected(row, profile.sticker)
    media = sticker or _selected(row, profile.image)
    if media is not None and sticker is None and profile.image_minimum_row_height:
        bounds = row.bounds
        if bounds is None or bounds[3] < profile.image_minimum_row_height:
            media = None
    link = _selected(row, profile.link)
    timestamp = _selected(row, profile.timestamp)
    owns = _selected(row, profile.self_marker) is not None
    other = _selected(row, profile.other) is not None
    if owns and other:
        raise ReadError("conflicting message ownership markers")

    raw_time = _label(timestamp)
    raw_desc = row.description or _label(row)
    text = _label(body)
    if link is not None and not text:
        text = _label(link)
    links = urls_in(text) if text else ()
    if link is not None:
        try:
            link_urls = (url for url in link.uris if _http_url(url))
            links = tuple(dict.fromkeys((*link_urls, *links)))
        except Exception:
            LOG.debug("An optional accessible Hyperlink value could not be read", exc_info=True)
    kind = "unknown"
    if media is not None:
        kind = "sticker" if sticker is not None else "image"
    elif link is not None:
        kind = "link"
    elif text:
        kind = "text"

    metadata: dict[str, Any] = {}
    if raw_desc:
        metadata["raw_description"] = raw_desc
    if kind == "unknown":
        metadata["accessible_role"] = row.role
        if not raw_desc:
            descriptions = []
            for index, child in enumerate(row.children()):
                if index >= 12:
                    break
                if label := _label(child):
                    descriptions.append(label[:512])
            if descriptions:
                metadata["raw_description"] = "\n".join(descriptions)
    if raw_time:
        metadata["accessible_timestamp"] = raw_time
    if links:
        metadata["urls"] = list(links)
    if media is not None:
        metadata["capture_quality"] = "unavailable"
    return Message(
        id=None, chat=chat, sender=_label(sender),
        sender_id=(sender or row).attributes.get(profile.sender_id_attribute)
        if profile.sender_id_attribute else None,
        type=kind, text=text, media_path=None, url=links[0] if links else None,
        timestamp=_original_time(raw_time, profile), observed_at=observed_at,
        is_self=True if owns else False if other else None, source="accessibility",
        source_id=row.attributes.get(profile.source_id_attribute) if profile.source_id_attribute else None,
        metadata=metadata,
    ), media


class _Unavailable(ReadError):
    def __init__(self, state: str):
        self.state = state
        super().__init__(state)


def _scroll_state(scrollbar: Node | None) -> tuple[Any, float, float, float] | None:
    if scrollbar is None or (value := scrollbar.value) is None:
        return None
    minimum = value.get_minimum_value()
    maximum = value.get_maximum_value()
    current = value.get_current_value()
    if not all(math.isfinite(item) for item in (minimum, maximum, current)):
        return None
    if minimum <= current <= maximum:
        return value, current, minimum, maximum
    return None


def _active_x11(connection, window_bounds: Bounds, pid: int) -> tuple[Any, int] | None:
    from Xlib import X

    root = connection.screen().root
    prop = root.get_full_property(connection.intern_atom("_NET_ACTIVE_WINDOW"), X.AnyPropertyType)
    if prop is None or len(prop.value) != 1 or not prop.value[0]:
        return None
    window = int(prop.value[0])
    active = connection.create_resource_object("window", window)
    prop = active.get_full_property(connection.intern_atom("_NET_WM_PID"), X.AnyPropertyType)
    if prop is None or len(prop.value) != 1 or int(prop.value[0]) != pid:
        return None
    geometry = active.get_geometry()
    position = root.translate_coords(active, 0, 0)
    if (position.x, position.y, geometry.width, geometry.height) != window_bounds:
        return None
    return root, window


def _unobscured_x11(connection, root, window_id: int, bounds: Bounds) -> bool:
    from Xlib import X

    window = connection.create_resource_object("window", window_id)
    for _ in range(12):
        parent = window.query_tree().parent
        if parent.id == root.id:
            break
        window = parent
    else:
        return False
    windows = root.query_tree().children  # XQueryTree returns bottom to top.
    stack = [child.id for child in windows]
    if window.id not in stack:
        return False
    for sibling in windows[stack.index(window.id) + 1:]:
        attributes = sibling.get_attributes()
        if attributes.map_state != X.IsViewable or attributes.win_class != X.InputOutput:
            continue
        geometry = sibling.get_geometry()
        position = root.translate_coords(sibling, 0, 0)
        if intersection(bounds, (position.x, position.y, geometry.width, geometry.height)):
            return False
    return True


def _capture_pixels(
    bounds: Bounds, window_bounds: Bounds, pid: int, *, window_id: int | None = None,
) -> tuple[Any, Bounds] | None:
    from Xlib import display
    from mss import MSS
    from PIL import Image

    connection = display.Display()
    try:
        active = _active_x11(connection, window_bounds, pid)
        if active is None:
            return None
        root, window = active
        if window_id is not None and window != window_id:
            return None
        geometry = root.get_geometry()
        clipped = intersection(bounds, (0, 0, geometry.width, geometry.height))
        if clipped is None or not _unobscured_x11(connection, root, window, clipped):
            return None
        with MSS() as capture:
            x, y, w, h = clipped
            shot = capture.grab({"left": x, "top": y, "width": w, "height": h})
            next_active = _active_x11(connection, window_bounds, pid)
            if (next_active is None or next_active[1] != window
                    or not _unobscured_x11(connection, root, window, clipped)):
                return None
        return Image.frombytes("RGB", shot.size, shot.rgb), clipped
    finally:
        connection.close()


def _click_from_window(
    point: tuple[int, int], window: Bounds, pid: int, button: int = 1, *, window_id: int | None = None,
) -> bool:
    from Xlib import X, display
    from Xlib.ext import xtest

    x, y = point
    if intersection((x, y, 1, 1), window) != (x, y, 1, 1):
        return False
    connection = display.Display()
    try:
        active = _active_x11(connection, window, pid)
        if (active is None or (window_id is not None and active[1] != window_id)
                or not _unobscured_x11(connection, *active, (x, y, 1, 1))):
            return False
        xtest.fake_input(connection, X.MotionNotify, x=x, y=y)
        xtest.fake_input(connection, X.ButtonPress, button)
        xtest.fake_input(connection, X.ButtonRelease, button)
        connection.sync()
        return True
    finally:
        connection.close()


def _click_popup(point: tuple[int, int], popup: Bounds, window: Bounds, pid: int) -> bool:
    from Xlib import X, display
    from Xlib.ext import xtest

    if _click_from_window(point, popup, pid):
        return True
    x, y = point
    if intersection((x, y, 1, 1), popup) != (x, y, 1, 1):
        return False
    connection = display.Display()
    try:
        active = _active_x11(connection, window, pid)
        if active is None:
            return False
        root, _ = active
        # An override-redirect menu can be above the main window while the
        # window manager still reports the main window as active.
        for sibling in reversed(root.query_tree().children):
            attributes = sibling.get_attributes()
            if attributes.map_state != X.IsViewable or attributes.win_class != X.InputOutput:
                continue
            geometry = sibling.get_geometry()
            position = root.translate_coords(sibling, 0, 0)
            bounds = (position.x, position.y, geometry.width, geometry.height)
            if intersection(bounds, (x, y, 1, 1)) is None:
                continue
            if bounds != popup or not attributes.override_redirect:
                return False
            xtest.fake_input(connection, X.MotionNotify, x=x, y=y)
            xtest.fake_input(connection, X.ButtonPress, 1)
            xtest.fake_input(connection, X.ButtonRelease, 1)
            connection.sync()
            return True
        return False
    finally:
        connection.close()


def _box_center(bounds: Bounds, box: tuple[int, int, int, int]) -> tuple[int, int]:
    return bounds[0] + (box[0] + box[2]) // 2, bounds[1] + (box[1] + box[3]) // 2


def _escape_from_window(window: Bounds, pid: int, *, window_id: int | None = None) -> bool:
    from Xlib import X, XK, display
    from Xlib.ext import xtest

    connection = display.Display()
    try:
        active = _active_x11(connection, window, pid)
        if active is None or (window_id is not None and active[1] != window_id):
            return False
        code = connection.keysym_to_keycode(XK.string_to_keysym("Escape"))
        if not code:
            return False
        xtest.fake_input(connection, X.KeyPress, code)
        xtest.fake_input(connection, X.KeyRelease, code)
        connection.sync()
        return True
    finally:
        connection.close()


def _scroll_wheel(chat: _Chat, button: int, ticks: int) -> bool:
    from Xlib import display

    window_bounds, panel_bounds = chat.window.bounds, chat.panel.bounds
    if panel_bounds is None or intersection(window_bounds, panel_bounds) != panel_bounds:
        return False
    connection = display.Display()
    try:
        active = _active_x11(connection, window_bounds, chat.pid)
        if active is None:
            return False
        root, _window = active
        pointer = root.query_pointer()
        geometry = root.get_geometry()
        bounded = intersection(panel_bounds, (0, 0, geometry.width, geometry.height))
        if bounded is None:
            return False
        x, y, width, height = bounded
        try:
            subprocess.run([
                "xdotool", "mousemove", str(x + width // 2), str(y + height // 2),
                "click", "--repeat", str(ticks), str(button),
            ], check=True, timeout=3, capture_output=True)
        finally:
            try:
                subprocess.run(["xdotool", "mousemove", str(pointer.root_x), str(pointer.root_y)],
                               check=True, timeout=3, capture_output=True)
            except (OSError, subprocess.SubprocessError):
                LOG.debug("Cannot restore the pointer after scrolling", exc_info=True)
        return True
    finally:
        connection.close()


def _save_media(directory: Path, contents: bytes) -> str:
    filename = hashlib.sha256(contents).hexdigest() + ".png"
    path = directory / filename
    directory.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        descriptor, temporary = tempfile.mkstemp(dir=directory)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(contents)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return filename


def _merge_history(older: tuple[Message, ...], newer: tuple[Message, ...]) -> tuple[Message, ...] | None:
    previous, current = tuple(map(message_key, older)), tuple(map(message_key, newer))
    for length in range(min(len(previous), len(current)), 0, -1):
        anchor = previous[-length:]
        if anchor != current[:length]:
            continue
        if sum(previous[i:i + length] == anchor for i in range(len(previous) - length + 1)) != 1:
            return None
        if sum(current[i:i + length] == anchor for i in range(len(current) - length + 1)) != 1:
            return None
        if len(set(anchor)) >= 2 or all(msg.source_id for msg in newer[:length]):
            return older[:-length] + newer
        return None
    return None


class AccessibilitySource:
    """Use the event bus to wake a fixed interval snapshot reader."""

    def __init__(
        self, chat: str, *, profile: dict | str | None = None, poll_interval: float = 0.5,
        media_dir: Path | None = None, desktop_lock: asyncio.Lock | None = None,
        lookup_sender_names: bool = False, native_image_viewer: bool = False,
    ) -> None:
        if not math.isfinite(poll_interval) or poll_interval <= 0:
            raise ValueError("poll_interval must be finite and positive")
        self.chat = chat
        self._preset = profile if isinstance(profile, str) else None
        if self._preset is not None:
            if self._preset != "wechat-4.1.13.29-arm64-en":
                raise ValueError("unknown AT-SPI profile preset")
            profile = _WECHAT_ARM64_EN_PROFILE
        self.profile = Profile.parse(profile) if profile else None
        self.lookup_sender_names = lookup_sender_names and self._preset is not None
        self.native_image_viewer = native_image_viewer and self._preset is not None
        self.poll_interval = poll_interval
        self.media_dir = media_dir
        self.desktop_lock = desktop_lock if desktop_lock is not None else asyncio.Lock()
        self._executor: ThreadPoolExecutor | None = None
        self._pump: asyncio.Task | None = None
        self._changed = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._atspi: Any = None
        self._context: Any = None
        self._listener: Any = None
        self._registered: list[str] = []
        self._version: str | None = None
        self._state = "starting"
        self._at_tail = False
        self._app_pid: int | None = None
        self._last_read: Snapshot | None = None
        self._capture_origin: tuple[Snapshot, tuple[Any, ...]] | None = None

    async def start(self) -> None:
        if self._executor is not None:
            return
        self._state = "starting"
        self._loop = asyncio.get_running_loop()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="atspi")
        await self._call(self._open)
        if self._registered:
            self._pump = asyncio.create_task(self._pump_events())

    async def stop(self) -> None:
        if self._pump is not None:
            self._pump.cancel()
            await asyncio.gather(self._pump, return_exceptions=True)
            self._pump = None
        if self._executor is not None:
            await self._call(self._close)
            self._executor.shutdown(wait=True)
            self._executor = None
        self._state = "stopped"
        self._at_tail = False
        self._last_read = None
        self._capture_origin = None

    async def _call(self, function, *args):
        if self._executor is None or self._loop is None:
            raise RuntimeError("accessibility source has not started")
        # Wait for a cancelled caller's in-progress GI operation before its desktop lock is released.
        pending = self._loop.run_in_executor(self._executor, function, *args)
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:
            await asyncio.gather(pending, return_exceptions=True)
            raise

    def _open(self) -> None:
        self._version = client_version()
        try:
            atspi, glib = runtime()
            if atspi.init() not in (0, 1):
                raise ReadError("AT-SPI session initialization failed")
            atspi.set_timeout(900, 900)
            self._atspi = atspi
            self._context = glib.MainContext.default()
        except (ImportError, ValueError, ReadError, RuntimeError) as error:
            LOG.warning("Cannot initialize the AT-SPI session: %s", error)
            self._state = "atspi_unavailable"
            return
        try:
            self._listener = self._atspi.EventListener.new(self._on_event)
            for event in EVENT_TYPES:
                if self._listener.register(event):
                    self._registered.append(event)
        except Exception as error:
            LOG.warning("Cannot subscribe to all AT-SPI events. Polling remains active: %s", error)

    def _close(self) -> None:
        if self._listener is not None:
            for event in self._registered:
                try:
                    self._listener.deregister(event)
                except Exception:
                    LOG.debug("AT-SPI listener deregistration failed", exc_info=True)
        self._registered.clear()
        self._listener = None
        if self._atspi is not None:
            self._atspi.exit()
        self._atspi = self._context = None

    def _on_event(self, event, *_data) -> None:
        if self._loop is None or self._app_pid is None or event.source is None:
            return
        try:
            if event.source.get_process_id() == self._app_pid:
                self._loop.call_soon_threadsafe(self._changed.set)
        except Exception:
            LOG.debug("AT-SPI event source disappeared", exc_info=True)

    def _iterate_events(self) -> None:
        for _ in range(32):
            if not self._context.pending():
                break
            self._context.iteration(False)

    async def _pump_events(self) -> None:
        while True:
            try:
                await self._call(self._iterate_events)
            except Exception:
                LOG.debug("AT-SPI event dispatch failed", exc_info=True)
            await asyncio.sleep(0.05)

    async def wait_for_change(self, timeout: float | None = None) -> None:
        try:
            await asyncio.wait_for(self._changed.wait(), self.poll_interval if timeout is None else timeout)
        except asyncio.TimeoutError:
            pass
        self._changed.clear()

    async def get_status(self) -> dict:
        return {
            "source": "accessibility", "state": self._state, "ready": self._state == "ready",
            "chat": self.chat, "wechat_version": self._version, "events": bool(self._registered),
            "at_tail": self._at_tail,
        }

    def _chat(self) -> _Chat:
        if self._atspi is None:
            raise _Unavailable("atspi_unavailable")
        candidates = [node for node in desktop_nodes(self._atspi) if wechat_app(node)]
        if len(candidates) != 1:
            self._app_pid = None
            raise _Unavailable("wechat_not_accessible")
        app = candidates[0]
        self._app_pid = app.native.get_process_id()
        if not self._app_pid:
            raise _Unavailable("wechat_not_accessible")
        profile = self.profile
        if profile is None:
            raise _Unavailable("profile_required")
        if profile.package_version != self._version:
            raise _Unavailable("profile_version_mismatch")
        if self._preset and platform.machine() not in {"aarch64", "arm64"}:
            raise _Unavailable("profile_architecture_mismatch")

        window = profile.window.one(app)
        if window is None or not window.showing:
            raise _Unavailable("window_unavailable")
        if self._preset and (login := _wechat_login_state(window)):
            raise _Unavailable(login)
        header = profile.chat_header.one(window)
        if header is None:
            raise _Unavailable("profile_mismatch")
        self._verify_header(header)
        if profile.group_count is not None:
            group_count = profile.group_count.one(window)
            if group_count is None or not group_count.showing or not re.fullmatch(r"\(\d+\)", group_count.name):
                raise _Unavailable("profile_mismatch")
        panel = profile.message_list.one(window)
        input_field = profile.input.one(window)
        if panel is None or input_field is None or not panel.showing or not input_field.showing:
            raise _Unavailable("profile_mismatch")
        return _Chat(window, header, panel, input_field, _selected(window, profile.scrollbar), self._app_pid)

    def _verify_header(self, header: Node) -> None:
        header.refresh()
        if not header.showing:
            raise _Unavailable("profile_mismatch")
        title = header.text or header.name
        if not title or title.strip() != self.chat:
            raise _Unavailable("chat_mismatch")

    def _verify_chat(self, before: _Chat) -> None:
        after = self._chat()
        if before.pid != after.pid or any(
            getattr(before, name).native != getattr(after, name).native
            for name in ("window", "header", "panel", "input")
        ):
            raise ReadError("active chat controls changed during collection")

    def _tail(self, chat: _Chat, last_row: Node | None) -> bool:
        profile = self.profile
        if profile.tail is not None:
            marker = profile.tail.one(chat.window)
            if marker is not None and marker.showing:
                return True
        scroll = _scroll_state(chat.scrollbar)
        if scroll is None:
            if profile.tail_from_last_row and last_row is not None and last_row.showing:
                panel, row = chat.panel.bounds, last_row.bounds
                return bool(intersection(panel, row) and row[1] + row[3] <= panel[1] + panel[3])
            return False
        _value, current, minimum, maximum = scroll
        endpoint = minimum if profile.tail_is_minimum else maximum
        return math.isclose(current, endpoint, rel_tol=0, abs_tol=0.001)

    async def get_target(self) -> Target | None:
        return await self._call(self._target)

    def _target(self) -> Target | None:
        try:
            chat = self._chat()
            window_bounds = chat.window.bounds
            input_bounds = chat.input.bounds
            if input_bounds is None or intersection(window_bounds, input_bounds) != input_bounds:
                return None
            self._verify_chat(chat)
            return Target(chat.pid, window_bounds, input_bounds)
        except Exception:
            return None

    async def focus_input(self) -> bool:
        return await self._call(self._focus_input)

    def _focus_input(self) -> bool:
        try:
            chat = self._chat()
            if "Component" not in chat.input.interfaces:
                return False
            if not chat.input.native.grab_focus():
                return False
            for _ in range(5):
                if self._input_focused():
                    return True
                time.sleep(0.05)
            return False
        except Exception:
            return False

    async def input_focused(self) -> bool:
        return await self._call(self._input_focused)

    def _input_focused(self) -> bool:
        try:
            field = self._chat().input
            field.refresh()
            return field.showing and "focused" in field.states
        except Exception:
            return False

    async def read_input(self) -> str | None:
        return await self._call(self._read_input)

    def _read_input(self) -> str | None:
        try:
            chat = self._chat()
            chat.input.refresh()
            return chat.input.text
        except Exception:
            return None

    def inspect_profile(self, atspi: Any, package_version: str | None) -> dict:
        """Check the profile in an initialized diagnostic session without changing the UI."""
        self._atspi, self._version = atspi, package_version
        result = {
            "state": "unconfigured", "at_tail": False, "visible_rows": 0, "message_types": [],
            "x11_target_active": False, "composer": "unverified", "quote_preview": "unverified",
            "manual_send_preconditions": False,
        }
        if not self.chat:
            return result
        try:
            chat = self._chat()
            snapshot, _ = self._read_chat(chat)
        except _Unavailable as error:
            return {**result, "state": error.state}
        except Exception:
            return {**result, "state": "stale_nodes"}
        result.update(state="ready" if snapshot.at_tail else "reading_history", at_tail=snapshot.at_tail,
                      visible_rows=len(snapshot.messages), message_types=sorted({m.type for m in snapshot.messages}))

        try:
            chat.input.refresh()
            value = chat.input.text
            if value is not None:
                result["composer"] = "empty" if value == "" else "occupied"
            result["quote_preview"] = self._quote_preview_state(chat.window)
        except Exception:
            LOG.debug("Cannot inspect composer preconditions", exc_info=True)

        target = self._target()
        if target is not None:
            try:
                from Xlib import display

                connection = display.Display()
                try:
                    result["x11_target_active"] = _active_x11(connection, target.window_bounds, target.pid) is not None
                finally:
                    connection.close()
            except Exception:
                LOG.debug("Cannot inspect the active X11 target", exc_info=True)
        result["manual_send_preconditions"] = (
            result["at_tail"] and result["x11_target_active"]
            and result["composer"] == "empty" and result["quote_preview"] == "clear"
        )
        return result

    async def read_snapshot(self, *, capture_media: bool = True) -> Snapshot:
        if capture_media and self.media_dir is not None:
            async with self.desktop_lock:
                return await self._call(self._snapshot, True)
        return await self._call(self._snapshot, False)

    def _snapshot(self, capture_media: bool) -> Snapshot:
        try:
            chat = self._chat()
            snapshot, parsed = self._read_chat(chat)
            if capture_media:
                snapshot = replace(snapshot, messages=self._with_media(parsed, chat))
            self._capture_origin = (snapshot, tuple(row.native for row, _, _ in parsed))
            self._state = "ready" if snapshot.at_tail else "reading_history"
            self._at_tail = snapshot.at_tail
            return snapshot
        except _Unavailable as error:
            self._state = error.state
        except Exception:
            LOG.debug("Discarded an inconsistent AT-SPI snapshot", exc_info=True)
            self._state = "stale_nodes"
        self._at_tail, self._capture_origin = False, None
        return Snapshot(chat=self.chat, messages=(), at_tail=False, ready=False)

    def _read_chat(self, chat: _Chat) -> tuple[Snapshot, list[tuple[Node, Message, Node | None]]]:
        profile = self.profile
        chat.panel.refresh()
        rows = list(profile.message_row.find(chat.panel))
        if profile.reverse:
            rows.reverse()
        at_tail = self._tail(chat, rows[-1] if rows else None)
        rows = [row for row in rows if row.showing][-profile.max_rows:]
        observed_at = datetime.now(timezone.utc)
        parsed = []
        separator = None
        for row in rows:
            if _selected(row, profile.non_message) is not None:
                continue
            if profile.time_separator_height and re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", row.name):
                bounds = row.bounds
                if bounds is not None and bounds[3] == profile.time_separator_height:
                    separator = row.name
                    continue
            message, media = parse_message(row, profile, self.chat, observed_at)
            if separator is not None:
                message = replace(message, metadata={**message.metadata, "preceding_time_label": separator})
            parsed.append((row, message, media))
        self._verify_chat(chat)
        messages = tuple(message for _, message, _ in parsed)
        return Snapshot(chat=self.chat, messages=messages, at_tail=at_tail), parsed

    async def capture_messages(self, snapshot: Snapshot, indices: Iterable[int]) -> tuple[Message, ...]:
        selected = tuple(indices)
        unchanged = tuple(snapshot.messages[index] for index in selected)
        if not unchanged or (
            self._preset is None
            and (not self.media_dir or not any(message.type in {"image", "sticker"} for message in unchanged))
        ):
            return unchanged
        from .desktop import pause_vnc_input

        async with self.desktop_lock:
            try:
                async with pause_vnc_input():
                    return await self._call(self._capture_messages, snapshot, selected)
            except Exception:
                LOG.debug("Cannot enrich the new rows", exc_info=True)
                return unchanged

    async def quote_clear(self) -> bool:
        return await self._call(self._quote_clear)

    def _quote_clear(self) -> bool:
        state = self._quote_preview_state(self._chat().window)
        if state == "unverified":
            raise ReadError("The custom profile could not verify the quote-preview state")
        return state == "clear"

    def _quote_preview_state(self, window: Node) -> str:
        if self._preset is not None:
            return "clear" if wechat_quote_preview(window) is None else "present"
        selector = self.profile.quote_preview if self.profile is not None else None
        if selector is None:
            return "unverified"
        panel = selector.one(window)
        if panel is None:
            return "unverified"
        panel.refresh()
        states = panel.states
        if not states or "defunct" in states:
            return "unverified"
        return "present" if states & {"showing", "visible"} else "clear"

    async def quote_matches(self, text: str) -> bool:
        if self._preset is None:
            return False
        return await self._call(self._quote_matches, text)

    def _quote_matches(self, text: str) -> bool:
        from .desktop import accessible_text

        panel = wechat_quote_preview(self._chat().window)
        if panel is None:
            return False
        label = Selector(path=(0, 1, 0), role="label").one(panel)
        if label is None or not label.showing:
            return False
        preview = accessible_text(label.name)
        return preview == accessible_text(text) or preview.endswith(": " + accessible_text(text))

    async def select_quote(self, snapshot: Snapshot, index: int, *, allow_own: bool = False) -> bool:
        if self._preset is None:
            return False
        return await self._call(self._select_quote, snapshot, index, allow_own)

    def _select_quote(self, snapshot: Snapshot, index: int, allow_own: bool) -> bool:
        from .wechat_actions import find_quote_action
        from .wechat_layout import analyze_message_row

        chat = self._chat()
        latest, parsed = self._read_chat(chat)
        if (not latest.at_tail or not self._quote_clear()
                or tuple(map(message_key, snapshot.messages)) != tuple(map(message_key, latest.messages))):
            return False
        row, message, _ = parsed[index]
        bounds, window = row.bounds, chat.window.bounds
        if (message.type != "text" or bounds is None or window is None
                or intersection(bounds, chat.panel.bounds) != bounds):
            return False
        captured = _capture_pixels(bounds, window, chat.pid)
        if captured is None or captured[1] != bounds:
            return False
        layout = analyze_message_row(captured[0], "text")
        if (layout.is_self is None or (layout.is_self and not allow_own)
                or layout.bubble_box is None):
            return False
        self._verify_chat(chat)
        row.refresh()
        if row.bounds != bounds or row.name != message.metadata.get("raw_description"):
            return False
        parent = chat.window.native.get_parent()
        if parent is None:
            return False
        app = Node(parent, self._atspi)
        point = _box_center(bounds, layout.bubble_box)
        if not _click_from_window(point, window, chat.pid, button=3):
            return False
        for _ in range(8):
            time.sleep(0.06)
            action = find_quote_action(app, chat.window, point)
            if action is None:
                continue
            popup, item = action
            popup_bounds, item_bounds = popup.bounds, item.bounds
            if popup_bounds is None or item_bounds is None:
                return False
            item_point = (item_bounds[0] + item_bounds[2] // 2, item_bounds[1] + item_bounds[3] // 2)
            if not _click_popup(item_point, popup_bounds, window, chat.pid):
                _escape_from_window(popup_bounds, chat.pid)
                return False
            for _ in range(8):
                time.sleep(0.06)
                if self._quote_matches(message.text):
                    return True
            return False
        return False

    def _capture_messages(self, snapshot: Snapshot, indices: tuple[int, ...]) -> tuple[Message, ...]:
        original = tuple(snapshot.messages[index] for index in indices)
        provenance = self._capture_origin
        if provenance is None or provenance[0] is not snapshot or not snapshot.at_tail:
            return original
        chat = self._chat()
        latest, parsed = self._read_chat(chat)
        if not latest.at_tail or tuple(map(message_key, snapshot.messages)) != tuple(map(message_key, latest.messages)):
            return original
        if provenance[1] != tuple(row.native for row, _, _ in parsed):
            return original
        selected = [(parsed[index][0], message, parsed[index][2]) for index, message in zip(indices, original)]
        return self._with_media(
            selected, chat, lookup_names=self.lookup_sender_names, open_native_images=self.native_image_viewer,
        )

    def _with_media(
        self, parsed: list[tuple[Node, Message, Node | None]], chat: _Chat, *, lookup_names: bool = False,
        open_native_images: bool = False,
    ) -> tuple[Message, ...]:
        messages = [message for _, message, _ in parsed]
        visual_layout = self._preset is not None
        if not visual_layout and not any(media is not None for _, _, media in parsed):
            return tuple(messages)
        window_bounds = chat.window.bounds
        panel_bounds = intersection(window_bounds, chat.panel.bounds)
        if panel_bounds is None or window_bounds is None:
            return tuple(messages)

        crops = []
        for index, (row, _, media) in enumerate(parsed):
            if media is None:
                continue
            try:
                candidate = None
                quality = "image_bounds"
                if media.native != row.native and media.showing:
                    try:
                        candidate = media.image_bounds
                    except Exception:
                        LOG.debug("Cannot read image bounds", exc_info=True)
                if candidate is None:
                    if not self.profile.bubble_fallback:
                        continue
                    candidate = row.bounds
                    quality = "bubble_bounds"
                area = intersection(candidate, panel_bounds)
                if area is not None:
                    crops.append((index, row, media, candidate, area, quality))
            except Exception:
                LOG.debug("Cannot locate accessible media on screen", exc_info=True)
        row_bounds = [row.bounds for row, _, _ in parsed] if visual_layout else []
        if not crops and not row_bounds:
            return tuple(messages)

        try:
            captured = _capture_pixels(panel_bounds, window_bounds, chat.pid)
        except Exception:
            LOG.debug("Cannot capture accessible desktop pixels", exc_info=True)
            return tuple(messages)
        if captured is None:
            return tuple(messages)
        image, screen_area = captured
        self._verify_chat(chat)
        if panel_bounds != intersection(chat.window.bounds, chat.panel.bounds):
            return tuple(messages)

        if visual_layout:
            from .wechat_layout import analyze_message_row, image_preview_box

            lookups = 0
            for index, ((row, message, _), bounds) in enumerate(zip(parsed, row_bounds)):
                if bounds is None or row.bounds != bounds or intersection(bounds, screen_area) != bounds:
                    continue
                x, y = bounds[0] - screen_area[0], bounds[1] - screen_area[1]
                row_image = image.crop((x, y, x + bounds[2], y + bounds[3]))
                layout = analyze_message_row(row_image, message.type)
                if layout.is_self is not None and message.is_self is None:
                    messages[index] = replace(
                        message, is_self=layout.is_self,
                        metadata={
                            **message.metadata, "ownership_basis": "wechat_avatar_side",
                            "observation_key": message_key(message),
                        },
                    )
                if self.media_dir is not None:
                    native_crop = None
                    if open_native_images and message.type == "image":
                        box = image_preview_box(row_image, layout)
                        if box is not None:
                            try:
                                native_crop = self._capture_native_image(chat, row, bounds, box)
                            except Exception:
                                LOG.debug("Could not verify the native photo viewer", exc_info=True)
                    if native_crop is not None:
                        messages[index] = self._write_crop(messages[index], native_crop, "wechat_viewer_window")
                    elif layout.media_box is not None:
                        messages[index] = self._write_crop(
                            messages[index], row_image.crop(layout.media_box), "wechat_bubble_estimate",
                        )
                if (lookup_names and lookups < 2 and layout.is_self is False
                        and layout.avatar_box is not None and messages[index].sender is None
                        and layout.bubble_box is not None):
                    lookups += 1
                    try:
                        nickname = self._lookup_group_alias(chat, row, bounds, layout.avatar_box)
                    except Exception:
                        LOG.debug("Could not verify the member's native group alias", exc_info=True)
                        break
                    if nickname is not None:
                        enriched = messages[index]
                        messages[index] = replace(
                            enriched, sender=nickname,
                            metadata={**enriched.metadata, "sender_basis": "wechat_profile_popup"},
                        )

        for index, row, media, candidate, area, quality in crops:
            try:
                if messages[index].metadata.get("capture_quality") == "wechat_viewer_window":
                    continue
                # Discard crops from a row that moved while the desktop was being captured.
                current_bounds = row.bounds if quality == "bubble_bounds" else media.image_bounds
                if current_bounds != candidate:
                    continue
                clipped = intersection(area, screen_area)
                if clipped is None:
                    continue
                x, y = clipped[0] - screen_area[0], clipped[1] - screen_area[1]
                crop = image.crop((x, y, x + clipped[2], y + clipped[3]))
                if self.media_dir is not None:
                    messages[index] = self._write_crop(
                        messages[index], crop, quality + ("_clipped" if clipped != candidate else "")
                    )
            except Exception:
                LOG.debug("Cannot write accessible media file", exc_info=True)
        return tuple(messages)

    def _capture_native_image(self, chat: _Chat, row: Node, bounds: Bounds, box) -> Any:
        from Xlib import X, display

        from .wechat_images import find_image_viewer

        self._verify_chat(chat)
        row.refresh()
        chat.input.refresh()
        window = chat.window.bounds
        if (window is None or row.bounds != bounds or row.name != "Image"
                or intersection(bounds, chat.panel.bounds) != bounds or chat.input.text != ""):
            return None
        parent = chat.window.native.get_parent()
        if parent is None:
            return None
        app = Node(parent, self._atspi)
        visible = [node for node in app.children() if node.showing]
        if len(visible) != 1 or visible[0].native != chat.window.native:
            return None

        connection = display.Display()
        try:
            main = _active_x11(connection, window, chat.pid)
            if main is None or not _click_from_window(_box_center(bounds, box), window, chat.pid):
                return None
            for _ in range(12):
                time.sleep(0.07)
                viewer = find_image_viewer(app, chat.window)
                if viewer is None:
                    continue
                viewer_bounds = viewer.window.bounds
                if viewer_bounds is None:
                    return None
                active = _active_x11(connection, viewer_bounds, chat.pid)
                if active is None or active[1] == main[1]:
                    continue
                viewer_id = active[1]
                capture = None
                try:
                    native = connection.create_resource_object("window", viewer_id)
                    transient = native.get_wm_transient_for()
                    types = native.get_full_property(connection.intern_atom("_NET_WM_WINDOW_TYPE"), X.AnyPropertyType)
                    if (native.get_wm_class() == ("wechat", "wechat")
                            and (transient is None or transient.id == main[1])
                            and types is not None
                            and connection.intern_atom("_NET_WM_WINDOW_TYPE_NORMAL") in types.value):
                        capture = _capture_pixels(viewer_bounds, viewer_bounds, chat.pid, window_id=viewer_id)
                finally:
                    _escape_from_window(viewer_bounds, chat.pid, window_id=viewer_id)
                    for _ in range(5):
                        time.sleep(0.07)
                        restored = _active_x11(connection, window, chat.pid)
                        if restored is not None and restored[1] == main[1]:
                            break
                    else:
                        close = viewer.close.bounds
                        if close is not None and intersection(viewer_bounds, close) == close:
                            x, y, width, height = close
                            _click_from_window((x + width // 2, y + height // 2), viewer_bounds, chat.pid,
                                               window_id=viewer_id)
                            time.sleep(0.14)
                restored = _active_x11(connection, window, chat.pid)
                if restored is None or restored[1] != main[1]:
                    return None
                self._verify_chat(chat)
                return capture[0] if capture is not None and capture[1] == viewer_bounds else None
            return None
        finally:
            connection.close()

    def _lookup_group_alias(self, chat: _Chat, row: Node, bounds: Bounds, avatar_box) -> str | None:
        from .wechat_actions import identify_profile_popup

        self._verify_chat(chat)
        chat.input.refresh()
        if chat.input.text != "" or row.bounds != bounds or chat.window.bounds is None:
            return None
        parent = chat.window.native.get_parent()
        if parent is None:
            return None
        app = Node(parent, self._atspi)
        point = _box_center(bounds, avatar_box)
        if not _click_from_window(point, chat.window.bounds, chat.pid):
            return None
        for _ in range(8):
            time.sleep(0.06)
            popup = identify_profile_popup(app, chat.window, point)
            if popup is None:
                continue
            popup_bounds = popup.node.bounds
            if popup_bounds is None or not _escape_from_window(popup_bounds, chat.pid):
                return None
            for _ in range(8):
                time.sleep(0.06)
                if not any(node.showing and node.native == popup.node.native for node in app.children()):
                    self._verify_chat(chat)
                    return popup.nickname
            return None
        return None

    def _write_crop(self, message: Message, crop: Any, quality: str) -> Message:
        try:
            buffer = io.BytesIO()
            crop.save(buffer, format="PNG")
            filename = _save_media(self.media_dir, buffer.getvalue())
        except OSError:
            LOG.debug("Cannot write accessible media file", exc_info=True)
            return message
        return replace(
            message, media_path=filename, metadata={**message.metadata, "capture_quality": quality},
        )

    async def read_recent(self, limit: int) -> list[Message]:
        if limit <= 0:
            return []
        return list((await self.read_snapshot(capture_media=False)).messages[-limit:])

    async def read_new(self) -> list[Message]:
        snapshot = await self.read_snapshot(capture_media=False)
        if not snapshot.ready or not snapshot.at_tail:
            return []
        previous = self._last_read
        if previous is None or not previous.messages:
            self._last_read = snapshot
            return list(await self.capture_messages(snapshot, range(len(snapshot.messages))))
        from .sync import match_tail

        match = match_tail(tuple(map(message_key, previous.messages)), tuple(map(message_key, snapshot.messages)))
        if match.state != "matched":
            return []
        self._last_read = snapshot
        return list(await self.capture_messages(snapshot, range(match.end, len(snapshot.messages))))

    async def recover(self, previous: Snapshot, current: Snapshot) -> Snapshot | None:
        profile = self.profile
        if not profile or not (profile.recovery_step or profile.recovery_wheel_ticks) or not current.at_tail:
            return None
        from .desktop import pause_vnc_input

        async with self.desktop_lock:
            try:
                async with pause_vnc_input():
                    return await self._call(self._recover, previous, current)
            except Exception:
                LOG.debug("Cannot recover accessible history", exc_info=True)
                return None

    def _recover(self, previous: Snapshot, current: Snapshot) -> Snapshot | None:
        profile = self.profile
        chat = self._chat()
        verified, _ = self._read_chat(chat)
        if not verified.at_tail or not current.messages or not verified.messages:
            return None
        if tuple(map(message_key, current.messages)) != tuple(map(message_key, verified.messages)):
            return None
        merged = current.messages
        moved = False
        try:
            for _ in range(profile.recovery_pages):
                self._verify_header(chat.header)
                moved = True
                if not self._scroll_older(chat):
                    return None
                time.sleep(0.2)
                self._iterate_events()
                chat = self._chat()
                page, _ = self._read_chat(chat)
                if not page.messages or page.at_tail:
                    return None
                candidate = _merge_history(page.messages, merged)
                if candidate is None:
                    return None
                merged = candidate
                if _merge_history(previous.messages, merged) is not None:
                    return replace(current, messages=merged)
        finally:
            if moved:
                self._restore_tail()
        return None

    def _scroll_older(self, chat: _Chat) -> bool:
        profile = self.profile
        if profile.recovery_wheel_ticks:
            return _scroll_wheel(chat, 4, profile.recovery_wheel_ticks)
        scroll = _scroll_state(chat.scrollbar)
        if scroll is None:
            return False
        value, position, minimum, maximum = scroll
        direction = 1 if profile.tail_is_minimum else -1
        next_position = min(maximum, max(minimum, position + direction * profile.recovery_step))
        return next_position != position and bool(value.set_current_value(next_position))

    def _restore_tail(self) -> None:
        profile = self.profile
        if profile.recovery_wheel_ticks:
            for _ in range(profile.recovery_pages + 2):
                chat = self._chat()
                snapshot, _ = self._read_chat(chat)
                if snapshot.at_tail:
                    return
                if not _scroll_wheel(chat, 5, profile.recovery_wheel_ticks):
                    break
                time.sleep(0.2)
            else:
                snapshot, _ = self._read_chat(self._chat())
                if snapshot.at_tail:
                    return
        else:
            scroll = _scroll_state(self._chat().scrollbar)
            if scroll is not None:
                value, _, minimum, maximum = scroll
                if value.set_current_value(minimum if profile.tail_is_minimum else maximum):
                    return
        raise ReadError("could not restore the target chat to its tail")
