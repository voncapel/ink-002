from PIL import Image

import mtg
from mtg import DeckCard, build_batches, parse_decklist


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
