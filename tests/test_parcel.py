from PIL import Image, ImageDraw

from parcel import _critical_intervals, build_roll, choose_cuts, snap_label_box


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
    assert all(
        abs(actual - expected) <= 6
        for actual, expected in zip(cuts, (552, 956, 1_344), strict=True)
    )

    roll, heights = build_roll(label, cuts)
    assert roll.size == (554, 5_470)
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
