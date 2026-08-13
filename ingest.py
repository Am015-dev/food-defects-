"""
Daily ingestion: fetch every tracked shop and store a snapshot in the
database. Safe to call more than once on the same UTC day -- it replaces
that day's snapshot per shop rather than duplicating it.

Can be run as a CLI (`python ingest.py`) or triggered over HTTP via the
web app's /ingest endpoint (see webapp.py), which is what the daily
GitHub Actions workflow calls -- Render's free tier doesn't offer a free
cron job service type, so scheduling lives in GitHub Actions instead.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from db import ItemPrice, SessionLocal, Shop, Snapshot, init_db
from efood_client import fetch_restaurant
from price_analysis import (
    find_placeholder_reference_price_bugs,
    find_verified_deep_discounts,
    find_zero_price_bugs,
    l30d_price,
    load_items,
)
from shops import SHOPS

MAX_CONCURRENCY = 2  # how many shops to fetch+store at once


def store_snapshot(session, shop_id, label, today, data):
    """Persist one already-fetched shop payload as today's snapshot. Returns the item count."""
    items = load_items(data)

    zero_bug_ids = {it["id"] for it in find_zero_price_bugs(items)}
    placeholder_bug_ids = {it["id"] for it in find_placeholder_reference_price_bugs(items)}
    deal_pct_by_id = {d["item"]["id"]: d["pct"] for d in find_verified_deep_discounts(items)}

    shop = session.get(Shop, shop_id)
    if shop is None:
        shop = Shop(id=shop_id, label=label)
        session.add(shop)
    else:
        shop.label = label

    existing = (
        session.query(Snapshot)
        .filter_by(shop_id=shop_id, snapshot_date=today)
        .one_or_none()
    )
    if existing is not None:
        session.delete(existing)  # cascades to its item_prices
        session.flush()

    snapshot = Snapshot(
        shop_id=shop_id,
        snapshot_date=today,
        fetched_at=datetime.now(timezone.utc),
        store_title=data["information"]["title"],
        store_address=data["information"]["address"]["description"],
        is_open=data["information"]["is_open"],
        total_items=len(items),
        total_categories=len(data["menu"]["categories"]),
        zero_price_bug_count=len(zero_bug_ids),
        placeholder_bug_count=len(placeholder_bug_ids),
        verified_deal_count=len(deal_pct_by_id),
    )
    session.add(snapshot)
    session.flush()  # assigns snapshot.id

    rows = [
        ItemPrice(
            snapshot_id=snapshot.id,
            item_id=it["id"],
            name=it["name"],
            category=it["_category"],
            price=it.get("price"),
            full_price=it.get("full_price"),
            l30d_price=l30d_price(it),
            size_info=it.get("size_info"),
            is_zero_price_bug=it["id"] in zero_bug_ids,
            is_placeholder_bug=it["id"] in placeholder_bug_ids,
            is_verified_deal=it["id"] in deal_pct_by_id,
            deal_pct=deal_pct_by_id.get(it["id"]),
        )
        for it in items
    ]
    session.bulk_save_objects(rows)
    session.commit()
    return len(rows)


def fetch_and_store_shop(shop_id, label, today):
    """Fetch one shop and store it immediately, in the same worker, so its
    raw payload (a shop's full catalog can be ~15MB on the wire, several
    times that as parsed Python dicts) doesn't have to stick around in
    memory alongside every other shop's. Each worker gets its own DB
    session -- sessions aren't thread-safe."""
    try:
        data = fetch_restaurant(shop_id)
    except Exception as exc:  # noqa: BLE001
        return {"shop_id": shop_id, "label": label, "ok": False, "error": str(exc)}

    session = SessionLocal()
    try:
        items = store_snapshot(session, shop_id, label, today, data)
        return {"shop_id": shop_id, "label": label, "ok": True, "items": items}
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        return {"shop_id": shop_id, "label": label, "ok": False, "error": str(exc)}
    finally:
        session.close()


def run_ingestion():
    """Fetch and store every tracked shop, with bounded concurrency so
    only a handful of shops' raw catalogs are ever in memory at once.
    Fetching all of them upfront -- even before writing any to the
    database -- was pushing the app well past Render's 512MB limit."""
    init_db()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    results = [None] * len(SHOPS)
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENCY) as pool:
        future_to_index = {
            pool.submit(fetch_and_store_shop, shop["id"], shop["label"], today): i
            for i, shop in enumerate(SHOPS)
        }
        for future in as_completed(future_to_index):
            results[future_to_index[future]] = future.result()

    return {"date": today, "results": results}


def main():
    summary = run_ingestion()
    print(f"Ingestion for {summary['date']}:")
    for r in summary["results"]:
        if r["ok"]:
            print(f"  {r['label']}: stored {r['items']} items")
        else:
            print(f"  FAILED for {r['label']}: {r['error']}")


if __name__ == "__main__":
    main()
