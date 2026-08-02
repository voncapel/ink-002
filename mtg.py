"""Resolve an MTG decklist and prepare full-width thermal proxies for the S002."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from PIL import Image, ImageDraw, ImageOps

from rendering import MAX_HEIGHT, PRINT_WIDTH, adjust_image, apply_dither
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
    set_name: str = ""

    def public(self) -> dict:
        return {
            "qty": self.qty,
            "requested_name": self.requested_name,
            "resolved_name": self.resolved_name,
            "lang": self.lang,
            "image_url": self.image_url,
            "set_name": self.set_name,
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


def _card_image_url(card: dict[str, Any]) -> str | None:
    uris = card.get("image_uris") or {}
    url = uris.get("png") or uris.get("large")
    if url:
        return url
    faces = card.get("card_faces") or []
    for face in faces:
        url = (face.get("image_uris") or {}).get("png")
        if url:
            return url
    return None


def _search_card(name: str, lang: str) -> dict[str, Any] | None:
    query = f'name:"{name}" lang:{lang}'
    url = f"{SCRYFALL_SEARCH}?q={quote(query)}&unique=cards&order=released&dir=asc"
    try:
        data = _http_json(url)
    except (HTTPError, URLError, ValueError, OSError):
        return None
    exact = name.strip().lower()
    for card in data.get("data", []):
        if (card.get("name") or "").strip().lower() == exact:
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
        cards.append(
            DeckCard(
                qty=qty,
                requested_name=name,
                resolved_name=(card.get("name") or name).strip()[:120],
                lang=card.get("lang") or lang,
                image_url=_card_image_url(card) or "",
                set_name=(card.get("set_name") or "").strip()[:80],
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


def render_card_image(
    card: DeckCard,
    *,
    cache_dir: Path,
    dither: str,
    contrast: int,
    brightness: int,
    sharpness: int,
) -> Image.Image:
    """Render one card full-width at the S002 dot width, monochrome, no margins."""
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
