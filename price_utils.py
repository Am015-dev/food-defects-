"""Price normalization utilities for comparing products across different package sizes."""

import re
from typing import Optional, Tuple


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
        elif unit in ('piece', 'pieces', 'pack', 'packs', 'item', 'items', 'τμχ', 'τεμ'):
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


def format_normalized_price(normalized_price: Optional[float], unit: str = 'auto', size_info: Optional[str] = None) -> str:
    """Format normalized price for display.

    Args:
        normalized_price: The normalized price value
        unit: Display unit ('per_100g', 'per_item', 'auto' = detect from size_info)
        size_info: The size info string, used for auto-detection
    """
    if normalized_price is None:
        return '—'

    # Auto-detect unit from size_info if needed
    if unit == 'auto' and size_info:
        _, unit_type = parse_size_info(size_info)
        if unit_type == 'count':
            unit = 'per_item'
        else:
            unit = 'per_100g'
    elif unit == 'auto':
        unit = 'per_100g'

    if unit == 'per_100g':
        return f'{normalized_price:.2f}€/100g'.replace('.', ',')
    elif unit == 'per_item':
        return f'{normalized_price:.2f}€/piece'.replace('.', ',')
    else:
        return f'{normalized_price:.2f}€'.replace('.', ',')


def get_price_comparison_info(price: float, size_info: Optional[str]) -> dict:
    """Return a dict with raw price, normalized price, and display strings."""
    normalized = normalize_price(price, size_info)

    return {
        'price': price,
        'size_info': size_info,
        'normalized_price': normalized,
        'normalized_display': format_normalized_price(normalized, unit='auto', size_info=size_info),
        'has_size_info': size_info is not None and parse_size_info(size_info)[0] is not None,
    }
