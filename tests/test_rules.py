"""The store's rules as plain functions, plus which tools the server exposes as writes."""

from __future__ import annotations

import asyncio

import pytest

from lightspeed_mcp import rules
from lightspeed_mcp.rules import RuleViolation
from lightspeed_mcp.write_path import WRITE_ROUTES


@pytest.mark.parametrize("text", ["3pc Suit", "3 pc", "3-piece", "Three Piece", "three-piece", "3PCS", "3 Pieces"])
def test_three_piece_spellings_are_caught(text):
    with pytest.raises(RuleViolation, match="3-piece"):
        rules.check_not_three_piece(text)


@pytest.mark.parametrize("text", ["2pc Suit", "Suit", "Vest", "Slim 32"])
def test_two_piece_and_plain_words_pass(text):
    rules.check_not_three_piece(text)
    rules.check_not_boys(text)


@pytest.mark.parametrize("text", ["Boys Suit", "Boy's Suit", "Kids Tux", "Youth Vest", "Children's Pants"])
def test_boys_items_are_caught(text):
    with pytest.raises(RuleViolation, match="boys"):
        rules.check_not_boys(text)


@pytest.mark.parametrize("description, codes", [
    ("320-0S2 Suit Grey 62R Classic", ["320-0S2", "62R"]),
    ("Statement A0000200IX Suit", ["A0000200IX"]),
    ("Caravelli Super 120s Suit", []),
    ("Caravelli 2pc Suit", []),
    ("Caravelli 100% Wool 2-button Suit", []),
    ("Caravelli Suit", []),
])
def test_vendor_codes(description, codes):
    assert rules.vendor_codes_in(description) == codes


@pytest.mark.parametrize("description", ["Dress Shirt White 15.5 32/33", "Oxford Shoe Black 10.5D",
                                         "Dress Shirt Blue 16½ 34/35", "Polo Navy 3XL", "Loafer Brown 11W"])
def test_shirt_and_shoe_sizes_are_plain_in_item_descriptions(description):
    assert rules.check_plain_description(description, allow_sizes=True) == description


@pytest.mark.parametrize("description, code", [("Shirt 320-0S2 White", "320-0S2"), ("Shoe A0000200IX", "A0000200IX"),
                                               ("Tie BN-5851", "BN-5851")])
def test_vendor_codes_still_refused_next_to_sizes(description, code):
    with pytest.raises(RuleViolation, match=code):
        rules.check_plain_description(description, allow_sizes=True)


def test_sizes_are_allowed_in_item_descriptions_only():
    assert rules.check_plain_description("Caravelli Black 42R Slim", allow_sizes=True) == "Caravelli Black 42R Slim"
    with pytest.raises(RuleViolation, match="vendor code"):
        rules.check_plain_description("Caravelli Black 42R Slim")


@pytest.mark.parametrize("size, ok", [("32", True), ("40", True), ("34x32", False), ("34R", False), ("34/30", False),
                                      ("3", False), ("100", False)])
def test_waist_sizes(size, ok):
    if ok:
        rules.check_waist_sizes([size])
    else:
        with pytest.raises(RuleViolation, match="waist only"):
            rules.check_waist_sizes([size])


def test_garment_type_aliases_and_description():
    assert rules.check_garment_type("2pc", "Caravelli Suit") == "suit"
    assert rules.check_garment_type("Tux", "Caravelli Tuxedo") == "tuxedo"
    assert rules.check_garment_type("pants", "Caravelli Slacks") == "pants"
    with pytest.raises(RuleViolation, match="does not say"):
        rules.check_garment_type("pants", "Caravelli Suit")


def test_values_are_trimmed_but_never_recased():
    assert rules.check_values("color", ["  Lt. Grey ", "NAVY", "midnight blue"]) == ["Lt. Grey", "NAVY", "midnight blue"]
    with pytest.raises(RuleViolation, match="listed twice"):
        rules.check_values("color", ["Navy", "navy"])


def test_color_check_ignores_brand_names():
    rules.check_not_split_by_color("Ascott Brown Pants", {"brown", "Black"}, ("Ascott Brown",))
    with pytest.raises(RuleViolation, match="names the color 'Black'"):
        rules.check_not_split_by_color("Ascott Brown Black Pants", {"brown", "Black"}, ("Ascott Brown",))


@pytest.mark.parametrize("sku, ok", [("012911", True), ("013471", True), ("12911", False), ("0129111", False),
                                     ("01291A", False), ("", False)])
def test_custom_sku_format(sku, ok):
    if ok:
        assert rules.check_custom_sku_format(f" {sku} ") == sku
    else:
        with pytest.raises(RuleViolation, match="6-digit"):
            rules.check_custom_sku_format(sku)


def test_server_marks_exactly_the_write_tools_as_writes():
    """Every other tool is read-only, except take_snapshot, which only adds local files (GET only)."""
    import server

    tools = asyncio.run(server.mcp.list_tools())
    not_read_only = {t.name for t in tools if not (t.annotations and t.annotations.read_only_hint)}
    assert not_read_only == set(WRITE_ROUTES) | {"take_snapshot"}
    snapshot_tool = next(t for t in tools if t.name == "take_snapshot")
    assert snapshot_tool.annotations.destructive_hint is False
    writes = set(WRITE_ROUTES)
    for t in tools:
        if t.name in writes:
            assert "dry_run" in t.input_schema["properties"]
            assert t.input_schema["properties"]["dry_run"]["default"] is True
            assert "approve" in t.description
