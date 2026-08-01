"""Turn text and common uploaded documents into S002-width monochrome images."""

from __future__ import annotations

import io
import os
from pathlib import Path
from typing import BinaryIO

from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps

from s002_protocol import PRINT_WIDTH


MAX_HEIGHT = 30_000
DEFAULT_MARGIN = 18
SUPPORTED_IMAGES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
DITHER_PRESETS = {"threshold", "floyd", "atkinson", "bayer4", "bayer8"}


def find_font() -> str:
    configured = os.environ.get("S002_FONT_PATH")
    candidates = [
        configured,
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise RuntimeError("no Unicode font found; install fonts-dejavu-core or set S002_FONT_PATH")


def _split_long_word(draw: ImageDraw.ImageDraw, word: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    pieces: list[str] = []
    current = ""
    for character in word:
        proposal = current + character
        if current and draw.textlength(proposal, font=font) > width:
            pieces.append(current)
            current = character
        else:
            current = proposal
    if current:
        pieces.append(current)
    return pieces or [""]


def _wrap_text(text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    measure = ImageDraw.Draw(Image.new("L", (1, 1), 255))
    lines: list[str] = []
    for paragraph in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not paragraph:
            lines.append("")
            continue
        current = ""
        for word in paragraph.split(" "):
            proposal = word if not current else f"{current} {word}"
            if measure.textlength(proposal, font=font) <= width:
                current = proposal
                continue
            if current:
                lines.append(current)
                current = ""
            if measure.textlength(word, font=font) <= width:
                current = word
            else:
                pieces = _split_long_word(measure, word, font, width)
                lines.extend(pieces[:-1])
                current = pieces[-1]
        lines.append(current)
    return lines


def render_text(text: str, *, font_size: int = 32, align: str = "left") -> Image.Image:
    cleaned = text.strip("\n")
    if not cleaned.strip():
        raise ValueError("enter some text to print")
    font_size = max(14, min(72, int(font_size)))
    if align not in {"left", "center", "right"}:
        raise ValueError("invalid text alignment")
    font = ImageFont.truetype(find_font(), font_size)
    usable_width = PRINT_WIDTH - (DEFAULT_MARGIN * 2)
    lines = _wrap_text(cleaned, font, usable_width)
    line_height = max(font_size + 8, int(font_size * 1.35))
    height = DEFAULT_MARGIN * 2 + line_height * len(lines)
    if height > MAX_HEIGHT:
        raise ValueError("text is too long for one print job")

    image = Image.new("L", (PRINT_WIDTH, height), 255)
    draw = ImageDraw.Draw(image)
    y = DEFAULT_MARGIN
    for line in lines:
        line_width = draw.textlength(line, font=font)
        if align == "center":
            x = (PRINT_WIDTH - line_width) / 2
        elif align == "right":
            x = PRINT_WIDTH - DEFAULT_MARGIN - line_width
        else:
            x = DEFAULT_MARGIN
        draw.text((x, y), line, font=font, fill=0)
        y += line_height
    return image


def _diffuse(gray: Image.Image, kernel: tuple[tuple[int, int, float], ...]) -> Image.Image:
    width, height = gray.size
    values = [float(value) for value in gray.tobytes()]
    output = bytearray(width * height)
    for y in range(height):
        row = y * width
        for x in range(width):
            index = row + x
            old = values[index]
            new = 255 if old >= 128 else 0
            output[index] = new
            error = old - new
            for dx, dy, weight in kernel:
                nx, ny = x + dx, y + dy
                if 0 <= nx < width and ny < height:
                    target = ny * width + nx
                    values[target] = max(0.0, min(255.0, values[target] + error * weight))
    return Image.frombytes("L", (width, height), bytes(output))


def _ordered(gray: Image.Image, matrix: tuple[tuple[int, ...], ...]) -> Image.Image:
    width, height = gray.size
    size = len(matrix)
    levels = size * size
    source = gray.load()
    output = Image.new("L", gray.size, 255)
    pixels = output.load()
    for y in range(height):
        for x in range(width):
            threshold = (matrix[y % size][x % size] + 0.5) * 255 / levels
            pixels[x, y] = 255 if source[x, y] >= threshold else 0
    return output


def apply_dither(gray: Image.Image, preset: str = "floyd") -> Image.Image:
    """Apply one of the five deterministic 1-bit image treatments offered by the UI."""
    if preset not in DITHER_PRESETS:
        raise ValueError("unknown dithering preset")
    gray = gray.convert("L")
    if preset == "threshold":
        return gray.point(lambda value: 255 if value >= 140 else 0, mode="L")
    if preset == "floyd":
        return _diffuse(
            gray,
            (
                (1, 0, 7 / 16),
                (-1, 1, 3 / 16),
                (0, 1, 5 / 16),
                (1, 1, 1 / 16),
            ),
        )
    if preset == "atkinson":
        return _diffuse(
            gray,
            (
                (1, 0, 1 / 8),
                (2, 0, 1 / 8),
                (-1, 1, 1 / 8),
                (0, 1, 1 / 8),
                (1, 1, 1 / 8),
                (0, 2, 1 / 8),
            ),
        )
    bayer4 = (
        (0, 8, 2, 10),
        (12, 4, 14, 6),
        (3, 11, 1, 9),
        (15, 7, 13, 5),
    )
    if preset == "bayer4":
        return _ordered(gray, bayer4)
    quadrant = ((0, 2), (3, 1))
    bayer8 = tuple(
        tuple(4 * bayer4[y % 4][x % 4] + quadrant[y // 4][x // 4] for x in range(8))
        for y in range(8)
    )
    return _ordered(gray, bayer8)


def adjust_image(
    gray: Image.Image,
    *,
    contrast: int = 100,
    brightness: int = 100,
    sharpness: int = 100,
) -> Image.Image:
    """Apply the same three adjustments exposed by the live image editor."""
    adjusted = ImageEnhance.Brightness(gray.convert("L")).enhance(brightness / 100)
    adjusted = ImageEnhance.Contrast(adjusted).enhance(contrast / 100)
    return ImageEnhance.Sharpness(adjusted).enhance(sharpness / 100)


def _fit_image(
    source: Image.Image,
    *,
    dither: str = "floyd",
    contrast: int = 100,
    brightness: int = 100,
    sharpness: int = 100,
) -> Image.Image:
    frame = source.convert("RGBA")
    background = Image.new("RGBA", frame.size, "white")
    background.alpha_composite(frame)
    gray = ImageOps.autocontrast(background.convert("L"))
    content_width = PRINT_WIDTH - DEFAULT_MARGIN * 2
    scale = min(1.0, content_width / gray.width)
    target_size = (max(1, round(gray.width * scale)), max(1, round(gray.height * scale)))
    if target_size != gray.size:
        gray = gray.resize(target_size, Image.Resampling.LANCZOS)
    if gray.height + DEFAULT_MARGIN * 2 > MAX_HEIGHT:
        raise ValueError("image is too tall for one print job")
    gray = adjust_image(
        gray,
        contrast=contrast,
        brightness=brightness,
        sharpness=sharpness,
    )
    gray = apply_dither(gray, dither)
    canvas = Image.new("L", (PRINT_WIDTH, gray.height + DEFAULT_MARGIN * 2), 255)
    canvas.paste(gray, ((PRINT_WIDTH - gray.width) // 2, DEFAULT_MARGIN))
    return canvas


def render_pdf(
    stream: BinaryIO,
    *,
    dither: str = "floyd",
    contrast: int = 100,
    brightness: int = 100,
    sharpness: int = 100,
) -> Image.Image:
    try:
        import fitz
    except ImportError as exc:
        raise RuntimeError("PDF support requires PyMuPDF") from exc
    content = stream.read()
    document = fitz.open(stream=content, filetype="pdf")
    if document.page_count > 20:
        raise ValueError("PDFs are limited to 20 pages per job")
    pages: list[Image.Image] = []
    total_height = DEFAULT_MARGIN
    try:
        for page in document:
            zoom = (PRINT_WIDTH - DEFAULT_MARGIN * 2) / page.rect.width
            pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False, colorspace=fitz.csGRAY)
            image = Image.frombytes("L", (pixmap.width, pixmap.height), pixmap.samples)
            image = adjust_image(
                ImageOps.autocontrast(image),
                contrast=contrast,
                brightness=brightness,
                sharpness=sharpness,
            )
            image = apply_dither(image, dither)
            pages.append(image)
            total_height += image.height + DEFAULT_MARGIN
            if total_height > MAX_HEIGHT:
                raise ValueError("rendered PDF is too long for one print job")
    finally:
        document.close()
    canvas = Image.new("L", (PRINT_WIDTH, total_height), 255)
    y = DEFAULT_MARGIN
    for page in pages:
        canvas.paste(page, ((PRINT_WIDTH - page.width) // 2, y))
        y += page.height + DEFAULT_MARGIN
    return canvas


def render_upload(
    filename: str,
    stream: BinaryIO,
    *,
    font_size: int = 32,
    dither: str = "floyd",
    contrast: int = 100,
    brightness: int = 100,
    sharpness: int = 100,
) -> Image.Image:
    suffix = Path(filename).suffix.lower()
    if suffix == ".txt":
        return render_text(stream.read().decode("utf-8-sig"), font_size=font_size)
    if suffix == ".pdf":
        return render_pdf(
            stream,
            dither=dither,
            contrast=contrast,
            brightness=brightness,
            sharpness=sharpness,
        )
    if suffix not in SUPPORTED_IMAGES:
        raise ValueError("supported files: PNG, JPEG, WebP, BMP, GIF, TIFF, PDF, and TXT")
    try:
        image = Image.open(stream)
        image.seek(0)
        image.load()
    except Exception as exc:
        raise ValueError("the uploaded image could not be decoded") from exc
    return _fit_image(
        image,
        dither=dither,
        contrast=contrast,
        brightness=brightness,
        sharpness=sharpness,
    )
