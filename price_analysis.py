"""Pricing-anomaly detection over an e-food.gr store catalog.

e-food tags each item with its lowest price over the last 30 days, e.g.
tags: ["l30d:5.57"] -- the EU/Greek "Omnibus Directive" reference price
that any advertised discount must be measured against.
"""


def load_items(data):
    """Flatten a store's categories into a deduplicated list of items."""
    items = []
    seen_ids = set()
    for category in data["menu"]["categories"]:
        for item in category.get("items", []):
            if item["id"] in seen_ids:
                continue  # same product listed under multiple categories
            seen_ids.add(item["id"])
            item = dict(item)
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
    return [it for it in items if (l30d := l30d_price(it)) is not None and l30d <= threshold]


def find_verified_deep_discounts(items, min_pct=20.0):
    """Fixed-size items genuinely priced below their real 30-day low, with
    a displayed discount badge. Excludes sold-by-weight items, where the
    30-day-low may have been recorded at a different purchase weight and
    so isn't a reliable like-for-like comparison."""
    results = []
    for it in items:
        l30d = l30d_price(it)
        price = it.get("price")
        full_price = it.get("full_price")
        tags = it.get("tags") or []
        if not (l30d and price and full_price and l30d > 0.05 and 0 < price < l30d):
            continue
        if full_price <= price or "sold_by_weight" in tags:
            continue
        pct = (l30d - price) / l30d * 100
        if pct >= min_pct:
            results.append({"item": it, "l30d": l30d, "pct": pct})
    results.sort(key=lambda x: x["pct"], reverse=True)
    return results


def analyze(data):
    """Run the full anomaly analysis on a store's raw catalog payload."""
    items = load_items(data)
    return {
        "information": data.get("information", {}),
        "total_items": len(items),
        "total_categories": len(data["menu"]["categories"]),
        "zero_price_bugs": find_zero_price_bugs(items),
        "placeholder_reference_bugs": find_placeholder_reference_price_bugs(items),
        "verified_deals": find_verified_deep_discounts(items),
    }
