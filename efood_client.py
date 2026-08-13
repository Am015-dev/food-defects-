"""Shared HTTP client for e-food.gr's public consumer API.

Note: e-food.gr's Terms of Service likely restrict automated access. This
is intended for low-frequency, personal, non-commercial use, not bulk or
high-frequency crawling.
"""

import requests

BASE_URL = "https://api.e-food.gr"

HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "el-GR,el;q=0.9",
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"
    ),
}


def fetch_restaurant(restaurant_id, timeout=25):
    """Fetch a store's full catalog (categories, items, prices). No login required."""
    url = f"{BASE_URL}/api/v1/restaurants/{restaurant_id}"
    response = requests.get(url, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") != "ok":
        raise RuntimeError(f"API returned an error for {restaurant_id}: {payload.get('message')}")
    return payload["data"]
