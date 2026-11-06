"""Daily price snapshots as compressed CSV files in the repo
(data/prices/YYYY-MM-DD.csv.gz), for the Streamlit app.

No database: one file per day, committed to GitHub so the data lives in
the repo. Reuses price_analysis's rules, so the flags match ingest.py.
"""

import base64
import csv
import gzip
import io
import os
from datetime import datetime, timezone
from pathlib import Path

import requests

from efood_client import fetch_restaurant
from price_analysis import (
    find_placeholder_reference_price_bugs,
    find_verified_deep_discounts,
    find_zero_price_bugs,
    l30d_price,
    load_items,
)
from price_utils import derive_unit_price
from shops import SHOPS

DATA_DIR = Path(os.environ.get("PRICES_DIR", Path(__file__).parent / "data" / "prices"))

COLUMNS = [
    "date", "shop_id", "shop", "code", "name", "category", "price", "full_price",
    "l30d_price", "unit_price", "size_info", "zero_price_bug", "placeholder_bug", "deal_pct",
]


def rows_for_shop(shop, data, today):
    """One CSV row per item in one shop's raw catalog payload."""
    items = load_items(data)
    zero = {it["id"] for it in find_zero_price_bugs(items)}
    placeholder = {it["id"] for it in find_placeholder_reference_price_bugs(items)}
    deal_pct = {d["item"]["id"]: round(d["pct"], 1) for d in find_verified_deep_discounts(items)}
    rows = []
    for it in items:
        unit_price, _ = derive_unit_price(
            it.get("price"), it.get("size_info"), it.get("metric_unit_description")
        )
        rows.append({
            "date": today,
            "shop_id": shop["id"],
            "shop": shop["label"],
            "code": it.get("code"),
            "name": it["name"],
            "category": it["_category"],
            "price": it.get("price"),
            "full_price": it.get("full_price"),
            "l30d_price": l30d_price(it),
            "unit_price": unit_price,
            "size_info": it.get("size_info"),
            "zero_price_bug": int(it["id"] in zero),
            "placeholder_bug": int(it["id"] in placeholder),
            "deal_pct": deal_pct.get(it["id"]),
        })
    return rows


def to_csv_gz(rows):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    return gzip.compress(buf.getvalue().encode("utf-8"))


def fetch_today(auth_header=None, progress=None):
    """Fetch every shop. Returns (rows, errors). Sequential on purpose:
    one user, a few times a day at most -- no reason to hit e-food in parallel."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows, errors = [], []
    for i, shop in enumerate(SHOPS):
        if progress:
            progress(i, shop["label"])
        try:
            rows += rows_for_shop(shop, fetch_restaurant(shop["id"], auth_header=auth_header), today)
        except Exception as exc:  # noqa: BLE001 -- report per shop, keep going
            errors.append(f"{shop['label']}: {exc}")
    return today, rows, errors


def save_local(day, blob):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"{day}.csv.gz"
    path.write_bytes(blob)
    return path


def push_to_github(day, blob, token, repo, branch="main"):
    """Create or replace data/prices/<day>.csv.gz through the GitHub contents API."""
    url = f"https://api.github.com/repos/{repo}/contents/data/prices/{day}.csv.gz"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    existing = requests.get(url, headers=headers, params={"ref": branch}, timeout=30)
    body = {
        "message": f"Price snapshot {day}",
        "content": base64.b64encode(blob).decode("ascii"),
        "branch": branch,
    }
    if existing.status_code == 200:
        body["sha"] = existing.json()["sha"]  # same day again: replace
    response = requests.put(url, headers=headers, json=body, timeout=120)
    response.raise_for_status()
