"""Live smoke test: print two impressions on the S002 with minimal paper use.

Finding: the printer firmware applies its own fixed leading and trailing margin
(~20 mm each side) to EVERY job. The completion-frame value (S002_TRAIL_FEED)
does not reduce this margin. So separate jobs waste paper: each pays the two
margins.

The way to save paper is to stack several impressions into ONE job. The printer
then pays the two fixed margins once for the whole batch, with no margin between
the stacked impressions.

This test prints two labels stacked into one job, then confirms the result.

Usage:
    python tests/two_print_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from s002_protocol import encode_print_job, send_print_job

PRINT_WIDTH = 554
FONT = "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"
GAP = 40  # blank rows between the two stacked impressions


def make_label(text: str) -> Image.Image:
    font = ImageFont.truetype(FONT, 60)
    img = Image.new("1", (PRINT_WIDTH, 90), 1)
    ImageDraw.Draw(img).text((10, 12), text, font=font, fill=0)
    return img


def stack(labels: list[Image.Image]) -> Image.Image:
    """Stack the labels vertically into one tall image (one job = one margin set)."""
    total = sum(label.height for label in labels) + GAP * max(0, len(labels) - 1)
    canvas = Image.new("1", (PRINT_WIDTH, total), 1)
    y = 0
    for index, label in enumerate(labels):
        canvas.paste(label, (0, y))
        y += label.height
        if index < len(labels) - 1:
            y += GAP
    return canvas


def main() -> int:
    stacked = stack([make_label("ONE"), make_label("TWO")])
    payload = encode_print_job(stacked)
    print(f"Encoding 2 labels stacked into ONE job: {len(payload)} bytes", flush=True)
    result = send_print_job(
        payload,
        mac="06:03:86:00:97:AB",
        channel=1,
        transport="macos_rfcomm",
        response_seconds=6.0,
        connect_timeout=15.0,
    )
    print(f"sent={result.sent_bytes} reply={len(result.reply)}", flush=True)
    print(
        "One job printed. Expect ONE and TWO close together (small gap), "
        "with the two fixed margins paid only once around the whole batch.",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
