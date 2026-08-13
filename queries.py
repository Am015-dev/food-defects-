"""Read-side queries over stored snapshots: latest state per shop, history
over time, and cross-shop price comparison for the same product name.
"""

from collections import defaultdict

from sqlalchemy import func

from db import ItemPrice, Snapshot


def get_latest_snapshot(session, shop_id):
    return (
        session.query(Snapshot)
        .filter_by(shop_id=shop_id)
        .order_by(Snapshot.snapshot_date.desc())
        .first()
    )


def get_snapshot_items(session, snapshot_id):
    return session.query(ItemPrice).filter_by(snapshot_id=snapshot_id).all()


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
        items = get_snapshot_items(session, snap.id)
        for it in items:
            if it.price and it.price > 0:
                by_name[it.name].append(
                    {
                        "shop_id": shop_id,
                        "shop_label": shop_labels_by_id[shop_id],
                        "price": it.price,
                        "category": it.category,
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


def get_all_bugs(session, shop_labels_by_id):
    """Zero-price and placeholder-reference-price bugs across every shop's
    latest snapshot, each tagged with which shop it's in."""
    latest = get_latest_snapshots_for_all_shops(session, list(shop_labels_by_id))
    zero_price = []
    placeholder = []
    for shop_id, snap in latest.items():
        label = shop_labels_by_id[shop_id]
        for it in get_snapshot_items(session, snap.id):
            if it.is_zero_price_bug:
                zero_price.append({"shop_label": label, "name": it.name, "category": it.category})
            if it.is_placeholder_bug:
                placeholder.append(
                    {"shop_label": label, "name": it.name, "category": it.category, "price": it.price}
                )
    return {"zero_price": zero_price, "placeholder": placeholder}


def get_top_bargains(session, shop_labels_by_id, limit=30):
    """The best verified deals (fixed-size, >=20% below the real 30-day
    low) across every shop's latest snapshot, ranked together."""
    latest = get_latest_snapshots_for_all_shops(session, list(shop_labels_by_id))
    deals = []
    for shop_id, snap in latest.items():
        label = shop_labels_by_id[shop_id]
        for it in get_snapshot_items(session, snap.id):
            if it.is_verified_deal:
                deals.append(
                    {
                        "shop_label": label,
                        "name": it.name,
                        "category": it.category,
                        "price": it.price,
                        "full_price": it.full_price,
                        "pct": it.deal_pct,
                    }
                )
    deals.sort(key=lambda d: d["pct"], reverse=True)
    return deals[:limit]


def get_all_sales(session, shop_labels_by_id, shop_id=None):
    """Every item currently showing ANY discount badge (full_price >
    price), across all shops or one specific shop -- unfiltered by size or
    verification against the 30-day low, unlike get_top_bargains. Meant
    for a full CSV export/audit, not for picking out the best deals."""
    ids = [shop_id] if shop_id is not None else list(shop_labels_by_id)
    latest = get_latest_snapshots_for_all_shops(session, ids)
    rows = []
    for sid, snap in latest.items():
        label = shop_labels_by_id[sid]
        for it in get_snapshot_items(session, snap.id):
            if not (it.full_price and it.price and it.full_price > it.price > 0):
                continue
            pct_off_full = (it.full_price - it.price) / it.full_price * 100
            pct_vs_l30d = None
            if it.l30d_price and it.l30d_price > 0:
                pct_vs_l30d = (it.l30d_price - it.price) / it.l30d_price * 100
            rows.append(
                {
                    "shop_label": label,
                    "name": it.name,
                    "category": it.category,
                    "price": it.price,
                    "full_price": it.full_price,
                    "l30d_price": it.l30d_price,
                    "pct_off_full": pct_off_full,
                    "pct_vs_l30d": pct_vs_l30d,
                    "is_verified_deal": it.is_verified_deal,
                    "snapshot_date": snap.snapshot_date,
                }
            )
    rows.sort(key=lambda r: r["pct_off_full"], reverse=True)
    return rows
