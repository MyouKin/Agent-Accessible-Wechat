"""Pixel geometry for fully visible message rows in the verified WeChat light layout."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from PIL import Image, ImageChops

Box = tuple[int, int, int, int]  # Pillow coordinates: left, top, right, bottom.

_BACKGROUND = (250, 250, 250)
_AVATAR_TOP = 8
_AVATAR_SIZE = 36
_LEFT_AVATAR_MARGIN = 20
_RIGHT_AVATAR_MARGIN = 22


@dataclass(frozen=True)
class RowLayout:
    is_self: bool | None = None
    avatar_box: Box | None = None
    bubble_box: Box | None = None
    media_box: Box | None = None


def _contrast(image: Image.Image, color: tuple[int, int, int]) -> Image.Image:
    red, green, blue = ImageChops.difference(image, Image.new("RGB", image.size, color)).split()
    return ImageChops.add(ImageChops.add(red, green), blue)


def _fraction(mask: Image.Image, box: Box) -> float:
    width, height = box[2] - box[0], box[3] - box[1]
    return mask.crop(box).histogram()[255] / (width * height)


def _avatar_fraction(mask: Image.Image, box: Box) -> float:
    x0, y0, x1, y1 = box
    return _fraction(mask, (x0 + 2, y0 + 2, x1 - 2, y1 - 2))


def _has_avatar_shape(mask: Image.Image, box: Box) -> bool:
    x0, y0, x1, y1 = box
    center_x, center_y = (x0 + x1) // 2, (y0 + y1) // 2
    sections = (
        (x0 + 2, y0 + 2, center_x, center_y),
        (center_x, y0 + 2, x1 - 2, center_y),
        (x0 + 2, center_y, center_x, y1 - 2),
        (center_x, center_y, x1 - 2, y1 - 2),
    )
    return all(_fraction(mask, patch) >= 0.40 for patch in sections)


def _spans(counts: list[int], minimum: int, gap: int = 2) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for index, count in enumerate(counts):
        if count < minimum:
            continue
        if spans and index - spans[-1][1] <= gap:
            spans[-1] = spans[-1][0], index + 1
        else:
            spans.append((index, index + 1))
    return spans


def _rectangular_region(mask: Image.Image, *, minimum_width: int, minimum_height: int) -> Box | None:
    width, height = mask.size
    data = mask.tobytes()
    counts = [data[x::width].count(255) for x in range(width)]
    candidates = [(start, end) for start, end in _spans(counts, max(8, height // 10))
                  if end - start >= minimum_width]
    if len(candidates) != 1:
        return None
    left, right = candidates[0]
    row_counts = [data[y * width + left:y * width + right].count(255) for y in range(height)]
    vertical = [(start, end) for start, end in _spans(row_counts, max(7, (right - left) // 8), gap=5)
                if end - start >= minimum_height]
    if len(vertical) != 1:
        return None
    top, bottom = vertical[0]
    box = left, top, right, bottom
    return box if _fraction(mask, box) >= 0.55 else None


def _text_mask(image: Image.Image) -> list[Image.Image]:
    colors = Counter({color: count for count, color in image.getcolors(image.width * image.height)})
    minimum_size = 220
    return [
        _contrast(image, color).point(lambda value: 255 if value == 0 else 0)
        for color, count in colors.most_common(8)
        if color != _BACKGROUND and min(color) >= 80 and count >= minimum_size
    ]


def _bubble(row: Image.Image, is_self: bool, kind: str, *, allow_cutoff: bool = False) -> Box | None:
    width, height = row.size
    left_edge, right_edge = 62, width - 64
    media = kind in {"image", "sticker"}
    top_edge = 5 if is_self else 28 if media else 24
    content = row.crop((left_edge, top_edge, right_edge, height - 4))
    if kind == "text":
        masks = _text_mask(content)
        minimum_width, minimum_height = 24, 23
    elif media:
        masks = [_contrast(content, _BACKGROUND).point(lambda value: 255 if value > 9 else 0)]
        minimum_width, minimum_height = 42, 45
    else:
        return None
    results: set[Box] = set()
    for mask in masks:
        bounds = _rectangular_region(mask, minimum_width=minimum_width, minimum_height=minimum_height)
        if bounds is None:
            continue
        left, top, right, bottom = bounds
        horizontal_gap = content.width - right if is_self else left
        if horizontal_gap > 25 or top > 18:
            continue
        # A rectangle touching the edge of the search area may be clipped or include another control.
        if (left == 0 or right == content.width or bottom == content.height
                or (media and top == 0 and not allow_cutoff)):
            continue
        results.add((left + left_edge, top + top_edge, right + left_edge, bottom + top_edge))
    return next(iter(results)) if len(results) == 1 else None


def image_preview_box(row: Image.Image, layout: RowLayout) -> Box | None:
    """Locate an interior click region even if the nickname cutoff clips its top."""
    if layout.is_self is None or layout.avatar_box is None:
        return None
    return layout.media_box or _bubble(row, layout.is_self, "image", allow_cutoff=True)


def analyze_message_row(row: Image.Image, kind: str) -> RowLayout:
    """Locate layout landmarks without reading or retaining avatar content."""
    width, height = row.size
    if row.mode != "RGB" or not 300 <= width <= 2000 or not 48 <= height <= 800:
        return RowLayout()
    contrast = _contrast(row, _BACKGROUND)
    foreground = contrast.point(lambda value: 255 if value > 24 else 0)
    outer = ((4, 6, 16, 45), (width - 16, 6, width - 4, 45))
    if any(_fraction(foreground, patch) > 0.08 for patch in outer):
        return RowLayout()

    top, bottom = _AVATAR_TOP, _AVATAR_TOP + _AVATAR_SIZE
    left = (_LEFT_AVATAR_MARGIN, top, _LEFT_AVATAR_MARGIN + _AVATAR_SIZE, bottom)
    right = (width - _RIGHT_AVATAR_MARGIN - _AVATAR_SIZE, top, width - _RIGHT_AVATAR_MARGIN, bottom)
    occupied = [_avatar_fraction(foreground, avatar) for avatar in (left, right)]
    if occupied[0] >= 0.62 and occupied[1] <= 0.04:
        is_self, avatar = False, left
    elif occupied[1] >= 0.62 and occupied[0] <= 0.04:
        is_self, avatar = True, right
    else:
        return RowLayout()
    if not _has_avatar_shape(foreground, avatar):
        return RowLayout()
    bubble = _bubble(row, is_self, kind)
    return RowLayout(is_self=is_self, avatar_box=avatar, bubble_box=bubble,
                     media_box=bubble if kind in {"image", "sticker"} else None)
