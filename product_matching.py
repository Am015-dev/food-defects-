"""Match e-food listings to a shared Product identity across shops/chains.

Different chains phrase the same product's name differently (brand-first
vs description-first, different size/pack phrasing), so exact-string
matching (the old approach in queries.py) mostly only matched a chain's
own multiple locations against each other. This module links listings by
fuzzy text similarity on the "core" name -- brand + description, with
the trailing size/quantity token stripped out, since pack-size
differences across listings are already handled fairly by
price_utils' unit-price normalization; product identity should group
the product *line*, not the exact SKU.

Runs at ingest time only (see ingest.py) -- never in the web app, which
must stay memory-light. Matching results are cached in ProductListing
(db.py) keyed by (shop_id, code), so only genuinely new listings ever
pay the matching cost.
"""

import re
from datetime import datetime, timezone

from rapidfuzz import fuzz

from db import Product
from price_utils import fold_name

# A conservative threshold: a missed link just means one fewer cross-shop
# comparison (today's baseline without this module at all); a wrong link
# corrupts a price comparison in a way a user would notice and distrust.
# Better to under-match than over-match.
MATCH_THRESHOLD = 90

_TRAILING_SIZE_RE = re.compile(
    r"""
    \s+
    (\d+[.,]?\d*\s*x\s*)?      # optional "NxM" multipack prefix, e.g. "6x"
    \d+[.,]?\d*\s*             # the number
    (kg|g|gr|gram|grams|kgs|
     l|lt|ml|liter|liters|
     pieces?|pcs?|packs?|
     τεμ\.?|τμχ|
     washes?|sachets?|rolls?|capsules?|tabs?)
    \.?\s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)


def normalize_for_matching(name):
    """Fold accents/case (see price_utils.fold_name) and strip a trailing
    size/quantity token, so "Anatoli Κουρκουμάς 60g" and "Anatoli
    Κουρκουμάς 100g" -- or the same product with the chain's own size
    phrasing -- compare on the same core text.
    """
    folded = fold_name(name)
    stripped = _TRAILING_SIZE_RE.sub("", folded).strip()
    return stripped or folded


def top_level_category(category):
    """The blocking key: same top-level group convention get_categories()
    uses ("Group || Subgroup" -> "Group") -- loose enough that different
    chains' inconsistent subcategorization doesn't cause false misses,
    but still cuts the candidate pool from the whole catalog down to
    roughly one category's worth of products.
    """
    if not category:
        return None
    return category.split(" || ")[0].strip()


def _embedded_numbers(text):
    """Digit sequences still present after size-stripping -- e.g. the
    "37" in "no37", the "2" in "2% λιπαρά", the "8" in "no 8 μπαλόνι".
    These are almost always exactly the token that distinguishes real
    variants (diaper size, fat %, candle/balloon number) in an
    otherwise near-identical name, and get diluted into an
    above-threshold score by the sheer length of shared surrounding
    text -- so they're checked separately, not left to the fuzzy score.
    """
    return set(re.findall(r"\d+", text))


def best_match(name, candidates):
    """Pure scoring, no DB access -- easy to unit test directly.

    candidates: iterable of (product_id, canonical_name) already
    narrowed to the right block. Returns (product_id, confidence 0-1)
    for the best match at or above MATCH_THRESHOLD, or (None, None) if
    nothing qualifies.
    """
    core = normalize_for_matching(name)
    core_numbers = _embedded_numbers(core)
    best_id, best_score = None, 0
    for product_id, canonical_name in candidates:
        candidate_core = normalize_for_matching(canonical_name)
        # A different embedded number (fat %, diaper/shoe size, candle
        # number, ...) means a different variant, full stop -- no text
        # score, however high, overrides that. Only reject on an actual
        # difference; two listings with no leftover numbers at all
        # (the common case, since the pack size itself was already
        # stripped) are unaffected.
        if core_numbers != _embedded_numbers(candidate_core):
            continue
        # token_sort_ratio, not token_set_ratio: set_ratio treats one
        # name's tokens being a SUBSET of another's as a perfect match,
        # which -- in a catalog full of long descriptive names sharing
        # brand/generic words -- wrongly merged "Coca-Cola Zero" with
        # "Coca-Cola Zero Χωρίς Καφεΐνη" and similar variant pairs at
        # real-catalog scale. sort_ratio is order-independent (still
        # handles brand-first vs description-first) but sensitive to
        # the extra/missing words that actually distinguish variants.
        score = fuzz.token_sort_ratio(core, candidate_core)
        if score > best_score:
            best_id, best_score = product_id, score
    if best_score >= MATCH_THRESHOLD:
        return best_id, best_score / 100
    return None, None


def match_or_create_product(session, name, category, block_cache):
    """Find (or create) the Product this listing belongs to.

    block_cache: a plain {block_key: [(product_id, canonical_name), ...]}
    dict, owned and passed in by the caller (see ingest.py) -- one query
    per newly-encountered block instead of one per item, and updated in
    place as new products are created so later items in the same run see
    them too.

    Returns (product_id, confidence 0-1; 1.0 for a freshly created product).
    """
    block_key = top_level_category(category)
    if block_key not in block_cache:
        block_cache[block_key] = [
            (p.id, p.canonical_name)
            for p in (
                session.query(Product.id, Product.canonical_name)
                .filter(Product.category == block_key)
                .all()
            )
        ]

    candidates = block_cache[block_key]
    product_id, confidence = best_match(name, candidates)
    if product_id is not None:
        return product_id, confidence

    product = Product(canonical_name=name, category=block_key, created_at=datetime.now(timezone.utc))
    session.add(product)
    session.flush()  # assigns product.id
    block_cache[block_key].append((product.id, name))
    return product.id, 1.0
