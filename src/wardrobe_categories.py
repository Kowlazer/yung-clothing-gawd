"""Canonical wardrobe garment taxonomy — one source of truth.

Shared by the three places that need to agree on the category vocabulary:

  * ``src/order_extract.py`` — Claude stamps a ``category`` per item while
    parsing an order email (full body context).
  * ``src/order_classify.py`` — the ``order_scan --classify`` backfill stamps
    a ``category`` onto already-stored items (name + shop + size + colour).
  * ``src/wardrobe_browser.py`` — the local browser reads the stored category
    (falling back to a name heuristic for items not yet classified).

Each entry is ``(key, label, description)``. ``key`` is the durable value
stored on ``wardrobe.json`` items and used everywhere as the join field;
``label`` is the browser's display name; ``description`` is the human gloss
handed to Claude so its choices land on these exact keys.

``CATEGORIES`` is in the browser's preferred *display* order (tops -> bottoms
-> footwear -> accessories). The name-heuristic fallback in the browser keeps
its own *specificity*-ordered regex list — these keys are the shared
vocabulary, not the matching order.

``non_clothing`` is the sentinel for anything that is not a wearable garment.
An item classified ``non_clothing`` is stored with ``is_clothing = False`` and
hidden by the browser; every other key implies clothing.
"""
from __future__ import annotations

# (key, label, description-for-Claude). Display order.
CATEGORIES: list[tuple[str, str, str]] = [
    ("tshirt",      "T-Shirts",                "short-sleeve t-shirts and graphic tees"),
    ("longsleeve",  "Long Sleeves",            "long-sleeve shirts/tees with no buttons"),
    ("shirt",       "Shirts",                  "button-up/button-down shirts, flannels, jerseys, overshirts"),
    ("polo",        "Polos",                   "polo / golf collared knit shirts"),
    ("tank",        "Tank Tops",               "tank tops and sleeveless shirts"),
    ("sweatshirt",  "Sweaters & Sweatshirts",  "crewneck sweatshirts, sweaters, cardigans, pullovers WITHOUT a hood"),
    ("hoodie",      "Hoodies",                 "hooded sweatshirts / hoodies"),
    ("jacket",      "Jackets & Coats",         "jackets, coats, parkas, vests, windbreakers, bombers, puffers"),
    ("pants",       "Pants & Jeans",           "pants, jeans, chinos, trousers, slacks, leggings"),
    ("sweatpants",  "Sweatpants & Joggers",    "sweatpants, joggers, track pants"),
    ("shorts",          "Shorts",              "shorts whose type (athletic vs casual) is unclear from the name/shop"),
    ("shorts_athletic", "Athletic Shorts",     "athletic / gym / running / training / performance shorts: mesh, lined, "
                                               "compression, sport, basketball, and activewear-brand shorts"),
    ("shorts_casual",   "Casual Shorts",       "everyday casual shorts: chino, cargo, denim, sweatshorts, lounge, fleece, "
                                               "board/swim, and graphic-print shorts"),
    ("shoes",       "Shoes",                   "shoes, sneakers, boots, sandals, slippers, slides, loafers"),
    ("socks",       "Socks",                   "socks"),
    ("underwear",   "Underwear",               "underwear, boxers, briefs, base layers"),
    ("hat",         "Hats",                    "hats, caps, beanies, visors, snapbacks"),
    ("accessory",   "Accessories",             "worn fabric accessories: belts, scarves, gloves, ties, bandanas, robes"),
    ("other",       "Other",                   "a wearable clothing garment whose specific type is unclear"),
    ("non_clothing", "Non-clothing",           "NOT a wearable garment: homeware, decor, electronics, supplements, "
                                               "grooming, furniture, software, games, jewelry, watches, sunglasses, bags"),
]

NON_CLOTHING = "non_clothing"

CATEGORY_ORDER: list[str] = [key for key, _label, _desc in CATEGORIES]
CATEGORY_LABELS: dict[str, str] = {key: label for key, label, _desc in CATEGORIES}
VALID_KEYS: frozenset[str] = frozenset(CATEGORY_ORDER)
# Garment (clothing) keys — everything except the non-clothing sentinel.
GARMENT_KEYS: list[str] = [key for key in CATEGORY_ORDER if key != NON_CLOTHING]


def is_garment_category(key: str | None) -> bool:
    """True when ``key`` is a known clothing category (not ``non_clothing``)."""
    return key in VALID_KEYS and key != NON_CLOTHING


def normalise_category(value: str | None) -> str | None:
    """Coerce a raw category string to a valid key, or ``None`` if unusable.

    Lower-cases and trims; maps unknown values to ``None`` so callers fall
    back to the name heuristic rather than storing a bogus key.
    """
    if not isinstance(value, str):
        return None
    key = value.strip().lower()
    return key if key in VALID_KEYS else None


def prompt_category_list() -> str:
    """The category menu block handed to Claude (``key — description`` lines)."""
    return "\n".join(f"  {key} — {desc}" for key, _label, desc in CATEGORIES)
