"""The store's business rules for new and changed inventory records.

The write tools call these before they build a payload. Each check raises
RuleViolation with a message that says what was refused and why, so Claude
can pass it straight on. Nothing here talks to Lightspeed.

The rules (from the store owner):
  * Use the store's own attribute set 5 "Color/Size/Fit". Color is an
    attribute value, never a separate matrix.
  * Matrices are split by garment type (suit, tuxedo, pants, ...), never by color.
  * Color and fit values use each vendor's own naming. They are sent exactly
    as given (outer spaces trimmed), never re-cased or "corrected".
  * Pants are separate items with their own matrices, stocked uncut and
    sized by waist only.
  * No 3-piece items: a 3pc is stored as a suit plus a vest.
  * No boys' items.
  * Matrix descriptions are plain language, with no vendor codes.
  * Every item gets its own custom SKU; duplicates are refused.

Some of these are worked out from text (e.g. "3pc" in a description). The
patterns are below; if a legitimate word gets refused, adjust its pattern.
"""

from __future__ import annotations

import re

from .errors import LightspeedError

# The store's own attribute set: attribute1 = Color, attribute2 = Size, attribute3 = Fit.
STORE_ATTRIBUTE_SET_ID = 5

# Custom SKUs in this store are 6-digit numbers, e.g. "012911" (NOTES.md).
# Kept as text so leading zeros survive.
CUSTOM_SKU_PATTERN = re.compile(r"^\d{6}$")

# Lightspeed's limit on description fields.
MAX_DESCRIPTION_LENGTH = 255

# Garment types a matrix can be. A matrix holds exactly one garment type, and
# its description must name it, so anyone reading the matrix list can tell
# what it is. "suit" means a 2-piece suit (jacket and pants).
GARMENT_WORDS = {
    "suit": ("suit", "suits", "2pc"),
    "tuxedo": ("tux", "tuxedo", "tuxedos"),
    "vest": ("vest", "vests"),
    "pants": ("pant", "pants", "trouser", "trousers", "slacks"),
    "sport coat": ("sport coat", "sportcoat", "blazer", "jacket"),
}

PANTS_WORDS = re.compile(r"\b(pant|pants|trouser|trousers|slack|slacks)\b", re.IGNORECASE)

# Pants are stocked uncut, so the size is the waist alone: "32", "34", "40".
# "34x32", "34R" or "34/30" carry a length and are refused.
WAIST_SIZE_PATTERN = re.compile(r"^\d{2}$")

# "3pc", "3 pc", "3-piece", "three piece", "3pcs" ...
THREE_PIECE_PATTERN = re.compile(r"\b(3|three)\s*-?\s*(pc|pcs|pce|piece|pieces)\b", re.IGNORECASE)

# Boys' and children's wear.
BOYS_PATTERN = re.compile(r"\b(boy|boys|boy's|boys'|kid|kids|kid's|child|children|children's|youth|toddler)\b",
                          re.IGNORECASE)

# Words with digits that are still plain language, e.g. "Super 120s", "2pc",
# "2-button", "100%". Any other word containing a digit is treated as a vendor
# code (like "320-0S2" or "A0000200IX") and refused in a matrix description.
PLAIN_WORDS_WITH_DIGITS = re.compile(r"^(\d{2,3}'?s|2pc|\d-button|\d+%)$", re.IGNORECASE)

# Sizes, allowed in an item's description (a matrix child is "Caravelli Black 42R Slim")
# but not in a matrix's: suit chest + length (42R, 40XL, 50S), waist (34), shirt neck
# (15.5, 16½) and sleeve (32/33), shoe size and width (10, 10.5D, 9EE, 11W), 3XL.
SIZE_WORD = re.compile(r"^(\d{1,2}(\.5|½)?(S|R|L|XL|XXL|M|W|[A-E]{1,3})?|\d{2}/\d{2})$", re.IGNORECASE)


class RuleViolation(LightspeedError, ValueError):
    """A request broke one of the store's rules. Nothing was sent."""


def clean(value) -> str:
    """Trim outer spaces only. Never changes case or spelling (vendor naming is kept as is)."""
    return "" if value is None else str(value).strip()


# ---- text checks --------------------------------------------------------------------


def check_not_three_piece(*texts: str) -> None:
    for text in texts:
        if text and THREE_PIECE_PATTERN.search(text):
            raise RuleViolation(
                f"Refused: '{text}' looks like a 3-piece item. The store has no 3-piece items: a 3pc is "
                "stored as a suit plus a vest, each in its own matrix."
            )


def check_not_boys(*texts: str) -> None:
    for text in texts:
        if text and BOYS_PATTERN.search(text):
            raise RuleViolation(f"Refused: '{text}' looks like a boys' or children's item. The store does not stock boys' items.")


def vendor_codes_in(description: str, allow_sizes: bool = False) -> list[str]:
    """Words that look like vendor codes: anything containing a digit that is not a plain word (or a size)."""
    codes = []
    for word in re.split(r"\s+", description.strip()):
        word = word.strip(",.()")
        if not re.search(r"\d", word) or PLAIN_WORDS_WITH_DIGITS.match(word):
            continue
        if allow_sizes and SIZE_WORD.match(word):
            continue
        codes.append(word)
    return codes


def check_plain_description(description: str, what: str = "description", allow_sizes: bool = False) -> str:
    """A plain-language description: not empty, not too long, not 3-piece or boys', no vendor codes.

    allow_sizes: an item's description may carry its size ("Caravelli Black 42R Slim"); a matrix's may not.
    """
    description = clean(description)
    if not description:
        raise RuleViolation(f"Refused: the {what} is empty.")
    if len(description) > MAX_DESCRIPTION_LENGTH:
        raise RuleViolation(f"Refused: the {what} is {len(description)} characters; Lightspeed allows {MAX_DESCRIPTION_LENGTH}.")
    check_not_three_piece(description)
    check_not_boys(description)
    codes = vendor_codes_in(description, allow_sizes)
    if codes:
        raise RuleViolation(
            f"Refused: the {what} '{description}' contains what looks like a vendor code ({', '.join(codes)}). "
            "Descriptions are plain language, e.g. 'Caravelli Suit'. Keep style numbers out of the description."
        )
    return description


# ---- matrices -----------------------------------------------------------------------


def check_garment_type(garment_type: str, description: str) -> str:
    """The garment type is one of GARMENT_WORDS and the description names it."""
    garment = clean(garment_type).lower()
    check_not_three_piece(garment)
    check_not_boys(garment)
    if garment in ("2pc", "2pc suit", "2-piece suit", "two piece suit"):
        garment = "suit"
    if garment in ("tux",):
        garment = "tuxedo"
    if garment not in GARMENT_WORDS:
        raise RuleViolation(
            f"Refused: garment type '{garment_type}' is not one of {', '.join(GARMENT_WORDS)}. Matrices are split by "
            "garment type; a 3pc is a suit plus a vest."
        )
    words = GARMENT_WORDS[garment]
    if not any(re.search(rf"\b{re.escape(w)}\b", description, re.IGNORECASE) for w in words):
        raise RuleViolation(
            f"Refused: the matrix description '{description}' does not say it is a {garment}. Put the garment type "
            f"in the description (one of: {', '.join(words)}), e.g. 'Caravelli {words[0].title()}'."
        )
    return garment


def check_attribute_set(attribute_set_id) -> None:
    if str(attribute_set_id) != str(STORE_ATTRIBUTE_SET_ID):
        raise RuleViolation(
            f"Refused: this uses attribute set {attribute_set_id}. New matrices and variants use the store's own "
            f"attribute set {STORE_ATTRIBUTE_SET_ID} 'Color/Size/Fit' (color is an attribute, never its own matrix)."
        )


def check_values(kind: str, values: list[str], required: bool = True) -> list[str]:
    """Trim each value; refuse blanks and duplicates (ignoring case). Spelling and case are kept as given."""
    cleaned = [clean(v) for v in values or []]
    if required and not cleaned:
        raise RuleViolation(f"Refused: give at least one {kind} value.")
    if any(not v for v in cleaned):
        raise RuleViolation(f"Refused: one of the {kind} values is blank.")
    seen: dict[str, str] = {}
    for v in cleaned:
        if v.lower() in seen:
            raise RuleViolation(f"Refused: {kind} '{v}' is listed twice (as '{seen[v.lower()]}' and '{v}').")
        seen[v.lower()] = v
    check_not_three_piece(*cleaned)
    check_not_boys(*cleaned)
    return cleaned


# Color words looked for in a new matrix's description, on top of every color
# value the store's matrices already use.
COMMON_COLOR_WORDS = ("black", "white", "ivory", "cream", "navy", "blue", "grey", "gray", "charcoal", "brown",
                      "tan", "beige", "khaki", "taupe", "burgundy", "wine", "red", "green", "olive", "purple",
                      "pink", "silver", "gold", "orange", "yellow")


def check_not_split_by_color(description: str, colors, ignore_names=()) -> None:
    """A matrix holds every color of a garment, so its description never names a color.

    `ignore_names` are the matrix's own vendor and manufacturer names: a brand
    like "Ascott Brown" is not a color split.
    """
    text = description
    for name in ignore_names:
        if name:
            text = re.sub(re.escape(name), " ", text, flags=re.IGNORECASE)
    for color in sorted({c for c in colors if c}, key=len, reverse=True):
        found = re.search(rf"(?<!\w){re.escape(color)}(?!\w)", text, re.IGNORECASE)
        if found:
            raise RuleViolation(
                f"Refused: the matrix description '{description}' names the color '{found.group(0)}'. Matrices are split by "
                "garment type, never by color: color is an attribute value inside the matrix."
            )


def is_pants(*texts: str) -> bool:
    return any(text and PANTS_WORDS.search(text) for text in texts)


def check_waist_sizes(sizes: list[str]) -> None:
    """Pants are stocked uncut: sizes are the waist alone."""
    bad = [s for s in sizes if not WAIST_SIZE_PATTERN.match(s)]
    if bad:
        raise RuleViolation(
            f"Refused: pants are stocked uncut and sized by waist only, e.g. '32' or '34'. Not valid: {', '.join(bad)}."
        )


# ---- SKUs ---------------------------------------------------------------------------


def check_custom_sku_format(sku: str) -> str:
    sku = clean(sku)
    if not CUSTOM_SKU_PATTERN.match(sku):
        raise RuleViolation(
            f"Refused: custom SKU '{sku}' is not a 6-digit number like '012911', which is the store's format. "
            "Pass it as text so leading zeros are kept."
        )
    return sku


def check_skus_unique_in_request(skus: list[str]) -> None:
    seen = set()
    for sku in skus:
        if sku in seen:
            raise RuleViolation(f"Refused: custom SKU {sku} is used twice in this request. Every item gets its own SKU.")
        seen.add(sku)
