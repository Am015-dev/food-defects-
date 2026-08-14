"""
Daily ingestion: fetch every tracked shop and store a snapshot in the
database. Safe to call more than once on the same UTC day -- it replaces
that day's snapshot per shop rather than duplicating it.

Run as a CLI (`python ingest.py`) directly against the production
database -- see .github/workflows/daily-ingest.yml, which is what runs
this on a schedule. Render's free tier doesn't offer a free cron job
service type, so scheduling lives in GitHub Actions instead, on a
runner with its own RAM rather than the memory-constrained web service.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from db import ItemPrice, ProductListing, SessionLocal, Shop, Snapshot, init_db
from efood_client import fetch_restaurant
from price_analysis import (
    find_placeholder_reference_price_bugs,
    find_verified_deep_discounts,
    find_zero_price_bugs,
    l30d_price,
    load_items,
)
from price_utils import derive_unit_price, fold_name
from product_matching import match_or_create_product
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

    # One query for this shop's whole (code -> product_id) cache, instead
    # of a per-item lookup -- see product_matching.py. Only genuinely new
    # codes (not in this map) pay the matching cost below.
    known_product_id_by_code = dict(
        session.query(ProductListing.code, ProductListing.product_id).filter(
            ProductListing.shop_id == shop_id
        )
    )
    # Candidate products considered during this call, keyed by top-level
    # category -- lazily filled and grown in place by match_or_create_product
    # so a product created earlier in this same run can still be matched
    # against by a later item, without a fresh DB round-trip each time.
    block_cache = {}

    rows = []
    new_listings = []
    for it in items:
        price = it.get("price")
        size_info = it.get("size_info")
        metric_unit_description = it.get("metric_unit_description")
        # unit_kind (the second return value) isn't stored -- it was
        # write-only in the schema; unit_price alone is what /deals and
        # /search sort by, and display already derives its own unit
        # label independently (see get_price_comparison_info).
        unit_price, _unit_kind = derive_unit_price(price, size_info, metric_unit_description)

        code = it.get("code")
        product_id = known_product_id_by_code.get(code) if code else None
        if code and product_id is None:
            product_id, confidence = match_or_create_product(
                session, it["name"], it["_category"], block_cache
            )
            new_listings.append(
                ProductListing(
                    shop_id=shop_id,
                    code=code,
                    product_id=product_id,
                    match_confidence=confidence,
                    first_seen_name=it["name"],
                    created_at=datetime.now(timezone.utc),
                )
            )
            known_product_id_by_code[code] = product_id

        rows.append(
            ItemPrice(
                snapshot_id=snapshot.id,
                item_id=it["id"],
                code=code,
                name=it["name"],
                name_fold=fold_name(it["name"]),
                category=it["_category"],
                price=price,
                full_price=it.get("full_price"),
                l30d_price=l30d_price(it),
                size_info=size_info,
                metric_unit_description=metric_unit_description,
                unit_price=unit_price,
                product_id=product_id,
                is_zero_price_bug=it["id"] in zero_bug_ids,
                is_placeholder_bug=it["id"] in placeholder_bug_ids,
                is_verified_deal=it["id"] in deal_pct_by_id,
                deal_pct=deal_pct_by_id.get(it["id"]),
            )
        )
    session.bulk_save_objects(rows)
    if new_listings:
        session.bulk_save_objects(new_listings)
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

    try:
        from retention import prune_old_item_prices  # lazy: keeps CLI import light

        pruned = prune_old_item_prices()
        if pruned:
            print(f"retention: deleted {pruned} old item_prices row(s)")
    except Exception as exc:  # noqa: BLE001 - a pruning failure shouldn't fail ingestion
        print(f"retention: pruning failed, will retry next run: {exc}")

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
