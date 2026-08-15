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

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from sqlalchemy import func

from db import (
    CategoryDailySummary,
    ItemPrice,
    PriceExtreme,
    ProductListing,
    SessionLocal,
    Shop,
    Snapshot,
    init_db,
)
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


def update_price_extremes_rollup(session=None):
    """Recompute price_extremes for every shop's currently-listed items,
    from the retained item_prices history (see retention.py -- a rolling
    ~90-day window). Delete-then-bulk-insert per shop, the same pattern
    store_snapshot uses for a day's rows, so an item that's since been
    delisted doesn't leave a stale row behind.

    Meant to run once at the end of a full ingest, after every shop's
    snapshot for today is written -- not per shop during fetch, since it
    needs each shop's *latest* snapshot to know today's name/category/
    price. The MIN/MAX aggregation itself is pushed down to the
    database (one GROUP BY per shop) rather than pulled into Python row
    by row -- this runs on the GitHub Actions runner precisely so that
    kind of full-history aggregation doesn't have to happen on the
    memory-constrained web service.
    """
    owns_session = session is None
    session = session or SessionLocal()
    try:
        rows_written = 0
        for (shop_id,) in session.query(Shop.id):
            latest = (
                session.query(Snapshot)
                .filter_by(shop_id=shop_id)
                .order_by(Snapshot.snapshot_date.desc())
                .first()
            )
            if latest is None:
                continue

            current = {
                code: (name, category, price)
                for code, name, category, price in session.query(
                    ItemPrice.code, ItemPrice.name, ItemPrice.category, ItemPrice.price
                ).filter(
                    ItemPrice.snapshot_id == latest.id,
                    ItemPrice.code.isnot(None),
                    ItemPrice.price > 0,
                )
            }

            session.query(PriceExtreme).filter_by(shop_id=shop_id).delete(synchronize_session=False)
            if not current:
                continue

            extremes = {
                code: (lo, hi)
                for code, lo, hi in session.query(
                    ItemPrice.code, func.min(ItemPrice.price), func.max(ItemPrice.price)
                )
                .join(Snapshot, Snapshot.id == ItemPrice.snapshot_id)
                .filter(
                    Snapshot.shop_id == shop_id,
                    ItemPrice.price > 0,
                    ItemPrice.code.isnot(None),
                )
                .group_by(ItemPrice.code)
            }

            now = datetime.now(timezone.utc)
            new_rows = []
            for code, (name, category, price) in current.items():
                lo, hi = extremes.get(code, (price, price))
                new_rows.append(
                    PriceExtreme(
                        shop_id=shop_id,
                        code=code,
                        name=name,
                        category=category,
                        current_price=price,
                        min_price=lo,
                        max_price=hi,
                        swing_pct=(hi - lo) / hi * 100 if hi else 0.0,
                        updated_at=now,
                    )
                )
            session.bulk_save_objects(new_rows)
            rows_written += len(new_rows)
        session.commit()
        return rows_written
    finally:
        if owns_session:
            session.close()


def update_category_daily_summary(session=None, today=None):
    """Roll up today's ItemPrice rows (across every shop combined) into
    one row per top-level category group: average price, item count,
    bug count. Unlike price_extremes, these rows are never pruned --
    same append-forever treatment as Snapshot -- so a category's price
    trend can span longer than item_prices' ~90-day retention window.
    Idempotent: reruns for the same day replace that day's rows rather
    than duplicating them, the same delete-then-insert pattern
    store_snapshot uses for a shop's day.
    """
    owns_session = session is None
    session = session or SessionLocal()
    try:
        if today is None:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        snapshot_ids = [
            sid for (sid,) in session.query(Snapshot.id).filter(Snapshot.snapshot_date == today)
        ]
        if not snapshot_ids:
            return 0

        buckets = defaultdict(lambda: {"price_sum": 0.0, "price_count": 0, "items": 0, "bugs": 0})
        query = session.query(
            ItemPrice.category,
            ItemPrice.price,
            ItemPrice.is_zero_price_bug,
            ItemPrice.is_placeholder_bug,
        ).filter(ItemPrice.snapshot_id.in_(snapshot_ids), ItemPrice.category.isnot(None))
        for category, price, is_zero, is_placeholder in query:
            top = category.split(" || ")[0].strip()
            if not top:
                continue
            bucket = buckets[top]
            bucket["items"] += 1
            if price and price > 0:
                bucket["price_sum"] += price
                bucket["price_count"] += 1
            if is_zero or is_placeholder:
                bucket["bugs"] += 1

        session.query(CategoryDailySummary).filter_by(snapshot_date=today).delete(synchronize_session=False)
        new_rows = [
            CategoryDailySummary(
                category=top,
                snapshot_date=today,
                avg_price=(b["price_sum"] / b["price_count"]) if b["price_count"] else None,
                item_count=b["items"],
                bug_count=b["bugs"],
            )
            for top, b in buckets.items()
        ]
        session.bulk_save_objects(new_rows)
        session.commit()
        return len(new_rows)
    finally:
        if owns_session:
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

    try:
        written = update_price_extremes_rollup()
        print(f"price extremes: refreshed {written} row(s)")
    except Exception as exc:  # noqa: BLE001 - a rollup failure shouldn't fail ingestion
        print(f"price extremes: rollup failed, will retry next run: {exc}")

    try:
        cat_written = update_category_daily_summary(today=today)
        print(f"category summary: wrote {cat_written} row(s) for {today}")
    except Exception as exc:  # noqa: BLE001 - a summary failure shouldn't fail ingestion
        print(f"category summary: failed, will retry next run: {exc}")

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
