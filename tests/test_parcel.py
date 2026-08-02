from PIL import Image, ImageDraw

from parcel import (
    _critical_intervals,
    _manual_box,
    _normalized_box,
    build_roll,
    choose_cuts,
    snap_label_box,
)


def synthetic_label() -> Image.Image:
    label = Image.new("RGB", (1_300, 1_700), "white")
    draw = ImageDraw.Draw(label)
    draw.rectangle((0, 0, 1_299, 1_699), outline="black", width=5)
    for y in (552, 956, 1_344):
        draw.line((0, y, 1_299, y), fill="black", width=5)
    draw.rectangle((780, 600, 1_150, 880), fill="black")
    draw.rectangle((120, 1_390, 1_180, 1_620), fill="black")
    return label


def test_suggested_safe_lines_build_four_full_scale_bands() -> None:
    label = synthetic_label()
    analysis = {
        "suggested_cuts_y": [325, 562, 791],
        "critical_regions": [
            {"kind": "qr", "box": {"x0": 600, "y0": 350, "x1": 900, "y1": 535}},
            {"kind": "barcode", "box": {"x0": 90, "y0": 815, "x1": 920, "y1": 960}},
        ],
    }

    cuts = choose_cuts(label, analysis)
    boundaries = (0, *cuts, label.height)
    assert len(cuts) == 3
    assert all(
        end - start <= 554 for start, end in zip(boundaries, boundaries[1:], strict=False)
    )
    widths = [
        end - start for start, end in zip(boundaries, boundaries[1:], strict=False)
    ]
    assert min(widths) >= int(label.height / 4 * 0.70)
    assert not any(590 <= cut <= 920 for cut in cuts)
    assert not any(1_375 <= cut <= 1_640 for cut in cuts)

    roll, heights = build_roll(label, cuts)
    assert roll.size == (554, 5_254)
    assert len(heights) == 4


def test_detected_box_snaps_to_a_strong_label_frame() -> None:
    page = Image.new("RGB", (1_600, 1_130), "white")
    draw = ImageDraw.Draw(page)
    draw.rectangle((1_000, 100, 1_550, 1_020), outline="black", width=5)
    raw = {"x0": 630, "y0": 95, "x1": 970, "y1": 895}

    snapped = snap_label_box(page, raw)

    assert abs(snapped.x0 - 1_000) <= 5
    assert abs(snapped.y0 - 100) <= 5
    assert abs(snapped.x1 - 1_550) <= 5
    assert abs(snapped.y1 - 1_020) <= 5


def test_critical_regions_follow_quarter_turn_rotation() -> None:
    analysis = {
        "critical_regions": [
            {"kind": "qr", "box": {"x0": 200, "y0": 50, "x1": 400, "y1": 350}}
        ]
    }

    start, end = _critical_intervals(analysis, 1_000, rotation=90)[0]

    assert start < 200
    assert end > 400


def test_bad_model_hints_cannot_split_a_dense_code_or_exceed_roll_width() -> None:
    label = Image.new("RGB", (1_305, 1_706), "white")
    draw = ImageDraw.Draw(label)
    draw.rectangle((0, 0, 1_304, 1_705), outline="black", width=5)
    for y in (492, 634, 956, 1_150, 1_331):
        draw.line((0, y, 1_304, y), fill="black", width=5)
    # Dense matrix-code stand-in. The deliberately bad model hint crosses it.
    for y in range(590, 930, 12):
        for x in range(880, 1_220, 12):
            if (x // 12 + y // 12) % 3:
                draw.rectangle((x, y, x + 7, y + 7), fill="black")
    analysis = {
        "suggested_cuts_y": [288, 372, 674],
        "critical_regions": [],
    }

    cuts = choose_cuts(label, analysis)
    boundaries = (0, *cuts, label.height)
    widths = [
        end - start for start, end in zip(boundaries, boundaries[1:], strict=False)
    ]

    assert len(cuts) == 3
    assert not any(580 <= cut <= 940 for cut in cuts)
    assert max(widths) <= 554


def test_roll_uses_tallest_band_scale_and_equal_fitted_lengths() -> None:
    label = Image.new("RGB", (300, 700), "black")

    roll, widths = build_roll(label, (100, 500))

    assert roll.size == (554, 936)
    assert tuple(round(width, 1) for width in widths) == (25.4, 25.4, 25.4)
    assert roll.getpixel((50, 318)) == 0
    assert roll.getpixel((50, 617)) == 0
    assert roll.getpixel((50, 636)) == 0
    assert roll.getpixel((50, 935)) == 0


def test_oversized_bands_are_scaled_to_the_physical_roll_width() -> None:
    label = Image.new("RGB", (300, 1_500), "black")

    roll, widths = build_roll(label, (750,))

    assert roll.size == (554, 462)
    assert tuple(round(width, 1) for width in widths) == (18.8, 18.8)


def test_one_pixel_crop_is_accepted_for_manual_and_detected_boxes() -> None:
    crop = {"x0": 0, "y0": 0, "x1": 1, "y1": 1}

    assert _manual_box(crop, 1_000, 1_000).width == 1
    assert _normalized_box(crop, 1_000, 1_000).height == 1
