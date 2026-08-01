from PIL import Image

from rendering import DITHER_PRESETS, adjust_image, apply_dither


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
