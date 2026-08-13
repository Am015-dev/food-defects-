"""Price normalization utilities for comparing products across different package sizes."""

import re
import unicodedata
from typing import Optional, Tuple

COUNT_UNITS = frozenset({
    'piece', 'pieces', 'pack', 'packs', 'item', 'items', 'τμχ', 'τεμ',
    'wash', 'washes', 'sachet', 'sachets', 'roll', 'rolls',
    'capsule', 'capsules', 'tab', 'tabs',
})


def parse_size_info(size_info: Optional[str]) -> Tuple[Optional[float], str]:
    """Parse size_info string and return (quantity, unit_type).

    Handles formats like:
    - "500g", "1.5kg" → (500, 'weight_g')
    - "1.5l", "500ml" → (1500, 'volume_ml')
    - "16x40g" → (640, 'weight_g')
    - "6x2l" → (12000, 'volume_ml')
    - "16pieces", "6pack" → (16, 'count')

    Returns (None, 'unknown') if unable to parse.
    """
    if not size_info or not isinstance(size_info, str):
        return None, 'unknown'

    size_info = size_info.strip().lower()

    # Handle "NxN" format (e.g., "16x40g", "6x2l")
    multi_match = re.match(r'(\d+\.?\d*)\s*x\s*(\d+\.?\d*)\s*([a-z]+)', size_info)
    if multi_match:
        count = float(multi_match.group(1))
        size = float(multi_match.group(2))
        unit = multi_match.group(3)

        if unit in ('g', 'gr', 'gram', 'grams'):
            return count * size, 'weight_g'
        elif unit in ('kg', 'kgs'):
            return count * size * 1000, 'weight_g'
        elif unit in ('l', 'liter', 'liters'):
            return count * size * 1000, 'volume_ml'
        elif unit in ('ml', 'mm3'):
            return count * size, 'volume_ml'
        elif unit in COUNT_UNITS:
            return count * size, 'count'

    # Handle single unit (e.g., "500g", "1.5l", "16pieces")
    single_match = re.match(r'(\d+\.?\d*)\s*([a-z]+)', size_info)
    if single_match:
        size = float(single_match.group(1))
        unit = single_match.group(2)

        if unit in ('g', 'gr', 'gram', 'grams'):
            return size, 'weight_g'
        elif unit in ('kg', 'kgs'):
            return size * 1000, 'weight_g'
        elif unit in ('l', 'liter', 'liters'):
            return size * 1000, 'volume_ml'
        elif unit in ('ml', 'mm3'):
            return size, 'volume_ml'
        elif unit in COUNT_UNITS:
            # Item count - can't normalize to weight without more info
            return size, 'count'

    return None, 'unknown'


def normalize_price(price: float, size_info: Optional[str], normalize_to: str = 'auto') -> Optional[float]:
    """Calculate normalized price for comparison across package sizes.

    Args:
        price: The price in euros
        size_info: The package size string (e.g., "500g", "16pieces")
        normalize_to: Target unit ('per_100g', 'per_item', 'auto' = auto-detect)

    Returns:
        Normalized price, or None if unable to parse/normalize
    """
    if price <= 0:
        return None

    quantity, unit_type = parse_size_info(size_info)
    if quantity is None or quantity <= 0:
        return None

    # Auto-detect normalization method based on unit type
    if normalize_to == 'auto':
        if unit_type in ('weight_g', 'volume_ml'):
            normalize_to = 'per_100g'
        elif unit_type == 'count':
            normalize_to = 'per_item'
        else:
            return None

    if normalize_to == 'per_100g':
        if unit_type in ('weight_g', 'volume_ml'):
            # Convert to price per 100g (or 100ml for liquids)
            return (price / quantity) * 100
    elif normalize_to == 'per_item':
        if unit_type == 'count':
            return price / quantity

    return None


def format_normalized_price(
    normalized_price: Optional[float], unit: str = 'auto', size_info: Optional[str] = None
) -> str:
    """Format normalized price for display.

    Args:
        normalized_price: The normalized price value
        unit: Display unit ('per_100g', 'per_100ml', 'per_item', 'auto' = detect from size_info)
        size_info: The size info string, used for auto-detection
    """
    if normalized_price is None:
        return '—'

    # Auto-detect unit from size_info if needed
    if unit == 'auto' and size_info:
        _, unit_type = parse_size_info(size_info)
        if unit_type == 'count':
            unit = 'per_item'
        elif unit_type == 'volume_ml':
            unit = 'per_100ml'
        else:
            unit = 'per_100g'
    elif unit == 'auto':
        unit = 'per_100g'

    # Comma-ify the decimal point BEFORE appending the unit suffix -- a
    # blind replace('.', ',') afterward would also mangle the literal dot
    # in "τεμ.".
    value = f'{normalized_price:.2f}'.replace('.', ',')
    if unit == 'per_100g':
        return f'{value}€/100g'
    elif unit == 'per_100ml':
        return f'{value}€/100ml'
    elif unit == 'per_item':
        return f'{value}€/τεμ.'
    else:
        return f'{value}€'


def parse_metric_unit_price(metric_unit_description: Optional[str]) -> Tuple[Optional[float], Optional[str]]:
    """Parse e-food's own authoritative unit price, e.g. "3,36€ / kg",
    "1,98€ / lt", "0,22€ / τεμ.". This is already a like-for-like unit
    price computed by e-food itself (it knows the real package weight
    even for sold-by-weight items), so it's preferred over parsing
    size_info wherever it's present.

    Returns (value, unit) or (None, None).
    """
    if not metric_unit_description or not isinstance(metric_unit_description, str):
        return None, None
    match = re.match(r'^([\d.,]+)\s*€\s*/\s*(.+)$', metric_unit_description.strip())
    if not match:
        return None, None
    try:
        value = float(match.group(1).replace(',', '.'))
    except ValueError:
        return None, None
    unit = match.group(2).strip().rstrip('.')
    if not unit:
        return None, None
    return value, unit


def get_price_comparison_info(
    price: float, size_info: Optional[str], metric_unit_description: Optional[str] = None
) -> dict:
    """Return a dict with raw price, normalized price, and display strings.

    Prefers e-food's own metric_unit_description (already a correct,
    per-standard-unit price straight from the source) and only falls back
    to parsing size_info ourselves when it's missing.
    """
    unit_price, unit = parse_metric_unit_price(metric_unit_description)
    if unit_price is not None:
        return {
            'price': price,
            'size_info': size_info,
            'normalized_price': unit_price,
            'normalized_display': f'{unit_price:.2f}€/{unit}'.replace('.', ','),
            'has_size_info': True,
        }

    normalized = normalize_price(price, size_info)
    return {
        'price': price,
        'size_info': size_info,
        'normalized_price': normalized,
        'normalized_display': format_normalized_price(normalized, unit='auto', size_info=size_info),
        'has_size_info': size_info is not None and parse_size_info(size_info)[0] is not None,
    }


def derive_unit_price(
    price: Optional[float], size_info: Optional[str], metric_unit_description: Optional[str] = None
) -> Tuple[Optional[float], Optional[str]]:
    """Best-effort per-standard-unit price for storage/sorting: e-food's
    own metric_unit_description when present, else our own size_info
    parse. Returns (unit_price, unit_kind) or (None, None).

    The two sources use different scales for the same physical quantity
    -- e-food reports weight/volume per whole kg/lt, while our own
    size_info fallback computes per-100g/100ml -- so a raw kg value and
    a raw 100g value differ by a factor of 10 for the same real price.
    Both are normalized to the 100g/100ml scale here so the stored
    unit_price column is one consistent scale that SQL can sort by
    (queries.get_deals_page / search_products ORDER BY unit_price).
    Non-weight/volume units (τεμ, καψ, m, ...) are already "per one base
    thing" on both sides and need no conversion; comparing *across*
    different unit kinds (e.g. per-capsule vs per-meter) is inherently
    apples-to-oranges regardless -- this only fixes the same-kind
    (weight-vs-weight, volume-vs-volume) mismatch.
    """
    value, unit = parse_metric_unit_price(metric_unit_description)
    if value is not None:
        if unit == 'kg':
            return value / 10, '100g'
        elif unit == 'lt':
            return value / 10, '100ml'
        return value, unit

    if price is None or price <= 0:
        return None, None
    quantity, unit_type = parse_size_info(size_info)
    if quantity is None or quantity <= 0:
        return None, None
    if unit_type == 'weight_g':
        return (price / quantity) * 100, '100g'
    elif unit_type == 'volume_ml':
        return (price / quantity) * 100, '100ml'
    elif unit_type == 'count':
        return price / quantity, 'τεμ'
    return None, None


def fold_name(name: Optional[str]) -> str:
    """Accent-strip and casefold a product name for search matching, e.g.
    "Γάλα ΦΡΕΣΚΟ" -> "γαλα φρεσκο". Lets someone type unaccented, any-case
    Greek and still find the product -- plain SQL ILIKE only folds ASCII
    case and leaves accents (and hence whole words) unmatched.
    """
    if not name:
        return ''
    decomposed = unicodedata.normalize('NFD', name)
    stripped = ''.join(c for c in decomposed if unicodedata.category(c) != 'Mn')
    return stripped.casefold()
