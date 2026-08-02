"""Resolve an MTG decklist and prepare full-width thermal proxies for the S002."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from PIL import Image, ImageDraw, ImageFont, ImageOps

from rendering import MAX_HEIGHT, PRINT_WIDTH, adjust_image, apply_dither, find_font
from s002_protocol import BYTES_PER_ROW, ROWS_PER_FRAME

SCRYFALL_SEARCH = "https://api.scryfall.com/cards/search"
USER_AGENT = "ink002/1.0 (local MTG proxy composer)"
SUPPORTED_LANGS = {"en", "fr", "de", "it", "pt", "es", "ja", "zh", "ko", "ru"}
DEFAULT_LANG = "fr"
MAX_BATCH_HEIGHT = int(os.environ.get("MTG_MAX_BATCH_HEIGHT", "3000"))
MAX_BATCH_BYTES = int(os.environ.get("MTG_MAX_BATCH_BYTES", "200000"))
CARD_SPACING = 8
DASH = 7
GAP = 6
MTG_RENDER_MODES = {"optimized", "full"}
OPTIMIZED_ART_HEIGHT = 270
BUNDLED_FONT_DIR = Path(__file__).resolve().parent / "static" / "fonts"

_MANA_GLYPHS = {
    "W": "\ue600",
    "U": "\ue601",
    "B": "\ue602",
    "R": "\ue603",
    "G": "\ue604",
    "0": "\ue605",
    "1": "\ue606",
    "2": "\ue607",
    "3": "\ue608",
    "4": "\ue609",
    "5": "\ue60a",
    "6": "\ue60b",
    "7": "\ue60c",
    "8": "\ue60d",
    "9": "\ue60e",
    "10": "\ue60f",
    "11": "\ue610",
    "12": "\ue611",
    "13": "\ue612",
    "14": "\ue613",
    "15": "\ue614",
    "16": "\ue62a",
    "17": "\ue62b",
    "18": "\ue62c",
    "19": "\ue62d",
    "20": "\ue62e",
    "X": "\ue615",
    "Y": "\ue616",
    "Z": "\ue617",
    "P": "\ue618",
    "S": "\ue619",
    "T": "\ue61a",
    "Q": "\ue61b",
    "C": "\ue904",
    "E": "\ue907",
    "∞": "\ue903",
}

_QUANTITY_RE = re.compile(r"^\s*(?:[-*•]\s*)?(\d+)\s*[xX×]?\s+(.+?)\s*$")


class MtgError(ValueError):
    """A user-facing failure while preparing an MTG deck for printing."""


@dataclass
class DeckCard:
    qty: int
    requested_name: str
    resolved_name: str = ""
    lang: str = ""
    image_url: str = ""
    artwork_url: str = ""
    set_name: str = ""
    mana_cost: str = ""
    type_line: str = ""
    oracle_text: str = ""
    flavor_text: str = ""
    power: str = ""
    toughness: str = ""
    loyalty: str = ""
    defense: str = ""
    artist: str = ""
    set_code: str = ""
    collector_number: str = ""

    def public(self) -> dict:
        return {
            "qty": self.qty,
            "requested_name": self.requested_name,
            "resolved_name": self.resolved_name,
            "lang": self.lang,
            "image_url": self.image_url,
            "artwork_url": self.artwork_url,
            "set_name": self.set_name,
            "mana_cost": self.mana_cost,
            "type_line": self.type_line,
            "oracle_text": self.oracle_text,
            "flavor_text": self.flavor_text,
            "power": self.power,
            "toughness": self.toughness,
            "loyalty": self.loyalty,
            "defense": self.defense,
            "artist": self.artist,
            "set_code": self.set_code,
            "collector_number": self.collector_number,
        }


@dataclass
class BatchInfo:
    index: int
    height: int
    estimated_bytes: int

    def public(self) -> dict:
        return {"index": self.index, "height": self.height, "estimated_bytes": self.estimated_bytes}


@dataclass
class RenderedCard:
    index: int
    card: DeckCard
    height: int
    width: int

    def public(self) -> dict:
        return {
            "index": self.index,
            "qty": self.card.qty,
            "name": self.card.resolved_name or self.card.requested_name,
            "lang": self.card.lang,
            "height": self.height,
            "width": self.width,
        }


@dataclass
class MtgDeck:
    id: str
    lang: str
    title: str
    cards: list[DeckCard]
    missing: list[tuple[int, str]]
    created_at: str
    batches: list[BatchInfo] = field(default_factory=list)
    gallery: list[RenderedCard] = field(default_factory=list)
    render_mode: str = "optimized"
    show_artwork: bool = True

    @property
    def printable_copies(self) -> int:
        return sum(card.qty for card in self.cards)

    def public(self) -> dict:
        return {
            "id": self.id,
            "lang": self.lang,
            "title": self.title,
            "cards": [card.public() for card in self.cards],
            "missing": [{"qty": qty, "name": name} for qty, name in self.missing],
            "card_count": len(self.cards),
            "printable_copies": self.printable_copies,
            "batches": [batch.public() for batch in self.batches],
            "gallery": [card.public() for card in self.gallery],
            "render_mode": self.render_mode,
            "show_artwork": self.show_artwork,
        }


def parse_decklist(text: str) -> list[tuple[int, str]]:
    """Convert pasted decklist text into (quantity, card name) pairs."""
    lines: list[tuple[int, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("//") or line.endswith("//") or "//" in line[:2]:
            continue
        match = _QUANTITY_RE.match(line)
        if not match:
            # A bare line without a leading number is one copy of the card.
            lines.append((1, line))
            continue
        lines.append((int(match.group(1)), match.group(2)))
    return lines


def _http_json(url: str) -> dict[str, Any]:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urlopen(request, timeout=20) as response:
        return json.load(response)


def _card_image_url(card: dict[str, Any], kind: str = "full") -> str | None:
    uris = card.get("image_uris") or {}
    url = uris.get("art_crop") if kind == "art" else uris.get("png") or uris.get("large")
    if url:
        return url
    faces = card.get("card_faces") or []
    for face in faces:
        face_uris = face.get("image_uris") or {}
        url = face_uris.get("art_crop") if kind == "art" else face_uris.get("png") or face_uris.get("large")
        if url:
            return url
    return None


def _card_face(card: dict[str, Any]) -> dict[str, Any]:
    faces = card.get("card_faces") or []
    return faces[0] if faces else card


def _localized(face: dict[str, Any], key: str) -> str:
    printed_key = {
        "name": "printed_name",
        "type_line": "printed_type_line",
        "oracle_text": "printed_text",
    }.get(key)
    value = face.get(printed_key) if printed_key else None
    return str(value or face.get(key) or "").strip()


def _search_card(name: str, lang: str) -> dict[str, Any] | None:
    query = f'name:"{name}" lang:{lang}'
    url = f"{SCRYFALL_SEARCH}?q={quote(query)}&unique=cards&order=released&dir=asc"
    try:
        data = _http_json(url)
    except (HTTPError, URLError, ValueError, OSError):
        return None
    exact = name.strip().casefold()
    for card in data.get("data", []):
        face = _card_face(card)
        names = {str(card.get("name") or "").strip().casefold(), _localized(face, "name").casefold()}
        if exact in names:
            return card
    return data.get("data", [None])[0] or None


def resolve_deck(lines: list[tuple[int, str]], lang: str) -> tuple[list[DeckCard], list[tuple[int, str]]]:
    """Resolve each card name against Scryfall in the requested language."""
    cards: list[DeckCard] = []
    missing: list[tuple[int, str]] = []
    for qty, name in lines:
        card = _search_card(name, lang)
        if card is None:
            missing.append((qty, name))
            continue
        face = _card_face(card)
        cards.append(
            DeckCard(
                qty=qty,
                requested_name=name,
                resolved_name=(_localized(face, "name") or name)[:120],
                lang=card.get("lang") or lang,
                image_url=_card_image_url(card) or "",
                artwork_url=_card_image_url(card, "art") or "",
                set_name=(card.get("set_name") or "").strip()[:80],
                mana_cost=str(face.get("mana_cost") or "")[:120],
                type_line=_localized(face, "type_line")[:240],
                oracle_text=_localized(face, "oracle_text")[:4000],
                flavor_text=str(face.get("flavor_text") or "")[:1200],
                power=str(face.get("power") or "")[:12],
                toughness=str(face.get("toughness") or "")[:12],
                loyalty=str(face.get("loyalty") or "")[:12],
                defense=str(face.get("defense") or "")[:12],
                artist=str(face.get("artist") or card.get("artist") or "")[:120],
                set_code=str(card.get("set") or "").upper()[:12],
                collector_number=str(card.get("collector_number") or "")[:24],
            )
        )
    return cards, missing


def _cache_path(cache_dir: Path, url: str) -> Path:
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return cache_dir / f"{key}.png"


def download_card_image(url: str, cache_dir: Path) -> Image.Image:
    if not url:
        raise MtgError("carte sans image téléchargeable")
    path = _cache_path(cache_dir, url)
    if path.is_file():
        return Image.open(path).convert("RGBA")
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=30) as response:
        data = response.read()
    cache_dir.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return Image.open(BytesIO(data)).convert("RGBA")


@lru_cache(maxsize=32)
def _load_font(
    size: int,
    *,
    bold: bool = False,
    medium: bool = False,
    italic: bool = False,
    unicode: bool = False,
) -> ImageFont.FreeTypeFont:
    configured = os.environ.get("MTG_FONT_PATH")
    configured_bold = os.environ.get("MTG_FONT_BOLD_PATH")
    candidates = [
        configured_bold if bold else configured,
        BUNDLED_FONT_DIR
        / (
            "AtkinsonHyperlegible-Bold.ttf"
            if bold
            else "AtkinsonHyperlegibleNext-Variable.ttf"
            if medium
            else "AtkinsonHyperlegible-Regular.ttf"
        ),
        "/System/Library/Fonts/Supplemental/Georgia Bold.ttf" if bold else None,
        "/System/Library/Fonts/Supplemental/Georgia Italic.ttf" if italic else None,
        "/System/Library/Fonts/Supplemental/Georgia.ttf",
        "/System/Library/Fonts/NewYorkItalic.ttf" if italic else "/System/Library/Fonts/NewYork.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf" if bold else None,
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Italic.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    ]
    if unicode:
        candidates.insert(0, find_font())
    for candidate in candidates:
        path = Path(candidate).expanduser() if candidate else None
        if path and path.is_file():
            font = ImageFont.truetype(str(path), size)
            if medium and path.name == "AtkinsonHyperlegibleNext-Variable.ttf":
                font.set_variation_by_name("Medium")
            return font
    return ImageFont.truetype(find_font(), size)


@lru_cache(maxsize=16)
def _load_mana_font(size: int) -> ImageFont.FreeTypeFont | None:
    candidates = [
        os.environ.get("MTG_SYMBOL_FONT_PATH"),
        "~/Library/Fonts/mana.ttf",
        "/usr/local/share/fonts/mana.ttf",
        "/usr/share/fonts/truetype/mana/mana.ttf",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).expanduser().is_file():
            return ImageFont.truetype(str(Path(candidate).expanduser()), size)
    return None


def _symbol_width(token: str, size: int) -> int:
    return size if token.upper() in _MANA_GLYPHS else max(size + 4, len(token) * (size // 2) + 8)


def _draw_symbol(draw: ImageDraw.ImageDraw, xy: tuple[int, int], token: str, size: int) -> int:
    x, y = xy
    key = token.upper()
    width = _symbol_width(key, size)
    mana_font = _load_mana_font(size)
    glyph = _MANA_GLYPHS.get(key)
    if mana_font is not None and glyph:
        draw.text((x, y), glyph, font=mana_font, fill=0, anchor="lt")
        return width
    draw.rounded_rectangle((x, y, x + width - 2, y + size - 2), radius=size // 2, outline=0, width=2)
    fallback = _load_font(max(10, size // 2), bold=True)
    label = key.replace("/", "∕")
    draw.text((x + width / 2 - 1, y + size / 2 - 1), label, font=fallback, fill=0, anchor="mm")
    return width


def _inline_tokens(text: str) -> list[tuple[str, str, str]]:
    return re.findall(r"\{([^}]+)\}|([^\s{}]+)|(\s+)", text)


def _layout_inline(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    symbol_size: int,
    width: int,
) -> list[list[tuple[str, str, int]]]:
    lines: list[list[tuple[str, str, int]]] = []
    for paragraph_index, paragraph in enumerate(text.split("\n")):
        current: list[tuple[str, str, int]] = []
        current_width = 0
        for symbol, word, whitespace in _inline_tokens(paragraph):
            kind = "symbol" if symbol else "space" if whitespace else "text"
            value = symbol or whitespace or word
            if kind == "symbol":
                pieces = [(kind, value, _symbol_width(value, symbol_size))]
            else:
                token_width = round(draw.textlength(value, font=font))
                pieces = [(kind, value, token_width)]
                if kind == "text" and token_width > width:
                    pieces = []
                    piece = ""
                    for character in value:
                        proposal = piece + character
                        proposal_width = round(draw.textlength(proposal, font=font))
                        if piece and proposal_width > width:
                            pieces.append((kind, piece, round(draw.textlength(piece, font=font))))
                            piece = character
                        else:
                            piece = proposal
                    if piece:
                        pieces.append((kind, piece, round(draw.textlength(piece, font=font))))
            for piece_kind, piece_value, piece_width in pieces:
                if piece_kind == "space" and not current:
                    continue
                if current and current_width + piece_width > width:
                    while current and current[-1][0] == "space":
                        current.pop()
                    lines.append(current)
                    current = []
                    current_width = 0
                    if piece_kind == "space":
                        continue
                current.append((piece_kind, piece_value, piece_width))
                current_width += piece_width
        lines.append(current)
        if paragraph_index < len(text.split("\n")) - 1 and not current:
            lines.append([])
    return lines or [[]]


def _draw_inline_lines(
    draw: ImageDraw.ImageDraw,
    lines: list[list[tuple[str, str, int]]],
    *,
    x: int,
    y: int,
    font: ImageFont.FreeTypeFont,
    symbol_size: int,
    line_height: int,
    stroke_width: int = 0,
) -> int:
    for line in lines:
        cursor = x
        for kind, value, width in line:
            if kind == "symbol":
                _draw_symbol(draw, (cursor, y + max(0, (line_height - symbol_size) // 2)), value, symbol_size)
            else:
                draw.text(
                    (cursor, y),
                    value,
                    font=font,
                    fill=0,
                    stroke_width=stroke_width,
                    stroke_fill=0,
                )
            cursor += width
        y += line_height
    return y


def _stat_label(card: DeckCard) -> str:
    if card.power or card.toughness:
        return f"{card.power or '—'} / {card.toughness or '—'}"
    if card.loyalty:
        return f"LOY {card.loyalty}"
    if card.defense:
        return f"DEF {card.defense}"
    return ""


def render_optimized_card(
    card: DeckCard,
    *,
    cache_dir: Path,
    show_artwork: bool,
    dither: str,
    contrast: int,
    brightness: int,
    sharpness: int,
) -> Image.Image:
    """Render a compact, text-first proxy whose artwork is optional."""
    margin = 15
    content_width = PRINT_WIDTH - margin * 2
    unicode_font = card.lang in {"ja", "zh", "ko"}
    title_font = _load_font(30, bold=True, unicode=unicode_font)
    type_font = _load_font(20, bold=True, unicode=unicode_font)
    body_font = _load_font(22, medium=True, unicode=unicode_font)
    stat_font = _load_font(23, bold=True, unicode=unicode_font)
    heading_stroke = 1 if unicode_font else 0
    body_stroke = 0
    measure = ImageDraw.Draw(Image.new("L", (1, 1), 255))

    mana_tokens = re.findall(r"\{([^}]+)\}", card.mana_cost)
    mana_width = sum(_symbol_width(token, 28) + 2 for token in mana_tokens)
    title_width = max(160, content_width - mana_width - (12 if mana_tokens else 0))
    title = card.resolved_name or card.requested_name
    while title_font.size > 22 and measure.textlength(title, font=title_font) > title_width:
        title_font = _load_font(title_font.size - 2, bold=True, unicode=unicode_font)

    stat = _stat_label(card)
    stat_width = round(measure.textlength(stat, font=stat_font)) if stat else 0
    stat_box_width = stat_width + 26 if stat else 0
    type_width = content_width - stat_box_width - (14 if stat else 0)
    type_lines = _layout_inline(measure, card.type_line or "Carte Magic", type_font, 19, type_width)
    oracle_lines = _layout_inline(
        measure,
        card.oracle_text or "Texte de règles indisponible.",
        body_font,
        22,
        content_width,
    )
    header_height = 52
    art_height = OPTIMIZED_ART_HEIGHT if show_artwork and card.artwork_url else 0
    type_height = max(len(type_lines) * 27 + 10, 40)
    oracle_height = len(oracle_lines) * 28 + 14
    height = (
        margin
        + header_height
        + art_height
        + type_height
        + oracle_height
        + margin
    )
    if height > MAX_HEIGHT:
        raise MtgError("une carte optimisée est trop haute pour un lot d'impression")

    image = Image.new("L", (PRINT_WIDTH, height), 255)
    draw = ImageDraw.Draw(image)
    y = margin
    draw.text(
        (margin, y + 3),
        title,
        font=title_font,
        fill=0,
        stroke_width=heading_stroke,
        stroke_fill=0,
    )
    mana_x = PRINT_WIDTH - margin - mana_width
    for token in mana_tokens:
        mana_x += _draw_symbol(draw, (mana_x, y + 6), token, 28) + 2
    y += header_height
    draw.line((margin, y - 2, PRINT_WIDTH - margin, y - 2), fill=0, width=2)

    if art_height:
        source = download_card_image(card.artwork_url, cache_dir)
        background = Image.new("RGBA", source.size, "white")
        background.alpha_composite(source.convert("RGBA"))
        art = ImageOps.fit(
            background.convert("L"),
            (content_width, art_height - 8),
            method=Image.Resampling.LANCZOS,
        )
        art = ImageOps.autocontrast(art)
        art = adjust_image(art, contrast=contrast, brightness=brightness, sharpness=sharpness)
        art = apply_dither(art, dither)
        image.paste(art, (margin, y + 4))
        y += art_height
        draw.line((margin, y - 2, PRINT_WIDTH - margin, y - 2), fill=0, width=1)

    type_y = y
    y = _draw_inline_lines(
        draw,
        type_lines,
        x=margin,
        y=y + 7,
        font=type_font,
        symbol_size=19,
        line_height=27,
        stroke_width=heading_stroke,
    ) + 4
    if stat:
        stat_left = PRINT_WIDTH - margin - stat_box_width
        draw.rounded_rectangle(
            (stat_left, type_y + 4, PRINT_WIDTH - margin, type_y + 35),
            radius=16,
            fill=255,
            outline=0,
            width=2,
        )
        draw.text(
            (stat_left + stat_box_width / 2, type_y + 19),
            stat,
            font=stat_font,
            fill=0,
            anchor="mm",
        )
    draw.line((margin, y, PRINT_WIDTH - margin, y), fill=0, width=1)
    y = _draw_inline_lines(
        draw,
        oracle_lines,
        x=margin,
        y=y + 9,
        font=body_font,
        symbol_size=22,
        line_height=28,
        stroke_width=body_stroke,
    ) + 6
    draw.line((margin, y, PRINT_WIDTH - margin, y), fill=0, width=1)
    return image


def render_card_image(
    card: DeckCard,
    *,
    cache_dir: Path,
    dither: str,
    contrast: int,
    brightness: int,
    sharpness: int,
    render_mode: str = "full",
    show_artwork: bool = True,
) -> Image.Image:
    """Render one full-card image or compact text-first proxy at S002 width."""
    if render_mode not in MTG_RENDER_MODES:
        raise MtgError("format de carte inconnu")
    if render_mode == "optimized":
        return render_optimized_card(
            card,
            cache_dir=cache_dir,
            show_artwork=show_artwork,
            dither=dither,
            contrast=contrast,
            brightness=brightness,
            sharpness=sharpness,
        )
    source = download_card_image(card.image_url, cache_dir)
    background = Image.new("RGBA", source.size, "white")
    background.alpha_composite(source)
    gray = ImageOps.autocontrast(background.convert("L"))
    scale = min(1.0, PRINT_WIDTH / gray.width)
    target = (max(1, round(gray.width * scale)), max(1, round(gray.height * scale)))
    if target != gray.size:
        gray = gray.resize(target, Image.Resampling.LANCZOS)
    if gray.height > MAX_HEIGHT:
        raise MtgError("une carte est trop haute pour un lot d'impression")
    gray = adjust_image(gray, contrast=contrast, brightness=brightness, sharpness=sharpness)
    return apply_dither(gray, dither)


def _estimated_bytes(height: int) -> int:
    rows_per_byte_row = BYTES_PER_ROW
    frames = max(1, height // ROWS_PER_FRAME + 1)
    return height * rows_per_byte_row + frames * 16


def _gap_line(canvas: Image.Image, y: int) -> None:
    draw = ImageDraw.Draw(canvas)
    x = 0
    while x < PRINT_WIDTH:
        draw.line([(x, y), (x + DASH, y)], fill=0)
        x += DASH + GAP


def _assemble_batch(images: list[Image.Image]) -> Image.Image:
    total = sum(image.height for image in images) + CARD_SPACING * max(0, len(images) - 1)
    canvas = Image.new("L", (PRINT_WIDTH, total), 255)
    y = 0
    for index, image in enumerate(images):
        canvas.paste(image, (0, y))
        y += image.height
        if index < len(images) - 1:
            _gap_line(canvas, y + CARD_SPACING // 2)
            y += CARD_SPACING
    return canvas


def build_batches(rendered: list[tuple[DeckCard, Image.Image]]) -> list[Image.Image]:
    """Stack cards (respecting quantities) full-width, splitting when a lot gets too large."""
    copies: list[Image.Image] = []
    for card, image in rendered:
        copies.extend([image] * card.qty)

    batches: list[Image.Image] = []
    current: list[Image.Image] = []
    current_height = 0
    current_bytes = 0
    for image in copies:
        spacing = CARD_SPACING if current else 0
        if current and (
            current_height + spacing + image.height > MAX_BATCH_HEIGHT
            or current_bytes + _estimated_bytes(image.height) > MAX_BATCH_BYTES
        ):
            batches.append(_assemble_batch(current))
            current = []
            current_height = 0
            current_bytes = 0
            spacing = 0
        current.append(image)
        current_height += spacing + image.height
        current_bytes += _estimated_bytes(image.height)
    if current:
        batches.append(_assemble_batch(current))
    if not batches:
        raise MtgError("aucune carte à imprimer")
    return batches
