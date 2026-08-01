from pathlib import Path

from PIL import Image

from s002_protocol import encode_print_job, raster_bytes

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_encoder_matches_vendor_capture_byte_for_byte() -> None:
    image = Image.open(FIXTURES / "iliad10-preview.png")
    expected = (FIXTURES / "iliad10-appseq.bin").read_bytes()
    assert encode_print_job(image) == expected


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
