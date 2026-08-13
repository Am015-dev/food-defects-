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


def run_ingestion():
    """Fetch every tracked shop (concurrently) and store today's snapshot
    for each. Returns a JSON-serializable summary."""
    init_db()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    fetched = {}
    with ThreadPoolExecutor(max_workers=len(SHOPS)) as pool:
        future_to_shop = {pool.submit(fetch_restaurant, shop["id"]): shop for shop in SHOPS}
        for future in as_completed(future_to_shop):
            shop = future_to_shop[future]
            try:
                fetched[shop["id"]] = future.result()
            except Exception as exc:  # noqa: BLE001 - report per-shop, don't abort the batch
                fetched[shop["id"]] = exc

    session = SessionLocal()
    results = []
    try:
        for shop in SHOPS:
            outcome = fetched.get(shop["id"])
            entry = {"shop_id": shop["id"], "label": shop["label"]}
            if isinstance(outcome, Exception):
                entry.update(ok=False, error=str(outcome))
            else:
                try:
                    entry.update(
                        ok=True,
                        items=store_snapshot(session, shop["id"], shop["label"], today, outcome),
                    )
                except Exception as exc:  # noqa: BLE001
                    session.rollback()
                    entry.update(ok=False, error=str(exc))
            results.append(entry)
    finally:
        session.close()

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
