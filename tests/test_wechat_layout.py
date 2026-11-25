from types import SimpleNamespace

from PIL import Image, ImageDraw

from bridge.accessibility import AccessibilitySource
from bridge.models import Message, message_key
from bridge.wechat_layout import RowLayout, analyze_message_row, image_preview_box


def paint(image, box, color):
    left, top, right, bottom = box
    ImageDraw.Draw(image).rectangle((left, top, right - 1, bottom - 1), fill=color)


def test_left_avatar_and_white_text_bubble_are_distinct():
    row = Image.new("RGB", (578, 75), "#fafafa")
    paint(row, (20, 8, 56, 44), "#84726d")
    paint(row, (68, 12, 151, 19), "#636363")  # Nickname, above the bubble.
    paint(row, (68, 28, 260, 65), "white")
    paint(row, (82, 41, 244, 44), "#161616")

    assert analyze_message_row(row, "text") == RowLayout(False, (20, 8, 56, 44), (68, 28, 260, 65))


def test_right_avatar_and_green_text_bubble_are_distinct():
    row = Image.new("RGB", (578, 57), "#fafafa")
    paint(row, (520, 8, 556, 44), "#324c78")
    paint(row, (326, 8, 509, 47), "#95ec69")
    paint(row, (343, 23, 494, 28), "#171717")

    assert analyze_message_row(row, "text") == RowLayout(True, (520, 8, 556, 44), (326, 8, 509, 47))


def test_image_crop_excludes_both_avatar_and_nickname():
    row = Image.new("RGB", (578, 156), "#fafafa")
    paint(row, (20, 8, 56, 44), "#84726d")
    paint(row, (68, 12, 151, 19), "#636363")
    paint(row, (69, 32, 235, 144), "#efd0f0")
    paint(row, (84, 54, 223, 85), "#345293")

    layout = analyze_message_row(row, "image")
    assert layout.avatar_box == (20, 8, 56, 44)
    assert layout.media_box == layout.bubble_box == (69, 32, 235, 144)
    assert row.crop(layout.media_box).size == (166, 112)


def test_native_click_can_use_visible_interior_of_a_photo_touching_name_cutoff():
    row = Image.new("RGB", (578, 156), "#fafafa")
    paint(row, (20, 8, 56, 44), "#84726d")
    paint(row, (69, 26, 235, 144), "#efd0f0")
    layout = analyze_message_row(row, "image")
    assert layout.media_box is None
    assert image_preview_box(row, layout) == (69, 28, 235, 144)


def test_photo_enrichment_falls_back_to_bubble_when_native_viewer_is_unverified(monkeypatch, tmp_path):
    bounds = (100, 50, 578, 156)
    chat = SimpleNamespace(pid=42, panel=SimpleNamespace(bounds=bounds),
                           window=SimpleNamespace(bounds=(0, 0, 880, 640)))
    row = SimpleNamespace(bounds=bounds)
    message = Message(chat="fixture", type="image", text="Image")
    pixels = Image.new("RGB", (578, 156), "#fafafa")
    paint(pixels, (20, 8, 56, 44), "#84726d")
    paint(pixels, (69, 32, 235, 144), "#efd0f0")
    source = AccessibilitySource("fixture", profile="wechat-4.1.13.29-arm64-en", media_dir=tmp_path,
                                 native_image_viewer=True)
    monkeypatch.setattr("bridge.accessibility._capture_pixels", lambda *_: (pixels, bounds))
    monkeypatch.setattr(source, "_verify_chat", lambda _chat: None)
    native = Image.new("RGB", (825, 855), "#323232")
    for image, quality, size in ((native, "wechat_viewer_window", native.size),
                                 (None, "wechat_bubble_estimate", (166, 112))):
        monkeypatch.setattr(source, "_capture_native_image", lambda *_: image)
        enriched, = source._with_media([(row, message, None)], chat, open_native_images=True)
        assert enriched.metadata["capture_quality"] == quality
        with Image.open(tmp_path / enriched.media_path) as stored:
            assert stored.size == size


def test_layout_rejects_no_avatar_both_avatars_and_unknown_background():
    row = Image.new("RGB", (578, 75), "#fafafa")
    assert analyze_message_row(row, "text") == RowLayout()
    paint(row, (20, 8, 56, 44), "#84726d")
    paint(row, (520, 8, 556, 44), "#324c78")
    assert analyze_message_row(row, "text") == RowLayout()
    dark = Image.new("RGB", (578, 75), "#222222")
    paint(dark, (520, 8, 556, 44), "#b4c6dd")
    assert analyze_message_row(dark, "text") == RowLayout()


def test_ambiguous_media_keeps_avatar_without_cropping_a_message():
    row = Image.new("RGB", (578, 156), "#fafafa")
    paint(row, (20, 8, 56, 44), "#84726d")
    paint(row, (73, 31, 144, 110), "#4f6d94")
    paint(row, (230, 31, 344, 110), "#9a8446")
    assert analyze_message_row(row, "image") == RowLayout(False, (20, 8, 56, 44))


def test_quote_abstains_when_two_filled_rectangles_share_the_avatar_lane():
    row = Image.new("RGB", (578, 130), "#fafafa")
    paint(row, (20, 8, 56, 44), "#84726d")
    paint(row, (68, 28, 220, 61), "white")
    paint(row, (68, 70, 220, 115), "white")
    assert analyze_message_row(row, "text") == RowLayout(False, (20, 8, 56, 44))


def test_source_uses_one_viewport_and_preserves_keys_when_enriching_full_rows(monkeypatch):
    source = AccessibilitySource("fixture", profile="wechat-4.1.13.29-arm64-en")
    panel_bounds = (100, 50, 578, 140)
    chat = SimpleNamespace(
        pid=42, panel=SimpleNamespace(bounds=panel_bounds), window=SimpleNamespace(bounds=(0, 0, 880, 640)),
    )
    viewport = Image.new("RGB", (578, 140), "#fafafa")
    paint(viewport, (520, 8, 556, 44), "#324c78")
    paint(viewport, (20, 65, 56, 101), "#84726d")
    rows = (
        SimpleNamespace(bounds=(100, 50, 578, 57)),
        SimpleNamespace(bounds=(100, 107, 578, 75)),
        SimpleNamespace(bounds=(100, 182, 578, 75)),
    )
    observations = tuple(Message(chat="fixture", type="text", text=str(i)) for i in range(3))
    calls = []

    def capture(bounds, window, pid):
        calls.append((bounds, window, pid))
        return viewport, panel_bounds

    monkeypatch.setattr("bridge.accessibility._capture_pixels", capture)
    monkeypatch.setattr(source, "_verify_chat", lambda chat: None)
    enriched = source._with_media([(row, message, None) for row, message in zip(rows, observations)], chat)

    assert calls == [(panel_bounds, chat.window.bounds, 42)]
    assert tuple(message.is_self for message in enriched) == (True, False, None)
    assert tuple(map(message_key, enriched)) == tuple(map(message_key, observations))
