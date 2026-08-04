"""Turn text and common uploaded documents into S002-width monochrome images."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import BinaryIO

from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps

from s002_protocol import PRINT_WIDTH

MAX_HEIGHT = 30_000
DEFAULT_MARGIN = 18
SUPPORTED_IMAGES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
DITHER_PRESETS = {"threshold", "floyd", "atkinson", "bayer4", "bayer8"}

# Markdown needs weight and a monospace face. Each variant falls back to the
# regular Unicode font, so a slim font package still renders — just flatter.
FONT_VARIANTS = {
    "bold": (
        "S002_FONT_BOLD_PATH",
        (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        ),
    ),
    "mono": (
        "S002_FONT_MONO_PATH",
        (
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
            "/usr/share/fonts/truetype/noto/NotoSansMono-Regular.ttf",
            "/System/Library/Fonts/Menlo.ttc",
        ),
    ),
    "mono_bold": (
        "S002_FONT_MONO_BOLD_PATH",
        (
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
            "/usr/share/fonts/truetype/noto/NotoSansMono-Bold.ttf",
        ),
    ),
}


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


def find_font_variant(style: str) -> str:
    if style == "regular":
        return find_font()
    env_name, candidates = FONT_VARIANTS[style]
    for candidate in (os.environ.get(env_name), *candidates):
        if candidate and Path(candidate).is_file():
            return candidate
    return find_font()


@lru_cache(maxsize=64)
def load_font(style: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(find_font_variant(style), size)


def _split_long_word(
    draw: ImageDraw.ImageDraw,
    word: str,
    font: ImageFont.FreeTypeFont,
    width: int,
) -> list[str]:
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
    # Images fill the whole printable width: no left/right margin is applied so
    # the full band width is used on screen and on paper. A 554-dot image is
    # already a complete printer-width raster and is preserved byte-for-byte.
    print_ready = gray.width == PRINT_WIDTH
    margin = 0 if print_ready else DEFAULT_MARGIN
    scale = PRINT_WIDTH / gray.width
    target_size = (max(1, round(gray.width * scale)), max(1, round(gray.height * scale)))
    if target_size != gray.size:
        gray = gray.resize(target_size, Image.Resampling.LANCZOS)
    if gray.height + margin * 2 > MAX_HEIGHT:
        raise ValueError("image is too tall for one print job")
    gray = adjust_image(
        gray,
        contrast=contrast,
        brightness=brightness,
        sharpness=sharpness,
    )
    gray = apply_dither(gray, dither)
    canvas = Image.new("L", (PRINT_WIDTH, gray.height + margin * 2), 255)
    canvas.paste(gray, ((PRINT_WIDTH - gray.width) // 2, margin))
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


_INLINE_PATTERN = re.compile(
    r"(?P<code>`[^`]+`)"
    r"|(?P<bold>\*\*[^*]+\*\*|__[^_]+__)"
    r"|(?P<italic>\*[^*\s][^*]*\*|_[^_\s][^_]*_)"
    r"|(?P<link>\[[^\]]+\]\([^)\s]+\))"
)
_LINK_PATTERN = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.*)$")
_RULE_PATTERN = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$")
_BULLET_PATTERN = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_ORDERED_PATTERN = re.compile(r"^(\s*)(\d{1,3})[.)]\s+(.*)$")
_FENCE_PATTERN = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
_QUOTE_PATTERN = re.compile(r"^\s{0,3}>\s?(.*)$")


@dataclass(frozen=True)
class Run:
    """A stretch of inline text sharing one font style."""

    text: str
    style: str


def _parse_inline(text: str, *, base_style: str = "regular") -> list[Run]:
    """Split one paragraph into styled runs, dropping the markdown punctuation."""
    code_style = "mono_bold" if base_style == "bold" else "mono"
    runs: list[Run] = []
    position = 0
    for match in _INLINE_PATTERN.finditer(text):
        if match.start() > position:
            runs.append(Run(text[position : match.start()], base_style))
        if match.group("code"):
            runs.append(Run(match.group("code")[1:-1], code_style))
        elif match.group("bold"):
            runs.append(Run(match.group("bold")[2:-2], "bold"))
        elif match.group("italic"):
            # DejaVu ships no sans oblique in the minimal font package, so
            # emphasis is flattened rather than faked with a slanted transform.
            runs.append(Run(match.group("italic")[1:-1], base_style))
        else:
            label, target = _LINK_PATTERN.match(match.group("link")).groups()
            runs.append(Run(f"{label} ({target})", base_style))
        position = match.end()
    if position < len(text):
        runs.append(Run(text[position:], base_style))
    return [run for run in runs if run.text]


def _wrap_runs(
    runs: list[Run],
    fonts: dict[str, ImageFont.FreeTypeFont],
    width: int,
) -> list[list[tuple[str, str, float]]]:
    """Greedily wrap styled runs into lines of (text, style, x offset) pieces.

    Word boundaries come from the source text rather than the run boundaries, so
    ``[lien](url).`` keeps its period tight against the closing parenthesis.
    """
    measure = ImageDraw.Draw(Image.new("L", (1, 1), 255))

    tokens: list[tuple[str, str, bool]] = []
    space_pending = False
    for run in runs:
        for index, word in enumerate(run.text.split(" ")):
            if index > 0:
                space_pending = True
            if not word:
                continue
            tokens.append((word, run.style, space_pending and bool(tokens)))
            space_pending = False

    lines: list[list[tuple[str, str, float]]] = []
    current: list[tuple[str, str, float]] = []
    x = 0.0
    for word, style, space_before in tokens:
        font = fonts[style]
        gap = measure.textlength(" ", font=font) if space_before and current else 0.0
        word_width = measure.textlength(word, font=font)
        if current and x + gap + word_width > width:
            lines.append(current)
            current, x, gap = [], 0.0, 0.0
        if word_width > width:
            for piece in _split_long_word(measure, word, font, width):
                piece_width = measure.textlength(piece, font=font)
                if current and x + gap + piece_width > width:
                    lines.append(current)
                    current, x, gap = [], 0.0, 0.0
                current.append((piece, style, x + gap))
                x += gap + piece_width
                gap = 0.0
            continue
        current.append((word, style, x + gap))
        x += gap + word_width
    if current:
        lines.append(current)
    return lines


def render_markdown(text: str, *, font_size: int = 32) -> Image.Image:
    """Render a practical markdown subset at the printer's native width.

    Supported: ATX headings, bullet and ordered lists with one nesting level,
    fenced code, blockquotes, horizontal rules, and inline code, bold, emphasis
    and links. Tables and image syntax fall through as plain paragraphs.
    """
    if not text.strip():
        raise ValueError("enter some markdown to print")
    base_size = max(14, min(72, int(font_size)))
    usable_width = PRINT_WIDTH - DEFAULT_MARGIN * 2

    heading_sizes = {
        1: max(base_size + 6, round(base_size * 1.45)),
        2: max(base_size + 4, round(base_size * 1.25)),
        3: max(base_size + 2, round(base_size * 1.1)),
    }
    code_size = max(11, round(base_size * 0.8))
    paragraph_gap = max(6, round(base_size * 0.45))
    block_gap = max(10, round(base_size * 0.7))

    def fonts_at(size: int) -> dict[str, ImageFont.FreeTypeFont]:
        return {style: load_font(style, size) for style in ("regular", "bold", "mono", "mono_bold")}

    def line_height(size: int) -> int:
        return max(size + 8, int(size * 1.35))

    # First pass: lay everything out into draw operations and measure the roll.
    operations: list[tuple] = []
    y = DEFAULT_MARGIN

    def emit_runs(
        runs: list[Run],
        *,
        size: int,
        indent: int = 0,
        hanging: str | None = None,
    ) -> None:
        nonlocal y
        fonts = fonts_at(size)
        width = usable_width - indent
        marker_width = 0.0
        if hanging is not None:
            measure = ImageDraw.Draw(Image.new("L", (1, 1), 255))
            marker_width = measure.textlength(f"{hanging} ", font=fonts["regular"])
            width -= marker_width
        lines = _wrap_runs(runs, fonts, max(40, width))
        step = line_height(size)
        for index, pieces in enumerate(lines or [[]]):
            left = DEFAULT_MARGIN + indent
            if hanging is not None:
                if index == 0:
                    operations.append(("text", left, y, hanging, "regular", size))
                left += marker_width
            for piece, style, offset in pieces:
                operations.append(("text", left + offset, y, piece, style, size))
            y += step

    source_lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    index = 0
    ordered_counters: dict[int, int] = {}
    while index < len(source_lines):
        raw = source_lines[index]
        stripped = raw.strip()

        if not stripped:
            ordered_counters.clear()
            y += paragraph_gap
            index += 1
            continue

        fence = _FENCE_PATTERN.match(raw)
        if fence:
            marker = fence.group(1)[0] * 3
            index += 1
            code_lines: list[str] = []
            while index < len(source_lines) and not source_lines[index].strip().startswith(marker):
                code_lines.append(source_lines[index])
                index += 1
            index += 1  # the closing fence, or the end of the document
            font = load_font("mono", code_size)
            measure = ImageDraw.Draw(Image.new("L", (1, 1), 255))
            step = line_height(code_size)
            y += max(4, paragraph_gap // 2)
            top = y
            for code_line in code_lines:
                for piece in _split_long_word(measure, code_line or " ", font, usable_width - 28):
                    operations.append(("text", DEFAULT_MARGIN + 22, y, piece, "mono", code_size))
                    y += step
            operations.append(("bar", DEFAULT_MARGIN + 6, top, y))
            y += max(4, paragraph_gap // 2)
            ordered_counters.clear()
            continue

        if _RULE_PATTERN.match(raw):
            y += paragraph_gap
            operations.append(("rule", y))
            y += paragraph_gap + 2
            ordered_counters.clear()
            index += 1
            continue

        heading = _HEADING_PATTERN.match(stripped)
        if heading:
            level = len(heading.group(1))
            y += block_gap
            emit_runs(
                _parse_inline(heading.group(2), base_style="bold"),
                size=heading_sizes.get(level, heading_sizes[3]),
            )
            if level == 1:
                y += 2
                operations.append(("rule", y))
                y += 6
            ordered_counters.clear()
            index += 1
            continue

        quote = _QUOTE_PATTERN.match(raw)
        if quote:
            quoted = [quote.group(1)]
            index += 1
            while index < len(source_lines) and (nested := _QUOTE_PATTERN.match(source_lines[index])):
                quoted.append(nested.group(1))
                index += 1
            top = y
            emit_runs(_parse_inline(" ".join(quoted).strip()), size=base_size, indent=24)
            operations.append(("bar", DEFAULT_MARGIN + 6, top, y))
            ordered_counters.clear()
            continue

        bullet = _BULLET_PATTERN.match(raw)
        ordered = _ORDERED_PATTERN.match(raw)
        if bullet or ordered:
            match = bullet or ordered
            level = min(3, len(match.group(1)) // 2)
            if ordered:
                ordered_counters[level] = ordered_counters.get(level, 0) + 1
                marker = f"{ordered_counters[level]}."
                content = ordered.group(3)
            else:
                marker = "•" if level == 0 else "◦"
                content = bullet.group(2)
            emit_runs(_parse_inline(content), size=base_size, indent=24 * level, hanging=marker)
            index += 1
            continue

        paragraph = [stripped]
        index += 1
        while index < len(source_lines):
            follow = source_lines[index]
            if (
                not follow.strip()
                or _HEADING_PATTERN.match(follow.strip())
                or _RULE_PATTERN.match(follow)
                or _BULLET_PATTERN.match(follow)
                or _ORDERED_PATTERN.match(follow)
                or _FENCE_PATTERN.match(follow)
                or _QUOTE_PATTERN.match(follow)
            ):
                break
            paragraph.append(follow.strip())
            index += 1
        emit_runs(_parse_inline(" ".join(paragraph)), size=base_size)
        ordered_counters.clear()

    height = y + DEFAULT_MARGIN
    if height > MAX_HEIGHT:
        raise ValueError("markdown is too long for one print job")

    image = Image.new("L", (PRINT_WIDTH, height), 255)
    draw = ImageDraw.Draw(image)
    for operation in operations:
        if operation[0] == "text":
            _, x, top, content, style, size = operation
            draw.text((x, top), content, font=load_font(style, size), fill=0)
        elif operation[0] == "rule":
            top = operation[1]
            draw.rectangle((DEFAULT_MARGIN, top, PRINT_WIDTH - DEFAULT_MARGIN, top + 2), fill=0)
        else:
            _, x, top, bottom = operation
            draw.rectangle((x, top, x + 4, max(top, bottom - 4)), fill=0)
    return image


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
    if suffix in {".md", ".markdown"}:
        return render_markdown(stream.read().decode("utf-8-sig"), font_size=font_size)
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
        raise ValueError("supported files: PNG, JPEG, WebP, BMP, GIF, TIFF, PDF, TXT, and MD")
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
