from pathlib import Path

from PIL import Image

from s002_protocol import CANCEL_QUERY, cus_frame, encode_print_job, raster_bytes

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_cancel_command_matches_cus_cancel_task_frame() -> None:
    assert CANCEL_QUERY == cus_frame(0x52, 2, b"\x00")


def test_encoder_matches_vendor_capture_byte_for_byte() -> None:
    image = Image.open(FIXTURES / "iliad10-preview.png")
    expected = (FIXTURES / "iliad10-appseq.bin").read_bytes()
    # The vendor capture ends with the legacy 200-dot trailing feed.
    assert encode_print_job(image, density=12, speed=85, trailing_feed=200) == expected


def test_encoder_exposes_speed_and_defaults_to_light_density() -> None:
    image = Image.new("1", (554, 1), color=1)
    payload = encode_print_job(image)

    assert payload[:11] == bytes.fromhex("64 0a 03 01 00 5f 00 00 00 00 9b")
    assert payload[11:22] == bytes.fromhex("64 09 04 01 00 07 00 00 00 00 9b")


def _completion_feed(payload: bytes) -> int:
    # Last frame is the 0x02 completion frame: 64 02 SS LL LL <feed:2> 00 00 00 00 9b
    assert payload.endswith(b"\x00\x00\x00\x00\x9b")
    return int.from_bytes(payload[-7:-5], "little")


def test_default_trailing_feed_matches_vendor_and_is_configurable() -> None:
    image = Image.new("1", (554, 1), color=1)

    default = encode_print_job(image)
    small = encode_print_job(image, trailing_feed=10)
    large = encode_print_job(image, trailing_feed=200)
    assert default == large
    assert _completion_feed(default) == 200
    assert _completion_feed(small) == 10


def test_raster_matches_payload_extracted_from_vendor_capture() -> None:
    image = Image.open(FIXTURES / "iliad1-test.png")
    capture = (FIXTURES / "iliad1-appseq.bin").read_bytes()
    offset = 22
    extracted = bytearray()
    while capture[offset + 1] == 0:
        size = int.from_bytes(capture[offset + 3 : offset + 5], "little")
        extracted.extend(capture[offset + 5 : offset + 5 + size])
        offset += size + 10
    assert bytes(extracted) == raster_bytes(image)
