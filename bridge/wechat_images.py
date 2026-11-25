"""Identify the photo window observed in the official 4.1.13.29 client."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .accessibility import Node


@dataclass(frozen=True, slots=True)
class ImageViewer:
    window: Node
    close: Node


def find_image_viewer(app: Node, main: Node) -> ImageViewer | None:
    from .accessibility import Selector, intersection, wechat_app

    if not wechat_app(app):
        return None
    visible = [node for node in app.children() if node.showing]
    others = [node for node in visible if node.native != main.native]
    if len(visible) != 2 or len(others) != 1:
        return None
    viewer = others[0]
    bounds = viewer.bounds
    if (viewer.role != "frame" or viewer.name != "Photos and Videos"
            or "active" not in viewer.states or bounds is None):
        return None

    controls = {}
    for path, name in (((0, 1, 3), "Close"), ((0, 0, 0, 7, 0), "Zoom"),
                       ((0, 0, 0, 2, 0), "Previous"), ((0, 0, 0, 3, 0), "Next")):
        control = Selector(path=path, role="push button", name=name).one(viewer)
        if control is None or not control.showing or control.bounds is None:
            return None
        if intersection(bounds, control.bounds) != control.bounds:
            return None
        controls[name] = control
    return ImageViewer(viewer, controls["Close"])
