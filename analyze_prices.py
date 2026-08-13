"""
Scans the Masoutis (e-food.gr) catalog fetched by masoutis_scraper.py for
pricing anomalies: outright bugs (e.g. zero prices, placeholder reference
prices) and genuinely large, verified discounts.

e-food tags each item with its lowest price over the last 30 days, e.g.
tags: ["l30d:5.57"] -- this is the EU/Greek "Omnibus Directive" reference
price that any advertised discount must be measured against. Comparing it
to the current price is what makes most of the checks below possible.

Run masoutis_scraper.py first to produce masoutis_catalog.json.
"""

import json
import re
from pathlib import Path

CATALOG_PATH = Path(__file__).with_name("masoutis_catalog.json")


def load_items():
    data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    items = []
    seen_ids = set()
    for category in data["menu"]["categories"]:
        for item in category.get("items", []):
            if item["id"] in seen_ids:
                continue  # same product listed under multiple categories
            seen_ids.add(item["id"])
            item["_category"] = category["name"]
            items.append(item)
    return items


def l30d_price(item):
    for tag in item.get("tags") or []:
        if tag.startswith("l30d:"):
            try:
                return float(tag.split(":", 1)[1])
            except ValueError:
                return None
    return None


def find_zero_price_bugs(items):
    return [it for it in items if it.get("price", 0) <= 0]


def find_placeholder_reference_price_bugs(items, threshold=0.05):
    """Items whose 30-day-low tag is an implausible near-zero placeholder."""
    return [it for it in items if (l := l30d_price(it)) is not None and l <= threshold]


def find_verified_deep_discounts(items, min_pct=20.0):
    """Fixed-size items genuinely priced below their real 30-day low, with
    a displayed discount badge. Excludes sold-by-weight items, where the
    30-day-low may have been recorded at a different purchase weight and
    so isn't a reliable like-for-like comparison."""
    results = []
    for it in items:
        l = l30d_price(it)
        price = it.get("price")
        full_price = it.get("full_price")
        tags = it.get("tags") or []
        if not (l and price and full_price and l > 0.05 and 0 < price < l):
            continue
        if full_price <= price or "sold_by_weight" in tags:
            continue
        pct = (l - price) / l * 100
        if pct >= min_pct:
            results.append((it, l, pct))
    results.sort(key=lambda x: x[2], reverse=True)
    return results


def main():
    items = load_items()
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
    for it, l, pct in deals[:20]:
        print(f"  - {it['name']} [{it['_category']}]: now {it['price']}€ "
              f"(was {it['full_price']}€) vs 30-day-low {l}€ "
              f"-> {pct:.1f}% below recent low")


if __name__ == "__main__":
    main()
