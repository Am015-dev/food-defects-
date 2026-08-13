"""
Personal price-tracking fetcher for a single Masoutis storefront on e-food.gr.

Note: e-food.gr's Terms of Service likely restrict automated access. This is
intended for low-frequency, personal, non-commercial use against one store,
not bulk/repeated crawling. Respect rate limits and stop using this if
e-food asks you to.

Calls e-food's own consumer app API directly (see efood_client.py) instead
of driving a browser -- no login required for browsing.
"""

import json
from pathlib import Path

from efood_client import fetch_restaurant

RESTAURANT_ID = 9038526  # from the e-food.gr URL: masoytis-9038526

RAW_JSON_PATH = Path(__file__).with_name("masoutis_catalog.json")


def main():
    print(f"Fetching restaurant {RESTAURANT_ID} ...")
    data = fetch_restaurant(RESTAURANT_ID)

    RAW_JSON_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved full raw catalog to {RAW_JSON_PATH.name}\n")

    info = data["information"]
    print(f"Store: {info['title']} - {info['address']['description']}")
    print(f"Open now: {info['is_open']}\n")

    categories = data["menu"]["categories"]
    total_items = sum(len(cat.get("items", [])) for cat in categories)
    print(f"{len(categories)} categories, {total_items} products total.\n")

    for category in categories:
        items = category.get("items", [])
        if not items:
            continue
        print(f"== {category['name']} ({len(items)} items) ==")
        for item in items:
            size = f" ({item['size_info']})" if item.get("size_info") else ""
            print(f"- {item['name']} | {item['calculated_price']}{size}")
        print()


if __name__ == "__main__":
    main()
