from dataclasses import replace

from PIL import Image, ImageDraw

import mtg
from mtg import DeckCard, MtgError, build_batches, parse_decklist, render_card_image, resolve_deck


def test_parse_decklist_quantities() -> None:
    lines = parse_decklist(
        "4 Lightning Bolt\n"
        "4x Consider\n"
        "2  Mountain\n"
        "1× Sol Ring\n"
        "- 3 Chart a Course\n"
        "• 1 Plains\n"
        "\n"
        "// sideboard\n"
        "2 Graveyard Trespasser\n"
    )
    assert lines == [
        (4, "Lightning Bolt"),
        (4, "Consider"),
        (2, "Mountain"),
        (1, "Sol Ring"),
        (3, "Chart a Course"),
        (1, "Plains"),
        (2, "Graveyard Trespasser"),
    ]


def test_parse_decklist_bare_line_is_one_copy() -> None:
    assert parse_decklist("Emrakul, the Aeons Torn") == [(1, "Emrakul, the Aeons Torn")]


def test_build_batches_splits_when_too_tall(monkeypatch) -> None:
    monkeypatch.setattr(mtg, "MAX_BATCH_HEIGHT", 100)
    monkeypatch.setattr(mtg, "MAX_BATCH_BYTES", 10_000_000)
    card = DeckCard(qty=1, requested_name="A")
    images = [(card, Image.new("L", (554, 70), 255)) for _ in range(4)]

    batches = build_batches(images)

    # Four 70-row cards do not fit in one 100-row lot.
    assert len(batches) > 1
    for batch in batches:
        assert batch.width == 554
        assert batch.height <= 100


def test_build_batches_respects_quantity() -> None:
    card = DeckCard(qty=3, requested_name="Mountain")
    image = Image.new("L", (554, 50), 255)

    batches = build_batches([(card, image)])

    assert len(batches) == 1
    # 3 copies, separated by 2 cut-gaps.
    assert batches[0].height == 3 * 50 + 2 * mtg.CARD_SPACING


def test_build_batches_splits_on_byte_budget(monkeypatch) -> None:
    monkeypatch.setattr(mtg, "MAX_BATCH_HEIGHT", 1_000_000)
    # A budget smaller than two 300-row cards forces a split.
    monkeypatch.setattr(mtg, "MAX_BATCH_BYTES", 20_000)
    card = DeckCard(qty=2, requested_name="A")
    image = Image.new("L", (554, 300), 255)

    batches = build_batches([(card, image)])

    assert len(batches) == 2


def test_resolve_deck_keeps_printed_card_text(monkeypatch) -> None:
    monkeypatch.setattr(
        mtg,
        "_search_card",
        lambda _name, _lang: {
            "name": "Lightning Bolt",
            "printed_name": "Foudre",
            "lang": "fr",
            "mana_cost": "{R}",
            "printed_type_line": "Éphémère",
            "printed_text": "La Foudre inflige 3 blessures à n’importe quelle cible.",
            "flavor_text": "Un éclair suffit.",
            "set_name": "Magic 2010",
            "set": "m10",
            "collector_number": "146",
            "artist": "Christopher Moeller",
            "image_uris": {
                "png": "https://cards.example/foudre.png",
                "art_crop": "https://cards.example/foudre-art.jpg",
            },
        },
    )

    cards, missing = resolve_deck([(4, "Foudre")], "fr")

    assert missing == []
    assert cards[0].resolved_name == "Foudre"
    assert cards[0].mana_cost == "{R}"
    assert cards[0].type_line == "Éphémère"
    assert cards[0].oracle_text.startswith("La Foudre inflige")
    assert cards[0].artwork_url.endswith("foudre-art.jpg")
    assert cards[0].set_code == "M10"


def test_compact_cards_use_bundled_hyperlegible_font() -> None:
    mtg._load_font.cache_clear()
    family, style = mtg._load_font(22, bold=True).getname()
    regular_family, regular_style = mtg._load_font(22).getname()
    body_family, body_style = mtg._load_font(22, medium=True).getname()

    assert family == "Atkinson Hyperlegible"
    assert style == "Bold"
    assert regular_family == "Atkinson Hyperlegible"
    assert regular_style == "Regular"
    assert body_family == "Atkinson Hyperlegible Next"
    assert body_style == "Medium"


def test_optimized_card_without_artwork_is_compact_and_offline(tmp_path, monkeypatch) -> None:
    card = DeckCard(
        qty=1,
        requested_name="Sol Ring",
        resolved_name="Anneau solaire",
        lang="fr",
        artwork_url="https://cards.example/art.jpg",
        mana_cost="{1}",
        type_line="Artefact",
        oracle_text="{T} : Ajoutez {C}{C}.",
        flavor_text="Une puissance contenue dans un cercle parfait.",
        artist="Mark Tedin",
        set_code="CMM",
        collector_number="396",
    )
    monkeypatch.setattr(
        mtg,
        "download_card_image",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError),
    )

    image = render_card_image(
        card,
        cache_dir=tmp_path,
        dither="threshold",
        contrast=100,
        brightness=100,
        sharpness=100,
        render_mode="optimized",
        show_artwork=False,
    )

    assert image.width == 554
    assert 120 < image.height < 300
    assert image.getextrema() == (0, 255)
    assert image.crop((0, 0, 8, image.height)).getextrema() == (255, 255)
    assert image.crop((image.width - 8, 0, image.width, image.height)).getextrema() == (255, 255)

    without_flavor_or_metadata = render_card_image(
        replace(card, flavor_text="", artist="", set_code="", collector_number=""),
        cache_dir=tmp_path,
        dither="threshold",
        contrast=100,
        brightness=100,
        sharpness=100,
        render_mode="optimized",
        show_artwork=False,
    )
    assert image.tobytes() == without_flavor_or_metadata.tobytes()


def test_optimized_card_can_include_artwork(tmp_path, monkeypatch) -> None:
    card = DeckCard(
        qty=1,
        requested_name="Bear",
        resolved_name="Grizzly Bears",
        artwork_url="https://cards.example/art.jpg",
        mana_cost="{1}{G}",
        type_line="Creature — Bear",
        oracle_text="A very dependable bear.",
        power="2",
        toughness="2",
    )
    monkeypatch.setattr(
        mtg,
        "download_card_image",
        lambda *_args, **_kwargs: Image.new("RGB", (800, 600), 128),
    )

    image = render_card_image(
        card,
        cache_dir=tmp_path,
        dither="bayer4",
        contrast=100,
        brightness=100,
        sharpness=100,
        render_mode="optimized",
        show_artwork=True,
    )

    assert image.width == 554
    assert image.height >= 400

    measure = ImageDraw.Draw(Image.new("L", (1, 1), 255))
    stat_font = mtg._load_font(23, bold=True)
    stat_width = round(measure.textlength("2 / 2", font=stat_font)) + 26
    stat_left = image.width - 15 - stat_width
    type_y = 15 + 52 + mtg.OPTIMIZED_ART_HEIGHT
    assert image.getpixel((stat_left, type_y + 19)) == 0
    assert image.getpixel((stat_left + stat_width // 2, type_y + 8)) == 255


def test_unknown_render_mode_is_rejected(tmp_path) -> None:
    card = DeckCard(qty=1, requested_name="Mountain")
    try:
        render_card_image(
            card,
            cache_dir=tmp_path,
            dither="threshold",
            contrast=100,
            brightness=100,
            sharpness=100,
            render_mode="poster",
        )
    except MtgError as exc:
        assert "format" in str(exc)
    else:
        raise AssertionError("unknown render mode should fail")
