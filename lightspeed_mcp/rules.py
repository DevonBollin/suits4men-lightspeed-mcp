"""The store's business rules for new and changed inventory records.

The write tools call these before they build a payload. Each check raises
RuleViolation with a message that says what was refused and why, so Claude
can pass it straight on. Nothing here talks to Lightspeed.

The rules (from the store owner, and since 2026-10-27 the owner's draft "Suits 4 Men —
Lightspeed R-Series Inventory Structure", §1, §2 and §4):
  * Every garment type has one matrix type (GARMENTS below), and every matrix of
    that garment type uses it: MT1 Color/Size/Fit (the store's set 5) for tailored
    clothing and dress and tux shirts, MT2 Color/Size/Width for shoes, MT3
    Lightspeed's built-in Color/Size for garments without a fit (casual vests,
    outerwear jackets, casual pants and denim, sweaters, belts), MT4 the built-in
    Color for one-size accessories. Attributes are always in that order.
  * Every attribute set the store uses has Color: color is an attribute
    value, never a separate matrix.
  * Matrices are split by garment type (suit, tuxedo, pants, ...), never by color.
    A matrix sits in the category of its garment type (§3), and its description
    names the garment (shoes: the style name stands in).
  * Color and fit values use each vendor's own naming. They are sent exactly
    as given (outer spaces trimmed), never re-cased or "corrected". A line with
    one unnamed fit uses Fit "Standard"; Fit and Width are never blank.
  * Size is the one attribute with a store format, per garment type (SIZE_FORMATS):
    40R for jackets, suits and vests; the waist alone for pants (stocked uncut);
    '15.5 34/35' for shirts; 10.5 for shoes; S-6XL for the rest.
  * No 3-piece items: a 3pc is stored as a suit plus a vest.
  * No boys' items.
  * Matrix descriptions are plain language, with no vendor codes.
  * Every item gets its own custom SKU; duplicates are refused.

Some of these are worked out from text (e.g. "3pc" in a description). The
patterns are below; if a legitimate word gets refused, adjust its pattern.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import LightspeedError

# The store's suiting set: attribute1 = Color, attribute2 = Size, attribute3 = Fit.
STORE_ATTRIBUTE_SET_ID = 5

# Custom SKUs in this store are 6-digit numbers, e.g. "012911" (NOTES.md).
# Kept as text so leading zeros survive.
CUSTOM_SKU_PATTERN = re.compile(r"^\d{6}$")

# Lightspeed's limit on description fields.
MAX_DESCRIPTION_LENGTH = 255

# ---- garment types and matrix types (the draft, §2 "Matrix types" and §4) ----------------------------

# A matrix type is the attribute set a garment's matrices use: its attribute names, in order.
MATRIX_TYPES = {
    "MT1": ("Color", "Size", "Fit"),    # tailored: the store's set 5
    "MT2": ("Color", "Size", "Width"),  # footwear: a store-made set (create_attribute_set "Color/Size/Width")
    "MT3": ("Color", "Size"),           # sized, no fit: Lightspeed's built-in set "Color/Size"
    "MT4": ("Color",),                  # one size, tracked by color: Lightspeed's built-in set "Color"
}

# The store's size formats: (pattern, how to say it). Values are compared as given (no re-casing).
SIZE_FORMATS = {
    "jacket": (re.compile(r"^\d{2}(S|R|L|XL)$"), "chest and length, e.g. 40R, 42L, 44XL"),
    "waist": (re.compile(r"^\d{2}$"), "the waist alone, e.g. 34 (pants are stocked uncut)"),
    "waist or hemmed": (re.compile(r"^\d{2}[SRL]?$"), "the waist, e.g. 34, or for pants shipped hemmed 34S / 34R / 34L"),
    "shirt": (re.compile(r"^\d{2}(\.5)? \d{2}/\d{2}$"), "neck and sleeve, e.g. '15.5 34/35'"),
    "shoe": (re.compile(r"^\d{1,2}(\.5)?$"), "a shoe size, e.g. 10.5"),
    "alpha": (re.compile(r"^(XS|S|M|L|XL|[2-6]XL)$"), "S, M, L, XL, 2XL ... 6XL"),
}


@dataclass(frozen=True)
class Garment:
    """One garment type (§4): its matrix type, size format, the words a matrix description names it
    with (none for shoes: the style name stands in), and the category names that hold it (§3)."""

    name: str
    matrix_type: str
    size: str | None
    words: tuple[str, ...]
    categories: tuple[str, ...]


GARMENTS = {g.name: g for g in (
    Garment("suit", "MT1", "jacket", ("suit", "suits", "2pc"), ("suits", "suit")),
    Garment("tuxedo", "MT1", "jacket", ("tuxedo", "tuxedos", "tux", "tailcoat"), ("tuxedos",)),
    Garment("tux jacket", "MT1", "jacket", ("tuxedo jacket", "tux jacket", "dinner jacket"), ("tux & dinner jackets",)),
    Garment("sport coat", "MT1", "jacket", ("sport coat", "sportcoat", "blazer", "jacket"),
            ("sport coats & blazers", "sport coats")),
    Garment("vest", "MT1", "jacket", ("vest", "vests"), ("suit vests", "tux vests", "vests")),
    Garment("topcoat", "MT1", "jacket", ("topcoat", "top coat", "car coat", "raincoat", "overcoat"), ("top coats",)),
    Garment("jacket", "MT3", "alpha", ("jacket",), ("jackets",)),
    Garment("casual vest", "MT3", "alpha", ("vest",), ("casual vests",)),
    Garment("pants", "MT1", "waist", ("pants", "pant", "trousers", "trouser", "slacks"), ("dress pants", "pants")),
    Garment("casual pants", "MT3", "waist or hemmed", ("pants", "pant", "denim", "jeans"), ("casual pants & denim",)),
    Garment("dress shirt", "MT1", "shirt", ("shirt",), ("dress shirts", "shirts")),
    Garment("tux shirt", "MT1", "shirt", ("tuxedo shirt", "tux shirt"), ("tux shirts",)),
    Garment("sweater", "MT3", "alpha", ("sweater", "quarter-zip", "cardigan"), ("sweaters",)),
    Garment("shoes", "MT2", "shoe", (), ("shoes",)),
    # Accessories are deferred in the draft (§4.15); these two are its provisional types.
    Garment("belt", "MT3", "waist", ("belt", "belts"), ("belts",)),
    Garment("accessory", "MT4", None, ("tie", "ties", "bow tie", "bowtie", "pocket square", "suspenders", "scarf"),
            ("neck ties", "bowties", "bow ties", "pocket squares", "suspenders", "scarves")),
)}

# Other names for a garment type, as create_matrix may be given it.
GARMENT_ALIASES = {
    "2pc": "suit", "2pc suit": "suit", "2-piece suit": "suit", "two piece suit": "suit", "tux": "tuxedo",
    "tuxedo jacket": "tux jacket", "dinner jacket": "tux jacket", "blazer": "sport coat", "suit vest": "vest",
    "tux vest": "vest", "tuxedo vest": "vest", "top coat": "topcoat", "outerwear jacket": "jacket",
    "dress pants": "pants", "denim": "casual pants", "jeans": "casual pants", "shirt": "dress shirt",
    "tuxedo shirt": "tux shirt", "shoe": "shoes", "tie": "accessory",
}

# Telling an existing matrix's garment type from its description when its category doesn't say:
# the more specific phrases first. A bare "jacket" (sport coat or outerwear?) decides nothing.
DESCRIPTION_GARMENTS = (
    ("tuxedo jacket", "tux jacket"), ("tux jacket", "tux jacket"), ("dinner jacket", "tux jacket"),
    ("tuxedo shirt", "tux shirt"), ("tux shirt", "tux shirt"), ("tuxedo vest", "vest"), ("tux vest", "vest"),
    ("sport coat", "sport coat"), ("sportcoat", "sport coat"), ("blazer", "sport coat"), ("topcoat", "topcoat"),
    ("top coat", "topcoat"), ("car coat", "topcoat"), ("raincoat", "topcoat"), ("overcoat", "topcoat"),
    ("sweater", "sweater"), ("cardigan", "sweater"), ("quarter-zip", "sweater"), ("denim", "casual pants"),
    ("jeans", "casual pants"), ("shirt", "dress shirt"), ("vest", "vest"), ("pants", "pants"), ("pant", "pants"),
    ("trousers", "pants"), ("slacks", "pants"), ("belt", "belt"), ("tuxedo", "tuxedo"), ("tux", "tuxedo"),
    ("tailcoat", "tuxedo"), ("suit", "suit"), ("suits", "suit"), ("2pc", "suit"),
)

PANTS_WORDS = re.compile(r"\b(pant|pants|trouser|trousers|slack|slacks)\b", re.IGNORECASE)

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


def names_garment(garment: Garment, description: str) -> bool:
    """Does the description name this garment type? (Shoes: always; the style name stands in.)"""
    return not garment.words or any(re.search(rf"(?<!\w){re.escape(w)}(?!\w)", description or "", re.IGNORECASE)
                                    for w in garment.words)


def garment_for_category(category_path: str) -> Garment | None:
    """The garment type a category holds (§3), from its own name or the nearest parent that says."""
    by_category = {c: g for g in GARMENTS.values() for c in g.categories}
    for part in reversed([p.strip().lower() for p in (category_path or "").split("/") if p.strip()]):
        if part in by_category:
            return by_category[part]
    return None


def garment_of(description: str, category_path: str = "") -> Garment | None:
    """An existing matrix's garment type: its category says (§3); failing that, its description."""
    garment = garment_for_category(category_path)
    if garment:
        return garment
    for phrase, name in DESCRIPTION_GARMENTS:
        if re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", description or "", re.IGNORECASE):
            return GARMENTS[name]
    return None


def check_garment_type(garment_type: str, description: str, category_path: str | None = None) -> Garment:
    """A new matrix's garment type: one of GARMENTS (or an alias), named in its description, and (when its
    category holds a garment type) the one its category holds."""
    name = clean(garment_type).lower()
    check_not_three_piece(name)
    check_not_boys(name)
    name = GARMENT_ALIASES.get(name, name)
    if name not in GARMENTS:
        raise RuleViolation(
            f"Refused: garment type '{garment_type}' is not one of {', '.join(GARMENTS)}. Matrices are split by "
            "garment type; a 3pc is a suit plus a vest."
        )
    garment = GARMENTS[name]
    if not names_garment(garment, description):
        raise RuleViolation(
            f"Refused: the matrix description '{description}' does not say it is a {garment.name}. Put the garment in "
            f"the description (one of: {', '.join(garment.words)}), e.g. 'Caravelli {garment.words[0].title()}'."
        )
    held = garment_for_category(category_path) if category_path is not None else None
    if held and held is not garment:
        raise RuleViolation(
            f"Refused: category '{category_path}' holds {held.name} matrices, not {garment.name}. Every matrix sits in "
            f"its garment type's category: create or move that category first (create_category, move_category)."
        )
    return garment


def check_matrix_attribute_set(attribute_set: dict | None, garment: Garment | None) -> dict[int, str]:
    """The set a matrix uses: the matrix type of its garment type (attribute names in that order; built-in
    sets included, as the draft uses them for MT3 and MT4), with Color. A matrix whose garment type isn't
    known needs only Color. Returns its attribute names by slot, e.g. {1: "Color", 2: "Size", 3: "Fit"}."""
    if not attribute_set:
        raise RuleViolation("Refused: that attribute set doesn't exist.")
    set_id = str(attribute_set.get("itemAttributeSetID"))
    label = f"attribute set {set_id} '{attribute_set.get('name')}'"
    if str(attribute_set.get("archived")).lower() == "true":
        raise RuleViolation(f"Refused: {label} is archived.")
    slots = {i: clean(attribute_set.get(f"attributeName{i}")) for i in (1, 2, 3)}
    slots = {i: name for i, name in slots.items() if name}
    if not any(name.lower() == "color" for name in slots.values()):
        raise RuleViolation(f"Refused: {label} has no Color attribute. Color is always an attribute, never its own "
                            "matrix.")
    if garment:
        wanted = MATRIX_TYPES[garment.matrix_type]
        if [slots.get(i, "").lower() for i in range(1, len(wanted) + 1)] != [w.lower() for w in wanted] \
                or len(slots) != len(wanted):
            raise RuleViolation(
                f"Refused: {garment.name} matrices use matrix type {garment.matrix_type} ({' / '.join(wanted)}, in that "
                f"order), but {label} is {' / '.join(slots.values())}."
                + (" Create that set first (create_attribute_set)." if garment.matrix_type == "MT2" else ""))
    return slots


def check_attribute_names(names: list) -> list[str]:
    """1 to 3 distinct attribute names for a set, one of them Color."""
    cleaned = [clean(n) for n in names or [] if clean(n)]
    if not 1 <= len(cleaned) <= 3:
        raise RuleViolation("Refused: an attribute set has 1 to 3 attributes.")
    if len({n.lower() for n in cleaned}) != len(cleaned):
        raise RuleViolation("Refused: an attribute set can't have the same attribute twice.")
    if not any(n.lower() == "color" for n in cleaned):
        raise RuleViolation("Refused: every store attribute set has Color (color is an attribute, never its own "
                            "matrix).")
    for n in cleaned:
        if "/" in n or len(n) > 50:
            raise RuleViolation(f"Refused: attribute name '{n}' (no '/', at most 50 characters).")
        check_not_boys(n)
    return cleaned


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


def check_sizes(garment: Garment | None, sizes: list[str]) -> None:
    """New Size values follow the garment type's size format (§4). Unknown garment type: not checked."""
    if not garment or not garment.size:
        return
    pattern, format_text = SIZE_FORMATS[garment.size]
    bad = [s for s in sizes if not pattern.match(s)]
    if bad:
        raise RuleViolation(f"Refused: {garment.name} sizes are {format_text}. Not valid: {', '.join(bad)}.")


# ---- SKUs ---------------------------------------------------------------------------


def check_custom_sku_format(sku: str) -> str:
    sku = clean(sku)
    if not CUSTOM_SKU_PATTERN.match(sku):
        raise RuleViolation(
            f"Refused: custom SKU '{sku}' is not a 6-digit number like '012911', which is the store's format. "
            "Pass it as text so leading zeros are kept."
        )
    return sku


def check_upc(upc) -> str:
    """A barcode number as printed on the box or tag (UPC or EAN). Lightspeed's rule: 11-14 digits.
    With 12-14 digits the last one is a check digit (GTIN), so a misread digit is caught here
    instead of making the label unscannable."""
    code = clean(upc).replace(" ", "").replace("-", "")
    if not re.fullmatch(r"\d{11,14}", code):
        raise RuleViolation(f"Refused: UPC '{upc}' must be 11 to 14 digits (Lightspeed's rule), as printed under the "
                            "barcode.")
    if len(code) >= 12:
        body, check = code[:-1], int(code[-1])
        # GTIN: from the right, the digits before the check digit are weighted 3, 1, 3, 1, ...
        total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
        if (10 - total % 10) % 10 != check:
            raise RuleViolation(f"Refused: UPC '{upc}' fails its check digit, so a digit is probably misread. Check "
                                "the number under the barcode.")
    return code


# ---- vendor and brand names ---------------------------------------------------------------

# Words that don't make two company names different: "San Malone" = "San Malone, Inc.".
COMPANY_SUFFIXES = {"inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation", "co", "company"}

# How alike two names must be to count as "probably the same company" (difflib ratio, 0-1).
# Catches typos like "Tommmy Hilfiger" / "Tommy Hilfiger" or "Canaletto" / "Canoletto".
SIMILAR_NAME_RATIO = 0.85


def name_key(name: str) -> str:
    """'San Malone, Inc.' -> 'san malone'. Case, punctuation and company suffixes don't count."""
    words = re.findall(r"[a-z0-9]+", clean(name).lower().replace("&", " and "))
    while len(words) > 1 and words[-1] in COMPANY_SUFFIXES:
        words.pop()
    return " ".join(words)


def check_record_name(name, kind: str) -> str:
    """A vendor's or brand's name, trimmed but otherwise exactly as given."""
    name = clean(name)
    if not name_key(name):
        raise RuleViolation(f"Refused: the {kind} name is empty.")
    if len(name) > MAX_DESCRIPTION_LENGTH:
        raise RuleViolation(f"Refused: the {kind} name is {len(name)} characters; Lightspeed allows {MAX_DESCRIPTION_LENGTH}.")
    return name


def same_and_similar_names(name: str, existing: list[dict]) -> tuple[list[dict], list[dict]]:
    """Existing records ({"id", "name", "archived"}) with the same name, and with a similar one."""
    from difflib import SequenceMatcher

    key = name_key(name)
    same, similar = [], []
    for record in existing:
        other = name_key(record["name"])
        if not other:
            continue
        if other == key:
            same.append(record)
        elif (SequenceMatcher(None, key, other).ratio() >= SIMILAR_NAME_RATIO
              or re.search(rf"\b{re.escape(key)}\b", other) or re.search(rf"\b{re.escape(other)}\b", key)):
            similar.append(record)
    return same, similar


def check_skus_unique_in_request(skus: list[str]) -> None:
    seen = set()
    for sku in skus:
        if sku in seen:
            raise RuleViolation(f"Refused: custom SKU {sku} is used twice in this request. Every item gets its own SKU.")
        seen.add(sku)
