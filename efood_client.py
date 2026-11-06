"""Shared HTTP client for e-food.gr's public consumer API.

Note: e-food.gr's Terms of Service likely restrict automated access. This
is intended for low-frequency, personal, non-commercial use, not bulk or
high-frequency crawling.
"""

import os
from urllib.parse import urlencode

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


def auth_headers(auth_header=None):
    """HEADERS plus the user's own login header, if given.

    Since 2026-09-23 e-food answers anonymous catalog calls with
    "authentication_error". EFOOD_AUTH_HEADER holds one header copied from
    the browser's DevTools while logged in, as "Name: value" (for example
    "Authorization: Bearer ..." or "Cookie: ...").
    """
    raw = auth_header if auth_header is not None else os.environ.get("EFOOD_AUTH_HEADER", "")
    if ":" not in raw:
        return HEADERS
    name, value = raw.split(":", 1)
    return {**HEADERS, name.strip(): value.strip()}


def fetch_restaurant(restaurant_id, timeout=25, auth_header=None):
    """Fetch a store's full catalog (categories, items, prices). Needs a login header since 2026-09-23."""
    url = f"{BASE_URL}/api/v1/restaurants/{restaurant_id}"
    response = requests.get(url, headers=auth_headers(auth_header), timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") != "ok":
        raise RuntimeError(f"API returned an error for {restaurant_id}: {payload.get('message')}")
    return payload["data"]


def menu_item_url(restaurant_id, item_code):
    """The public API URL for a single product -- shown on the verification
    page so a finding can be checked against e-food directly."""
    query = urlencode({"item_code": item_code, "restaurant_id": restaurant_id})
    return f"{BASE_URL}/api/v1/restaurants/menuitem/?{query}"


def fetch_menu_item(restaurant_id, item_code, timeout=20):
    """Fetch ONE product's current live data. Tiny compared with a whole
    catalog, so it's safe to do inside a web request."""
    response = requests.get(
        menu_item_url(restaurant_id, item_code), headers=HEADERS, timeout=timeout
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") != "ok":
        raise RuntimeError(f"API returned an error: {payload.get('message')}")
    return payload["data"]
