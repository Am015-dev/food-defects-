"""
Daily ingestion: fetch every tracked shop and store a snapshot in the
database. Meant to run once a day (see render.yaml's cron job). Safe to
re-run on the same UTC day -- it replaces that day's snapshot per shop
rather than duplicating it.
"""

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


def ingest_shop(session, shop_id, label, today):
    print(f"Fetching shop {shop_id} ({label})...")
    data = fetch_restaurant(shop_id)
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
    print(f"  stored snapshot {snapshot.id}: {len(rows)} items")


def main():
    init_db()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    session = SessionLocal()
    try:
        for shop in SHOPS:
            try:
                ingest_shop(session, shop["id"], shop["label"], today)
            except Exception as exc:  # noqa: BLE001 - one shop's failure shouldn't stop the rest
                print(f"  FAILED for shop {shop['id']} ({shop['label']}): {exc}")
                session.rollback()
    finally:
        session.close()


if __name__ == "__main__":
    main()
