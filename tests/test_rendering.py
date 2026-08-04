from io import BytesIO

from PIL import Image

from rendering import DITHER_PRESETS, adjust_image, apply_dither, render_upload


def png_stream(image: Image.Image) -> BytesIO:
    stream = BytesIO()
    image.save(stream, format="PNG")
    stream.seek(0)
    return stream


def test_all_dither_presets_produce_binary_images() -> None:
    source = Image.linear_gradient("L").resize((64, 48))
    outputs = []
    for preset in DITHER_PRESETS:
        result = apply_dither(source, preset)
        assert result.mode == "L"
        assert result.size == source.size
        assert set(result.tobytes()) <= {0, 255}
        outputs.append(result.tobytes())
    assert len(set(outputs)) == len(DITHER_PRESETS)


def test_image_adjustments_change_the_result() -> None:
    source = Image.linear_gradient("L").resize((64, 48))
    baseline = adjust_image(source)
    assert adjust_image(source, contrast=170).tobytes() != baseline.tobytes()
    assert adjust_image(source, brightness=70).tobytes() != baseline.tobytes()


def test_printer_width_roll_keeps_its_full_geometry() -> None:
    source = Image.new("L", (554, 5_470), 255)
    source.paste(0, (40, 100, 500, 300))

    rendered = render_upload(
        "roll.png",
        png_stream(source),
        dither="threshold",
    )

    assert rendered.size == (554, 5_470)


def test_images_fill_the_full_band_width_at_any_source_width() -> None:
    for width in (200, 400, 518, 554, 1_108):
        source = Image.new("L", (width, 120), 255)
        source.paste(0, (0, 0, width, 120))

        rendered = render_upload("edge.png", png_stream(source), dither="threshold")
        row = rendered.height // 2
        inked = [x for x in range(rendered.width) if rendered.getpixel((x, row)) < 128]

        assert rendered.width == 554
        assert inked[0] == 0, f"white left border for a {width}px source"
        assert inked[-1] == 553, f"white right border for a {width}px source"


def test_tall_image_is_scaled_only_from_its_width() -> None:
    source = Image.new("L", (1_108, 5_000), 255)

    rendered = render_upload(
        "tall.png",
        png_stream(source),
        dither="threshold",
    )

    assert rendered.size == (554, 2_536)
