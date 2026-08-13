"""
Scans the Masoutis (e-food.gr) catalog fetched by masoutis_scraper.py for
pricing anomalies: outright bugs (e.g. zero prices, placeholder reference
prices) and genuinely large, verified discounts.

Run masoutis_scraper.py first to produce masoutis_catalog.json.
"""

import json
from pathlib import Path

from price_analysis import (
    find_placeholder_reference_price_bugs,
    find_verified_deep_discounts,
    find_zero_price_bugs,
    l30d_price,
    load_items,
)

CATALOG_PATH = Path(__file__).with_name("masoutis_catalog.json")


def main():
    data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    items = load_items(data)
    print(f"Loaded {len(items)} unique products.\n")

    zero_price = find_zero_price_bugs(items)
    print(f"BUG: {len(zero_price)} listing(s) priced at 0.00 EUR:")
    for it in zero_price:
        print(f"  - {it['name']} [{it['_category']}] id={it['id']}")

    placeholder = find_placeholder_reference_price_bugs(items)
    print(f"\nBUG: {len(placeholder)} item(s) with an implausible near-zero "
          f"'30-day-low' reference price (data placeholder, not a real "
          f"historical price):")
    for it in placeholder[:15]:
        l = l30d_price(it)
        print(f"  - {it['name']} [{it['_category']}]: now {it['price']}€, "
              f"tagged 30-day-low {l}€")
    if len(placeholder) > 15:
        print(f"  ... and {len(placeholder) - 15} more")

    deals = find_verified_deep_discounts(items)
    print(f"\nGENUINE DEALS: {len(deals)} fixed-size item(s) verified at "
          f"20%+ below their real 30-day low:")
    for d in deals[:20]:
        it, l, pct = d["item"], d["l30d"], d["pct"]
        print(f"  - {it['name']} [{it['_category']}]: now {it['price']}€ "
              f"(was {it['full_price']}€) vs 30-day-low {l}€ "
              f"-> {pct:.1f}% below recent low")


if __name__ == "__main__":
    main()
