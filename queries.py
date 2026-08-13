"""Read-side queries over stored snapshots: latest state per shop, history
over time, and cross-shop price comparison for the same product name.
"""

from collections import defaultdict

from sqlalchemy import or_

from db import ItemPrice, Snapshot


def get_latest_snapshot(session, shop_id):
    return (
        session.query(Snapshot)
        .filter_by(shop_id=shop_id)
        .order_by(Snapshot.snapshot_date.desc())
        .first()
    )


def get_snapshot_items(session, snapshot_id):
    """Every item in a snapshot. Expensive for a big catalog (thousands of
    rows) -- prefer get_flagged_items or a filtered query when only a
    subset is actually needed."""
    return session.query(ItemPrice).filter_by(snapshot_id=snapshot_id).all()


def get_flagged_items(session, snapshot_id):
    """Only the items worth showing on the dashboard: bugs and verified
    deals. A tiny fraction of a snapshot's rows, unlike get_snapshot_items
    -- this is what keeps loading 12 shops' worth of data from ballooning
    into tens of thousands of ORM objects for one page render."""
    return (
        session.query(ItemPrice)
        .filter(
            ItemPrice.snapshot_id == snapshot_id,
            or_(
                ItemPrice.is_zero_price_bug.is_(True),
                ItemPrice.is_placeholder_bug.is_(True),
                ItemPrice.is_verified_deal.is_(True),
            ),
        )
        .all()
    )


def get_history(session, shop_id, limit=60):
    """Oldest-to-newest list of per-day summary rows for one shop."""
    rows = (
        session.query(Snapshot)
        .filter_by(shop_id=shop_id)
        .order_by(Snapshot.snapshot_date.desc())
        .limit(limit)
        .all()
    )
    return list(reversed(rows))


def get_item_history(session, shop_id, item_id, limit=60):
    """Oldest-to-newest price history for one specific product in one shop."""
    rows = (
        session.query(ItemPrice, Snapshot.snapshot_date)
        .join(Snapshot, ItemPrice.snapshot_id == Snapshot.id)
        .filter(Snapshot.shop_id == shop_id, ItemPrice.item_id == item_id)
        .order_by(Snapshot.snapshot_date.desc())
        .limit(limit)
        .all()
    )
    return list(reversed(rows))


def get_latest_snapshots_for_all_shops(session, shop_ids):
    latest = {}
    for shop_id in shop_ids:
        snap = get_latest_snapshot(session, shop_id)
        if snap is not None:
            latest[shop_id] = snap
    return latest


def compare_across_shops(session, shop_labels_by_id, min_shops=2, min_spread_pct=5.0):
    """For the latest snapshot of each shop, group items by exact product
    name and return those sold in >=min_shops shops, sorted by how much
    the price differs between the cheapest and priciest shop."""
    latest = get_latest_snapshots_for_all_shops(session, list(shop_labels_by_id))
    if len(latest) < min_shops:
        return []

    by_name = defaultdict(list)
    for shop_id, snap in latest.items():
        # Column-only query: this walks every product in every shop
        # (~85k rows across 12 shops), and only three fields are needed.
        # Materializing full ORM objects for that is what this instance
        # cannot afford.
        rows = (
            session.query(ItemPrice.name, ItemPrice.price, ItemPrice.category)
            .filter(ItemPrice.snapshot_id == snap.id, ItemPrice.price > 0)
            .all()
        )
        label = shop_labels_by_id[shop_id]
        for name, price, category in rows:
            by_name[name].append(
                {
                    "shop_id": shop_id,
                    "shop_label": label,
                    "price": price,
                    "category": category,
                }
            )

    results = []
    for name, rows in by_name.items():
        # A shop can list the same product name twice (e.g. deposit vs.
        # no-deposit bottles) -- keep only its cheapest listing so each
        # shop appears at most once per comparison group.
        cheapest_per_shop = {}
        for r in rows:
            existing = cheapest_per_shop.get(r["shop_id"])
            if existing is None or r["price"] < existing["price"]:
                cheapest_per_shop[r["shop_id"]] = r
        deduped_rows = list(cheapest_per_shop.values())

        if len(deduped_rows) < min_shops:
            continue
        prices = [r["price"] for r in deduped_rows]
        lo, hi = min(prices), max(prices)
        if lo <= 0:
            continue
        spread_pct = (hi - lo) / lo * 100
        if spread_pct >= min_spread_pct:
            results.append(
                {
                    "name": name,
                    "category": deduped_rows[0]["category"],
                    "rows": sorted(deduped_rows, key=lambda r: r["price"]),
                    "low": lo,
                    "high": hi,
                    "spread_pct": spread_pct,
                }
            )

    results.sort(key=lambda r: r["spread_pct"], reverse=True)
    return results


def iter_all_sales(session, shop_labels_by_id, shop_id=None):
    """Yield every item currently showing ANY discount badge (full_price >
    price), across all shops or one specific shop -- unfiltered by size or
    verification against the 30-day low. Meant for a full CSV export/audit.

    A generator over per-shop, column-only queries rather than a big list:
    the export covers ~10k rows, and neither the ORM objects nor the
    assembled CSV text should ever exist in memory all at once on a
    512MB instance. Rows come out grouped by shop, sorted by discount
    within each shop (a global sort would mean buffering everything,
    which is exactly what this avoids).
    """
    ids = [shop_id] if shop_id is not None else list(shop_labels_by_id)
    latest = get_latest_snapshots_for_all_shops(session, ids)
    for sid, snap in latest.items():
        label = shop_labels_by_id[sid]
        discounted = (
            session.query(
                ItemPrice.name,
                ItemPrice.category,
                ItemPrice.price,
                ItemPrice.full_price,
                ItemPrice.l30d_price,
                ItemPrice.is_verified_deal,
            )
            .filter(
                ItemPrice.snapshot_id == snap.id,
                ItemPrice.full_price > ItemPrice.price,
                ItemPrice.price > 0,
            )
            .all()
        )
        rows = []
        for name, category, price, full_price, l30d, is_deal in discounted:
            pct_off_full = (full_price - price) / full_price * 100
            pct_vs_l30d = None
            if l30d and l30d > 0:
                pct_vs_l30d = (l30d - price) / l30d * 100
            rows.append(
                {
                    "shop_label": label,
                    "name": name,
                    "category": category,
                    "price": price,
                    "full_price": full_price,
                    "l30d_price": l30d,
                    "pct_off_full": pct_off_full,
                    "pct_vs_l30d": pct_vs_l30d,
                    "is_verified_deal": is_deal,
                    "snapshot_date": snap.snapshot_date,
                }
            )
        rows.sort(key=lambda r: r["pct_off_full"], reverse=True)
        yield from rows
