"""Read-side queries over stored snapshots: latest state per shop, history
over time, and cross-shop price comparison for the same matched product
(see product_matching.py).
"""

import math
from collections import defaultdict

from sqlalchemy import and_, func, literal, literal_column, or_
from sqlalchemy.orm import aliased

from db import ItemPrice, Product, Snapshot
from price_utils import fold_name


def _apply_text_filters(query, q=None, category=None):
    """Narrow a query by product-name substring (folded, so it's accent-
    and case-insensitive -- see price_utils.fold_name) and/or top-level
    category prefix. Shared by every ItemPrice query that filters this
    way, so the matching rule only has to change in one place."""
    if category:
        query = query.filter(ItemPrice.category.ilike(f"{category}%"))
    if q:
        query = query.filter(ItemPrice.name_fold.ilike(f"%{fold_name(q)}%"))
    return query


def get_latest_snapshot(session, shop_id):
    return (
        session.query(Snapshot)
        .filter_by(shop_id=shop_id)
        .order_by(Snapshot.snapshot_date.desc())
        .first()
    )


def get_recent_snapshot_pair(session, shop_id):
    """A shop's two most recent snapshots, newest first. Returns
    (newest, previous) -- previous is None if there's only one so far."""
    snaps = (
        session.query(Snapshot)
        .filter_by(shop_id=shop_id)
        .order_by(Snapshot.snapshot_date.desc())
        .limit(2)
        .all()
    )
    newest = snaps[0] if snaps else None
    previous = snaps[1] if len(snaps) > 1 else None
    return newest, previous


def _shop_price_drops_query(session, sid, newest, previous, q=None, category=None):
    """One shop's self-join between its two most recent snapshots,
    matched by e-food's stable item code (item_id is only unique within
    one shop's own catalog and can be reused for an unrelated product
    the next day; code is the one thing that reliably identifies "the
    same listing" across two days). drop_pct is computed in SQL so the
    combined multi-shop query (see get_price_drops) can ORDER BY it
    directly instead of sorting in Python."""
    Yesterday = aliased(ItemPrice)
    drop_pct = (Yesterday.price - ItemPrice.price) / Yesterday.price * 100
    query = (
        session.query(
            ItemPrice.name.label("name"),
            ItemPrice.category.label("category"),
            ItemPrice.code.label("code"),
            ItemPrice.price.label("price"),
            Yesterday.price.label("prev_price"),
            ItemPrice.size_info.label("size_info"),
            ItemPrice.metric_unit_description.label("metric_unit_description"),
            literal(sid).label("shop_id"),
            literal(newest.snapshot_date).label("snapshot_date"),
            drop_pct.label("drop_pct"),
        )
        .join(
            Yesterday,
            and_(Yesterday.code == ItemPrice.code, Yesterday.snapshot_id == previous.id),
        )
        .filter(
            ItemPrice.snapshot_id == newest.id,
            ItemPrice.code.isnot(None),
            ItemPrice.price > 0,
            Yesterday.price > 0,
            ItemPrice.price < Yesterday.price,
        )
    )
    return _apply_text_filters(query, q=q, category=category)


def _get_previous_snapshot(session, shop_id, before_date):
    """The snapshot immediately before before_date for one shop, or None."""
    return (
        session.query(Snapshot)
        .filter(Snapshot.shop_id == shop_id, Snapshot.snapshot_date < before_date)
        .order_by(Snapshot.snapshot_date.desc())
        .first()
    )


def get_price_drops(
    session, shop_labels_by_id, shop_id=None, q=None, category=None, page=1, per_page=50, latest=None
):
    """Items whose price fell between a shop's previous snapshot and its
    latest one. Each shop's self-join (see _shop_price_drops_query) is
    combined into one SQL UNION ALL so sorting, counting, and
    OFFSET/LIMIT pagination all happen in the database -- not by pulling
    every drop across every shop into Python first. Shops without two
    snapshots yet are skipped, not treated as an error.

    `latest` lets a caller that already ran
    get_latest_snapshots_for_all_shops pass that result straight through,
    so each shop only needs one more query here (for the *previous*
    snapshot) instead of two (get_recent_snapshot_pair re-fetching the
    already-known newest one too).

    Returns (rows, total_count), same convention as get_deals_page /
    search_products.
    """
    ids = [shop_id] if shop_id is not None else list(shop_labels_by_id)
    per_shop_queries = []
    for sid in ids:
        if latest is not None:
            newest = latest.get(sid)
            previous = _get_previous_snapshot(session, sid, newest.snapshot_date) if newest else None
        else:
            newest, previous = get_recent_snapshot_pair(session, sid)
        if newest is None or previous is None:
            continue
        per_shop_queries.append(
            _shop_price_drops_query(session, sid, newest, previous, q=q, category=category)
        )

    if not per_shop_queries:
        return [], 0

    combined = (
        per_shop_queries[0].union_all(*per_shop_queries[1:])
        if len(per_shop_queries) > 1
        else per_shop_queries[0]
    )
    total = combined.count()

    combined = combined.order_by(literal_column("drop_pct").desc())
    last_page = max(1, math.ceil(total / per_page))
    page = min(max(1, page), last_page)
    rows = combined.offset((page - 1) * per_page).limit(per_page).all()

    return [
        {
            "shop_id": row_shop_id,
            "shop_label": shop_labels_by_id[row_shop_id],
            "name": name,
            "category": cat,
            "code": code,
            "price": price,
            "prev_price": prev_price,
            "drop_pct": drop_pct,
            "size_info": size_info,
            "metric_unit_description": mud,
            "snapshot_date": snapshot_date,
        }
        for name, cat, code, price, prev_price, size_info, mud, row_shop_id, snapshot_date, drop_pct in rows
    ], total


def get_product_across_shops(session, product_id, shop_labels, exclude_shop_id=None):
    """Find the same product -- matched across chains by product_matching.py,
    not just an exact name string -- across all shops' latest snapshots.

    Returns list of dicts: {shop_id, shop_label, name, price, full_price,
    l30d_price, size_info, metric_unit_description, category} ordered by
    price ascending.

    Args:
        session: SQLAlchemy session
        product_id: Product.id to search for (None if this listing has no
            e-food item code and so was never matched to a Product)
        shop_labels: Dict mapping shop_id to shop_label
        exclude_shop_id: Shop to exclude from results (e.g., the current shop)
    """
    if product_id is None:
        return []

    latest_snapshots = {}
    for shop_id in shop_labels.keys():
        snap = get_latest_snapshot(session, shop_id)
        if snap:
            latest_snapshots[shop_id] = snap

    results = []
    for shop_id, snapshot in latest_snapshots.items():
        if exclude_shop_id and shop_id == exclude_shop_id:
            continue

        item = (
            session.query(ItemPrice)
            .filter(
                ItemPrice.snapshot_id == snapshot.id,
                ItemPrice.product_id == product_id,
            )
            .first()
        )

        if item and item.price is not None and item.price > 0:
            results.append({
                "shop_id": shop_id,
                "shop_label": shop_labels.get(shop_id, "Unknown"),
                "name": item.name,
                "price": item.price,
                "full_price": item.full_price,
                "l30d_price": item.l30d_price,
                "size_info": item.size_info,
                "metric_unit_description": item.metric_unit_description,
                "category": item.category,
            })

    # Sort by price ascending
    return sorted(results, key=lambda r: r["price"])


def get_flagged_items(session, snapshot_id):
    """Only the items worth showing on the dashboard: bugs and verified
    deals. A tiny fraction of a snapshot's rows, unlike querying every
    item in a snapshot unfiltered would be -- this is what keeps loading
    a dozen shops' worth of data from ballooning into tens of thousands
    of ORM objects for one page render."""
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


def get_flagged_items_filtered(session, snapshot_id, q=None, bug_type=None):
    """get_flagged_items with optional narrowing: q is a product-name
    substring, bug_type one of 'zero'/'placeholder'/'deal'. Filtering
    happens in SQL so a search never widens what gets materialized."""
    query = session.query(ItemPrice).filter(ItemPrice.snapshot_id == snapshot_id)
    if bug_type == "zero":
        query = query.filter(ItemPrice.is_zero_price_bug.is_(True))
    elif bug_type == "placeholder":
        query = query.filter(ItemPrice.is_placeholder_bug.is_(True))
    elif bug_type == "deal":
        query = query.filter(ItemPrice.is_verified_deal.is_(True))
    else:
        query = query.filter(
            or_(
                ItemPrice.is_zero_price_bug.is_(True),
                ItemPrice.is_placeholder_bug.is_(True),
                ItemPrice.is_verified_deal.is_(True),
            )
        )
    query = _apply_text_filters(query, q=q)
    return query.all()


def get_categories(session, snapshot_ids):
    """Sorted distinct top-level category groups across the given
    snapshots, for filter dropdowns. Categories are stored as
    "Group || Subgroup"; the top-level group keeps the list to a few
    dozen entries instead of hundreds."""
    if not snapshot_ids:
        return []
    rows = (
        session.query(ItemPrice.category)
        .filter(ItemPrice.snapshot_id.in_(snapshot_ids), ItemPrice.category.isnot(None))
        .distinct()
        .all()
    )
    groups = {r[0].split(" || ")[0].strip() for r in rows if r[0]}
    return sorted(groups)


def get_deals_page(
    session,
    shop_labels_by_id,
    shop_id=None,
    category=None,
    q=None,
    min_pct=None,
    sort="pct",
    page=1,
    per_page=50,
    latest=None,
):
    """One page of verified deals across the latest snapshots, filtered
    and ordered in SQL. Returns (rows, total_count). Column-only and
    LIMIT'd -- at most per_page rows are ever materialized.

    `latest` lets a caller that already ran
    get_latest_snapshots_for_all_shops pass that result straight through
    instead of this function querying it again from scratch.
    """
    ids = [shop_id] if shop_id is not None else list(shop_labels_by_id)
    if latest is None:
        latest = get_latest_snapshots_for_all_shops(session, ids)
    else:
        latest = {sid: snap for sid, snap in latest.items() if sid in ids}
    if not latest:
        return [], 0
    shop_by_snapshot = {snap.id: (sid, snap.snapshot_date) for sid, snap in latest.items()}

    query = session.query(
        ItemPrice.snapshot_id,
        ItemPrice.name,
        ItemPrice.category,
        ItemPrice.code,
        ItemPrice.price,
        ItemPrice.full_price,
        ItemPrice.deal_pct,
        ItemPrice.unit_price,
    ).filter(
        ItemPrice.snapshot_id.in_(list(shop_by_snapshot)),
        ItemPrice.is_verified_deal.is_(True),
    )
    query = _apply_text_filters(query, q=q, category=category)
    if min_pct:
        query = query.filter(ItemPrice.deal_pct >= min_pct)

    total = query.count()

    if sort == "price":
        query = query.order_by(ItemPrice.price.asc())
    elif sort == "unit_price":
        query = query.order_by(ItemPrice.unit_price.asc().nulls_last())
    elif sort == "name":
        query = query.order_by(ItemPrice.name.asc())
    else:
        query = query.order_by(ItemPrice.deal_pct.desc())

    # Clamp against the real page count BEFORE querying -- an
    # out-of-range page (e.g. the last real page was 2 but ?page=999 was
    # requested) must return that last real page's rows, not run the
    # query at a huge OFFSET that returns nothing while the caller's
    # "page N of pages" label still claims real data was shown.
    last_page = max(1, math.ceil(total / per_page))
    page = min(max(1, page), last_page)
    rows = query.offset((page - 1) * per_page).limit(per_page).all()

    results = []
    for snapshot_id, name, cat, code, price, full_price, pct, unit_price in rows:
        sid, snapshot_date = shop_by_snapshot[snapshot_id]
        results.append(
            {
                "shop_id": sid,
                "shop_label": shop_labels_by_id[sid],
                "name": name,
                "category": cat,
                "code": code,
                "price": price,
                "full_price": full_price,
                "pct": pct,
                "unit_price": unit_price,
                "snapshot_date": snapshot_date,
            }
        )
    return results, total


def get_trend(session, days=30):
    """Per-day totals across all shops (bug counts, deal counts) for the
    dashboard trend chart. One aggregate query over the small snapshots
    table."""
    rows = (
        session.query(
            Snapshot.snapshot_date,
            func.sum(Snapshot.zero_price_bug_count),
            func.sum(Snapshot.placeholder_bug_count),
            func.sum(Snapshot.verified_deal_count),
        )
        .group_by(Snapshot.snapshot_date)
        .order_by(Snapshot.snapshot_date.desc())
        .limit(days)
        .all()
    )
    return [
        {"date": d, "zero": z or 0, "placeholder": p or 0, "deals": v or 0}
        for d, z, p, v in reversed(rows)
    ]


def get_item_history_by_code(session, shop_id, code, limit=60):
    """Oldest-to-newest (date, price, full_price) history for one product
    in one shop, keyed by e-food's stable item code."""
    rows = (
        session.query(Snapshot.snapshot_date, ItemPrice.price, ItemPrice.full_price)
        .join(ItemPrice, ItemPrice.snapshot_id == Snapshot.id)
        .filter(Snapshot.shop_id == shop_id, ItemPrice.code == code)
        .order_by(Snapshot.snapshot_date.desc())
        .limit(limit)
        .all()
    )
    return list(reversed(rows))


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


def get_latest_snapshots_for_all_shops(session, shop_ids):
    latest = {}
    for shop_id in shop_ids:
        snap = get_latest_snapshot(session, shop_id)
        if snap is not None:
            latest[shop_id] = snap
    return latest


def search_products(
    session,
    shop_labels_by_id,
    q,
    shop_id=None,
    category=None,
    sort="price",
    page=1,
    per_page=50,
    latest=None,
):
    """Search the full catalog -- not just flagged bug/deal rows -- across
    the latest snapshot of every tracked shop. q is required: unlike a
    dashboard that always renders the same small flagged-row set, an
    open-ended scan of the whole catalog needs a real search term to
    bound it, so an empty q returns nothing rather than paging through
    the entire multi-shop inventory.

    `latest` lets a caller that already ran
    get_latest_snapshots_for_all_shops (e.g. to build a category list)
    pass that result straight through instead of this function querying
    it again from scratch.
    """
    if not q:
        return [], 0

    ids = [shop_id] if shop_id is not None else list(shop_labels_by_id)
    if latest is None:
        latest = get_latest_snapshots_for_all_shops(session, ids)
    else:
        latest = {sid: snap for sid, snap in latest.items() if sid in ids}
    if not latest:
        return [], 0
    shop_by_snapshot = {snap.id: (sid, snap.snapshot_date) for sid, snap in latest.items()}

    query = session.query(
        ItemPrice.snapshot_id,
        ItemPrice.name,
        ItemPrice.category,
        ItemPrice.code,
        ItemPrice.price,
        ItemPrice.full_price,
        ItemPrice.size_info,
        ItemPrice.metric_unit_description,
        ItemPrice.unit_price,
    ).filter(
        ItemPrice.snapshot_id.in_(list(shop_by_snapshot)),
        ItemPrice.price > 0,
    )
    query = _apply_text_filters(query, q=q, category=category)

    total = query.count()

    if sort == "unit_price":
        query = query.order_by(ItemPrice.unit_price.asc().nulls_last())
    elif sort == "name":
        query = query.order_by(ItemPrice.name.asc())
    else:
        query = query.order_by(ItemPrice.price.asc())

    # See the identical comment in get_deals_page -- clamp against the
    # real page count before querying, not after.
    last_page = max(1, math.ceil(total / per_page))
    page = min(max(1, page), last_page)
    rows = query.offset((page - 1) * per_page).limit(per_page).all()

    results = []
    for snapshot_id, name, cat, code, price, full_price, size_info, mud, unit_price in rows:
        sid, snapshot_date = shop_by_snapshot[snapshot_id]
        results.append(
            {
                "shop_id": sid,
                "shop_label": shop_labels_by_id[sid],
                "name": name,
                "category": cat,
                "code": code,
                "price": price,
                "full_price": full_price,
                "size_info": size_info,
                "metric_unit_description": mud,
                "unit_price": unit_price,
                "snapshot_date": snapshot_date,
            }
        )
    return results, total


def compare_across_shops(
    session,
    shop_labels_by_id,
    min_shops=2,
    min_spread_pct=5.0,
    q=None,
    category=None,
    latest=None,
):
    """For the latest snapshot of each shop, group items by the Product
    they've been matched to (product_matching.py -- fuzzy, cross-chain,
    not just an exact name string) and return those sold in >=min_shops
    shops, sorted by how much the price differs between the cheapest and
    priciest shop. q and category narrow the scan in SQL before anything
    is grouped. Items with no product_id (no e-food code, so never
    matched) are excluded -- the same effective behavior as before, when
    an unmatched item couldn't group with anything either.

    `latest` lets a caller that already ran
    get_latest_snapshots_for_all_shops (e.g. to build a category list)
    pass that result straight through instead of this function querying
    it again from scratch.
    """
    if latest is None:
        latest = get_latest_snapshots_for_all_shops(session, list(shop_labels_by_id))
    if len(latest) < min_shops:
        return []

    by_product = defaultdict(list)
    for shop_id, snap in latest.items():
        # Column-only query: this walks every product in every shop
        # (~85k rows across 12 shops), and only fields are needed.
        # Materializing full ORM objects for that is what this instance
        # cannot afford.
        query = (
            session.query(
                ItemPrice.product_id,
                ItemPrice.price,
                ItemPrice.category,
                ItemPrice.size_info,
                ItemPrice.metric_unit_description,
            )
            .filter(
                ItemPrice.snapshot_id == snap.id,
                ItemPrice.price > 0,
                ItemPrice.product_id.isnot(None),
            )
        )
        query = _apply_text_filters(query, q=q, category=category)
        label = shop_labels_by_id[shop_id]
        for product_id, price, category_value, size_info, metric_unit_description in query.all():
            by_product[product_id].append(
                {
                    "shop_id": shop_id,
                    "shop_label": label,
                    "price": price,
                    "category": category_value,
                    "size_info": size_info,
                    "metric_unit_description": metric_unit_description,
                }
            )

    results = []
    for product_id, rows in by_product.items():
        # A shop can list two listings under the same matched product
        # (e.g. deposit vs. no-deposit bottles) -- keep only its
        # cheapest so each shop appears at most once per group.
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
                    "product_id": product_id,
                    "category": deduped_rows[0]["category"],
                    "rows": sorted(deduped_rows, key=lambda r: r["price"]),
                    "low": lo,
                    "high": hi,
                    "spread_pct": spread_pct,
                }
            )

    if not results:
        return []

    # One batch lookup for the display name of just the groups that
    # survived filtering, rather than joining Product for all ~85k
    # scanned rows up front.
    canonical_names = dict(
        session.query(Product.id, Product.canonical_name)
        .filter(Product.id.in_([r["product_id"] for r in results]))
        .all()
    )
    for r in results:
        r["name"] = canonical_names.get(r["product_id"], "?")

    results.sort(key=lambda r: r["spread_pct"], reverse=True)
    return results


def iter_all_sales(session, shop_labels_by_id, shop_id=None, q=None, category=None):
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
        query = (
            session.query(
                ItemPrice.name,
                ItemPrice.category,
                ItemPrice.price,
                ItemPrice.full_price,
                ItemPrice.l30d_price,
                ItemPrice.is_verified_deal,
                ItemPrice.code,
            )
            .filter(
                ItemPrice.snapshot_id == snap.id,
                ItemPrice.full_price > ItemPrice.price,
                ItemPrice.price > 0,
            )
        )
        query = _apply_text_filters(query, q=q, category=category)
        discounted = query.all()
        rows = []
        for name, cat, price, full_price, l30d, is_deal, code in discounted:
            pct_off_full = (full_price - price) / full_price * 100
            pct_vs_l30d = None
            if l30d and l30d > 0:
                pct_vs_l30d = (l30d - price) / l30d * 100
            rows.append(
                {
                    "shop_id": sid,
                    "shop_label": label,
                    "name": name,
                    "category": cat,
                    "code": code,
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
