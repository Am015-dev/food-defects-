"""
Personal price-tracking fetcher for a single Masoutis storefront on e-food.gr.

Note: e-food.gr's Terms of Service likely restrict automated access. This is
intended for low-frequency, personal, non-commercial use against one store,
not bulk/repeated crawling. Respect rate limits and stop using this if
e-food asks you to.

This calls e-food's own consumer app API directly (the same one their
website's JS uses) instead of driving a browser: GET
/api/v1/restaurants/{restaurant_id} on api.e-food.gr returns the full,
structured catalog -- categories, items, names, and prices -- as JSON,
with no login required for browsing. That avoids needing to render and
scrape the public web page's JS-heavy, Cloudflare-protected DOM entirely.

Endpoint shape and headers confirmed against the unofficial efood-mcp
project (https://github.com/DENNISDGR/efood-mcp), which documents this as
a public, unauthenticated endpoint.
"""

import json
from pathlib import Path

import requests

RESTAURANT_ID = 9038526  # from the e-food.gr URL: masoytis-9038526
API_URL = f"https://api.e-food.gr/api/v1/restaurants/{RESTAURANT_ID}"

HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "el-GR,el;q=0.9",
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"
    ),
}

RAW_JSON_PATH = Path(__file__).with_name("masoutis_catalog.json")


def fetch_catalog():
    response = requests.get(API_URL, headers=HEADERS, timeout=20)
    response.raise_for_status()
    payload = response.json()

    if payload.get("status") != "ok":
        raise RuntimeError(f"API returned an error: {payload.get('message')}")

    return payload["data"]


def main():
    print(f"Fetching {API_URL} ...")
    data = fetch_catalog()

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
