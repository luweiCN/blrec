from typing import Tuple

from blrec.vainglory.vision import (
    PixelRect,
    RgbFrame,
    ViewportTransform,
    detect_gameplay_hud,
    detect_result_layout,
    detect_result_layouts,
    extract_result_heroes,
    hamming_distance,
    hero_fingerprint,
    perceptual_hash,
    png_bytes,
)


def fill(
    pixels: bytearray, width: int, rect: PixelRect, color: Tuple[int, int, int]
) -> None:
    for y in range(rect.top, rect.bottom):
        for x in range(rect.left, rect.right):
            offset = (y * width + x) * 3
            pixels[offset : offset + 3] = bytes(color)


def synthetic_result_frame(*, left_teal: bool, winner_teal: bool) -> RgbFrame:
    width, height = 960, 540
    pixels = bytearray(bytes((45, 55, 45)) * width * height)
    fill(pixels, width, PixelRect(90, 120, 870, 420), (12, 15, 15))
    teal = (26, 112, 116)
    orange = (112, 55, 31)
    fill(pixels, width, PixelRect(105, 165, 410, 365), teal if left_teal else orange)
    fill(pixels, width, PixelRect(550, 165, 855, 365), orange if left_teal else teal)
    fill(
        pixels,
        width,
        PixelRect(460, 132, 500, 155),
        (75, 210, 215) if winner_teal else (225, 125, 65),
    )
    for left, right in ((105, 250), (260, 405), (555, 700), (710, 855)):
        fill(pixels, width, PixelRect(left, 379, right, 382), (230, 230, 230))
        fill(pixels, width, PixelRect(left, 410, right, 413), (230, 230, 230))
        fill(pixels, width, PixelRect(left, 379, left + 3, 413), (230, 230, 230))
        fill(pixels, width, PixelRect(right - 3, 379, right, 413), (230, 230, 230))
    return RgbFrame(width, height, bytes(pixels))


def responsive_source_frame(
    reference: RgbFrame, *, width: int, height: int, viewport: ViewportTransform
) -> RgbFrame:
    pixels = bytearray(width * height * 3)
    for target_y in range(height):
        source_y = min(
            reference.height - 1,
            int(
                (viewport.top + target_y / height * viewport.height) * reference.height
            ),
        )
        for target_x in range(width):
            source_x = min(
                reference.width - 1,
                int(
                    (viewport.left + target_x / width * viewport.width)
                    * reference.width
                ),
            )
            source = (source_y * reference.width + source_x) * 3
            target = (target_y * width + target_x) * 3
            pixels[target : target + 3] = reference.pixels[source : source + 3]
    return RgbFrame(width, height, bytes(pixels))


def test_result_layout_maps_text_color_to_the_matching_team() -> None:
    teal_left = detect_result_layout(
        synthetic_result_frame(left_teal=True, winner_teal=True)
    )
    orange_left = detect_result_layout(
        synthetic_result_frame(left_teal=False, winner_teal=False)
    )

    assert teal_left is not None
    assert teal_left.left_color == 'teal'
    assert teal_left.right_color == 'orange'
    assert teal_left.winner_side == 'left'
    assert orange_left is not None
    assert orange_left.left_color == 'orange'
    assert orange_left.right_color == 'teal'
    assert orange_left.winner_side == 'left'


def test_result_layout_adapts_to_phone_and_tablet_viewports() -> None:
    reference = synthetic_result_frame(left_teal=False, winner_teal=True)
    responsive = ViewportTransform(
        name='fixture-responsive',
        left=0.08,
        top=0.10,
        width=0.84,
        height=0.80,
        ocr_profile='wide',
    )

    phone = detect_result_layout(
        responsive_source_frame(reference, width=1200, height=540, viewport=responsive)
    )
    tablet = detect_result_layout(
        responsive_source_frame(reference, width=720, height=540, viewport=responsive)
    )

    assert phone is not None
    assert phone.winner_color == 'teal'
    assert phone.winner_side == 'right'
    assert phone.viewport.ocr_profile == 'wide'
    assert tablet is not None
    assert tablet.winner_color == 'teal'
    assert tablet.winner_side == 'right'


def test_result_layout_uses_content_candidates_inside_a_16_by_9_recording() -> None:
    reference = synthetic_result_frame(left_teal=False, winner_teal=True)
    responsive = ViewportTransform(
        name='fixture-responsive',
        left=138 / 1920,
        top=93 / 1080,
        width=1597 / 1920,
        height=866 / 1080,
        ocr_profile='wide',
    )
    letterboxed_capture = responsive_source_frame(
        reference, width=960, height=540, viewport=responsive
    )

    layouts = detect_result_layouts(letterboxed_capture)

    assert any(layout.viewport.ocr_profile == 'wide' for layout in layouts)


def test_hero_crops_use_the_layout_viewport() -> None:
    reference = synthetic_result_frame(left_teal=True, winner_teal=True)
    pixels = bytearray(reference.pixels)
    fill(pixels, reference.width, PixelRect(437, 193, 449, 205), (220, 20, 180))
    reference = RgbFrame(reference.width, reference.height, bytes(pixels))
    responsive = ViewportTransform(
        name='fixture-responsive',
        left=0.08,
        top=0.10,
        width=0.84,
        height=0.80,
        ocr_profile='wide',
    )
    source = responsive_source_frame(
        reference, width=1200, height=540, viewport=responsive
    )

    heroes = extract_result_heroes(source, viewport=responsive)

    assert len(heroes) == 6
    assert all((hero.frame.width, hero.frame.height) == (96, 96) for hero in heroes)
    center = (48 * 96 + 48) * 3
    assert heroes[0].frame.pixels[center : center + 3] == bytes((220, 20, 180))


def test_result_layout_rejects_a_plain_gameplay_frame() -> None:
    frame = RgbFrame(960, 540, bytes((45, 70, 45)) * 960 * 540)

    assert detect_result_layout(frame) is None


def test_result_layout_rejects_an_in_game_scoreboard() -> None:
    frame = synthetic_result_frame(left_teal=True, winner_teal=True)
    pixels = bytearray(frame.pixels)
    fill(pixels, frame.width, PixelRect(460, 132, 500, 155), (220, 220, 220))
    fill(pixels, frame.width, PixelRect(435, 132, 455, 155), (75, 210, 215))

    assert (
        detect_result_layout(RgbFrame(frame.width, frame.height, bytes(pixels))) is None
    )


def test_result_layout_rejects_scoreboard_with_only_a_surrender_action() -> None:
    frame = synthetic_result_frame(left_teal=True, winner_teal=True)
    pixels = bytearray(frame.pixels)
    fill(pixels, frame.width, PixelRect(260, 379, 405, 413), (12, 15, 15))

    assert (
        detect_result_layout(RgbFrame(frame.width, frame.height, bytes(pixels))) is None
    )


def test_result_layout_recognizes_low_contrast_result_actions() -> None:
    frame = synthetic_result_frame(left_teal=True, winner_teal=True)
    pixels = bytearray(frame.pixels)
    for left, right in ((105, 250), (260, 405), (555, 700), (710, 855)):
        fill(pixels, frame.width, PixelRect(left, 379, right, 382), (35, 35, 35))
        fill(pixels, frame.width, PixelRect(left, 410, right, 413), (35, 35, 35))
        fill(pixels, frame.width, PixelRect(left, 379, left + 3, 413), (35, 35, 35))
        fill(pixels, frame.width, PixelRect(right - 3, 379, right, 413), (35, 35, 35))

    assert (
        detect_result_layout(RgbFrame(frame.width, frame.height, bytes(pixels)))
        is not None
    )


def test_result_layout_rejects_gameplay_hud_with_result_theme_colors() -> None:
    frame = synthetic_result_frame(left_teal=False, winner_teal=True)
    pixels = bytearray(frame.pixels)
    for index, center in enumerate((0.365, 0.415, 0.465, 0.55, 0.60, 0.65)):
        rect = PixelRect(
            int((center - 0.021) * frame.width),
            0,
            int((center + 0.021) * frame.width),
            int(0.075 * frame.height),
        )
        fill(pixels, frame.width, rect, (35, 170, 210) if index % 2 else (180, 60, 45))
        fill(
            pixels,
            frame.width,
            PixelRect(rect.left, rect.top, (rect.left + rect.right) // 2, rect.bottom),
            (5, 5, 5),
        )
    fill(pixels, frame.width, PixelRect(465, 8, 475, 25), (225, 225, 225))
    gameplay = RgbFrame(frame.width, frame.height, bytes(pixels))

    assert detect_gameplay_hud(gameplay) is not None
    assert detect_result_layout(gameplay) is None


def test_result_layout_recognizes_dark_defeat_text() -> None:
    frame = synthetic_result_frame(left_teal=False, winner_teal=False)
    pixels = bytearray(frame.pixels)
    fill(pixels, frame.width, PixelRect(460, 132, 500, 155), (96, 20, 40))

    layout = detect_result_layout(RgbFrame(frame.width, frame.height, bytes(pixels)))

    assert layout is not None
    assert layout.winner_color == 'orange'
    assert layout.winner_side == 'left'


def test_gameplay_hud_requires_portraits_and_a_timer() -> None:
    width, height = 960, 540
    pixels = bytearray(bytes((15, 20, 15)) * width * height)
    for index, center in enumerate((0.365, 0.415, 0.465, 0.55, 0.60, 0.65)):
        rect = PixelRect(
            int((center - 0.021) * width),
            0,
            int((center + 0.021) * width),
            int(0.075 * height),
        )
        fill(pixels, width, rect, (35, 170, 210) if index % 2 else (180, 60, 45))
        fill(
            pixels,
            width,
            PixelRect(rect.left, rect.top, (rect.left + rect.right) // 2, rect.bottom),
            (5, 5, 5),
        )
    without_timer = RgbFrame(width, height, bytes(pixels))
    fill(pixels, width, PixelRect(465, 8, 475, 25), (225, 225, 225))
    with_timer = RgbFrame(width, height, bytes(pixels))

    assert detect_gameplay_hud(without_timer) is None
    assert detect_gameplay_hud(with_timer) is not None


def test_result_hero_crops_follow_both_sides_and_three_slots() -> None:
    heroes = extract_result_heroes(
        synthetic_result_frame(left_teal=True, winner_teal=True)
    )

    assert [(hero.side, hero.slot) for hero in heroes] == [
        ('left', 1),
        ('left', 2),
        ('left', 3),
        ('right', 1),
        ('right', 2),
        ('right', 3),
    ]
    assert all((hero.frame.width, hero.frame.height) == (96, 96) for hero in heroes)


def test_result_hero_crops_can_search_a_nearby_layout_offset() -> None:
    frame = synthetic_result_frame(left_teal=True, winner_teal=True)
    pixels = bytearray(frame.pixels)
    separator = int(round(frame.width * 0.52))
    left_center = separator - int(round(frame.width * 0.039))
    right_center = separator + int(round(frame.width * 0.039))
    fill(
        pixels,
        frame.width,
        PixelRect(left_center - 20, 182, left_center + 20, 222),
        (220, 20, 180),
    )
    fill(
        pixels,
        frame.width,
        PixelRect(right_center - 20, 182, right_center + 20, 222),
        (30, 220, 90),
    )

    heroes = extract_result_heroes(
        RgbFrame(frame.width, frame.height, bytes(pixels)), center_shift=0.02
    )

    left_middle = (48 * 96 + 48) * 3
    assert heroes[0].frame.pixels[left_middle : left_middle + 3] == bytes(
        (220, 20, 180)
    )
    assert heroes[3].frame.pixels[left_middle : left_middle + 3] == bytes((30, 220, 90))


def test_hero_fingerprint_ignores_team_rings_and_level_overlays() -> None:
    width = height = 96
    first = bytearray(bytes((5, 120, 150)) * width * height)
    second = bytearray(bytes((170, 60, 20)) * width * height)
    for y in range(16, 80):
        for x in range(16, 80):
            color = ((x * 3) % 256, (y * 5) % 256, ((x + y) * 2) % 256)
            offset = (y * width + x) * 3
            first[offset : offset + 3] = bytes(color)
            second[offset : offset + 3] = bytes(color)

    assert hero_fingerprint(RgbFrame(width, height, bytes(first))) == hero_fingerprint(
        RgbFrame(width, height, bytes(second))
    )


def test_hero_fingerprint_stays_close_after_resolution_crop_shift() -> None:
    width = height = 96
    fingerprints = []
    for shift in (0, 4):
        pixels = bytearray(bytes((20, 30, 40)) * width * height)
        for y in range(12, 84):
            for x in range(12, 84):
                source_x = x - shift
                color = (
                    (source_x * 7 + y * 3) % 256,
                    (source_x * 2 + y * 11) % 256,
                    (source_x * 13 + y * 5) % 256,
                )
                offset = (y * width + x) * 3
                pixels[offset : offset + 3] = bytes(color)
        fingerprints.append(hero_fingerprint(RgbFrame(width, height, bytes(pixels))))

    assert hamming_distance(*fingerprints) <= 16


def test_crop_hash_and_png_are_deterministic() -> None:
    width, height = 18, 8
    pixels = bytearray()
    for _y in range(height):
        for x in range(width):
            value = (x if x < 9 else 17 - x) * 20
            pixels.extend((value, value, value))
    frame = RgbFrame(width, height, bytes(pixels))
    left = frame.crop(PixelRect(0, 0, 9, 8))
    right = frame.crop(PixelRect(9, 0, 18, 8))

    assert perceptual_hash(left) == perceptual_hash(left)
    assert perceptual_hash(left) != perceptual_hash(right)
    assert png_bytes(left).startswith(b'\x89PNG\r\n\x1a\n')
