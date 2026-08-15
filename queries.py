"""Read-side queries over stored snapshots: latest state per shop, history
over time, and cross-shop price comparison for the same matched product
(see product_matching.py).
"""

import math
from collections import defaultdict

from sqlalchemy import and_, func, literal, literal_column, or_
from sqlalchemy.orm import aliased

from db import CategoryDailySummary, ItemPrice, PriceExtreme, Product, ProductListing, Snapshot
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


def _shop_price_drops_query(session, sid, newest, previous, q=None, category=None, min_drop_pct=None):
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
    # Filtered on the raw expression here, before the multi-shop UNION
    # ALL in get_price_drops -- drop_pct isn't a stored column like
    # get_deals_page's deal_pct, it only exists as this computed
    # self-join expression, so it has to be filtered per-shop.
    if min_drop_pct:
        query = query.filter(drop_pct >= min_drop_pct)
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
    session,
    shop_labels_by_id,
    shop_id=None,
    q=None,
    category=None,
    min_drop_pct=None,
    sort="drop_pct",
    page=1,
    per_page=50,
    latest=None,
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
            _shop_price_drops_query(
                session, sid, newest, previous, q=q, category=category, min_drop_pct=min_drop_pct
            )
        )

    if not per_shop_queries:
        return [], 0

    combined = (
        per_shop_queries[0].union_all(*per_shop_queries[1:])
        if len(per_shop_queries) > 1
        else per_shop_queries[0]
    )
    total = combined.count()

    if sort == "price":
        combined = combined.order_by(literal_column("price").asc())
    elif sort == "name":
        combined = combined.order_by(literal_column("name").asc())
    else:
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


def get_product_across_shops(session, product_id, shop_labels, exclude_shop_id=None, latest=None):
    """Find the same product -- matched across chains by product_matching.py,
    not just an exact name string -- across all shops' latest snapshots.

    Returns list of dicts: {shop_id, shop_label, name, price, full_price,
    l30d_price, size_info, metric_unit_description, category, code,
    unit_price} ordered by price ascending. unit_price is the DB-stored,
    already-normalized-to-a-common-scale value (see
    price_utils.derive_unit_price) -- safe to compare directly against
    another row's unit_price for the same product_id, unlike the raw
    size_info/metric_unit_description fields above.

    Args:
        session: SQLAlchemy session
        product_id: Product.id to search for (None if this listing has no
            e-food item code and so was never matched to a Product)
        shop_labels: Dict mapping shop_id to shop_label
        exclude_shop_id: Shop to exclude from results (e.g., the current shop)
        latest: optional pre-fetched {shop_id: Snapshot} from
            get_latest_snapshots_for_all_shops, so a caller that already
            has it (e.g. verify_item) skips re-fetching it here.
    """
    if product_id is None:
        return []

    if latest is None:
        latest = get_latest_snapshots_for_all_shops(session, list(shop_labels))

    snapshot_by_id = {
        snap.id: shop_id for shop_id, snap in latest.items() if shop_id != exclude_shop_id
    }
    if not snapshot_by_id:
        return []

    # One query across every shop's latest snapshot instead of one
    # per-shop lookup -- this used to be up to 13 separate ItemPrice
    # queries (one per tracked shop) on /item, the tool's core
    # evidentiary page.
    rows = (
        session.query(ItemPrice)
        .filter(
            ItemPrice.snapshot_id.in_(list(snapshot_by_id)),
            ItemPrice.product_id == product_id,
            ItemPrice.price > 0,
        )
        .all()
    )

    # A shop can list two listings under the same matched product (e.g.
    # deposit vs. no-deposit bottles, same convention as
    # compare_across_shops) -- the old per-shop .first() implicitly
    # picked one arbitrarily; keep the cheapest explicitly instead.
    cheapest_per_shop = {}
    for item in rows:
        shop_id = snapshot_by_id[item.snapshot_id]
        existing = cheapest_per_shop.get(shop_id)
        if existing is None or item.price < existing.price:
            cheapest_per_shop[shop_id] = item

    results = [
        {
            "shop_id": shop_id,
            "shop_label": shop_labels.get(shop_id, "Unknown"),
            "name": item.name,
            "price": item.price,
            "full_price": item.full_price,
            "l30d_price": item.l30d_price,
            "size_info": item.size_info,
            "metric_unit_description": item.metric_unit_description,
            "category": item.category,
            "code": item.code,
            "unit_price": item.unit_price,
        }
        for shop_id, item in cheapest_per_shop.items()
    ]
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


def _flag_streaks(session, shop_id, codes, flag_column, max_days):
    """For each code in `codes` (today's flagged items for one shop), how
    many consecutive most-recent daily snapshots also had flag_column
    set -- so a chronic offender (the same SKU broken, or the same deal
    running, for weeks) can be told apart from a one-off, which look
    identical in a plain "flagged today" list. Shared by get_bug_streaks
    (is_zero_price_bug / is_placeholder_bug) and get_deal_streaks
    (is_verified_deal) -- same lookback shape, different flag.

    One query for the WHOLE shop, not one per item -- looping a
    per-item lookback query here would repeat the exact N+1 shape
    already fixed for get_product_across_shops (see MISTAKES.md-style
    lesson: batch by shop, not by row). max_days bounds the lookback so
    a years-old chronic streak doesn't walk the shop's whole history;
    the badge only needs to distinguish "today only" from "ongoing",
    not an exact lifetime count.

    Returns {code: streak_days}, streak_days >= 1 for every code passed
    in (today itself always counts, since these are today's flagged
    codes by construction).
    """
    if not codes:
        return {}

    recent_snapshot_ids = [
        sid
        for (sid,) in (
            session.query(Snapshot.id)
            .filter(Snapshot.shop_id == shop_id)
            .order_by(Snapshot.snapshot_date.desc())
            .limit(max_days)
            .all()
        )
    ]
    if not recent_snapshot_ids:
        return {}

    rows = session.query(ItemPrice.snapshot_id, ItemPrice.code).filter(
        ItemPrice.snapshot_id.in_(recent_snapshot_ids),
        ItemPrice.code.in_(codes),
        flag_column.is_(True),
    )
    flagged_by_snapshot = defaultdict(set)
    for snapshot_id, code in rows:
        flagged_by_snapshot[snapshot_id].add(code)

    streaks = {}
    for code in codes:
        streak = 0
        for snapshot_id in recent_snapshot_ids:
            if code in flagged_by_snapshot[snapshot_id]:
                streak += 1
            else:
                break
        streaks[code] = streak
    return streaks


def get_bug_streaks(session, shop_id, codes, bug_type, max_days=30):
    """Consecutive-day streak for a bug flag ('zero' or 'placeholder') --
    see _flag_streaks for the shared mechanics."""
    flag_column = ItemPrice.is_zero_price_bug if bug_type == "zero" else ItemPrice.is_placeholder_bug
    return _flag_streaks(session, shop_id, codes, flag_column, max_days)


def get_deal_streaks(session, shop_id, codes, max_days=30):
    """Consecutive-day streak for a verified deal -- the same "chronic
    vs. one-off" distinction get_bug_streaks gives bugs, applied to
    deals: a discount that's been genuinely verified for N days running
    is worth trusting more than one that just appeared today."""
    return _flag_streaks(session, shop_id, codes, ItemPrice.is_verified_deal, max_days)


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


# Every verified deal already clears find_verified_deep_discounts' 20%
# floor (price_analysis.py) before it's stored as is_verified_deal, so
# this only distinguishes an already-verified deal from an exceptional
# one -- it's presentation-only, not a re-verification of anything.
GREAT_DEAL_THRESHOLD_PCT = 35


def deal_tier(pct):
    """'great' or 'good' badge tier for a verified deal's discount size,
    or None if pct is missing (shouldn't happen for a real verified
    deal, but callers may pass a possibly-null column value)."""
    if pct is None:
        return None
    return "great" if pct >= GREAT_DEAL_THRESHOLD_PCT else "good"


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
        ItemPrice.size_info,
        ItemPrice.metric_unit_description,
        ItemPrice.product_id,
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
    for snapshot_id, name, cat, code, price, full_price, pct, unit_price, size_info, mud, product_id in rows:
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
                "tier": deal_tier(pct),
                "unit_price": unit_price,
                "size_info": size_info,
                "metric_unit_description": mud,
                "product_id": product_id,
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


def get_shop_bug_rates(latest, shop_labels_by_id, min_items=1):
    """Shops ranked by bug rate -- (zero-price + placeholder bugs) as a
    fraction of that shop's own catalog size -- rather than raw count,
    since a 13-item shop's 2 bugs isn't comparable to a 9,000-item
    shop's 40. Pure computation over Snapshot rows the caller already
    fetched (see get_latest_snapshots_for_all_shops): those bug/item
    counts are written once per shop per day at ingest time
    (ingest.py: store_snapshot), so this needs no query at all.

    min_items excludes shops whose catalog is too small for a rate to
    be meaningful (e.g. a brand-new shop with a handful of items).
    """
    rows = []
    for shop_id, snap in latest.items():
        total = snap.total_items or 0
        if total < min_items:
            continue
        bug_count = (snap.zero_price_bug_count or 0) + (snap.placeholder_bug_count or 0)
        rows.append(
            {
                "shop_id": shop_id,
                "shop_label": shop_labels_by_id.get(shop_id, "Unknown"),
                "bug_count": bug_count,
                "total_items": total,
                "bug_rate": bug_count / total,
            }
        )
    rows.sort(key=lambda r: r["bug_rate"], reverse=True)
    return rows


def get_low_confidence_matches(session, max_confidence=0.93, limit=50):
    """ProductListing rows in the residual false-positive band documented
    in MISTAKES.md (2026-08-13 entry, "fuzzy product matching used the
    wrong rapidfuzz scorer") -- match_confidence < 1.0 (an actual fuzzy
    match against an existing Product, not a freshly created one, which
    always stores 1.0) and below max_confidence. A bad match here
    silently corrupts a /compare row across chains, so this is a manual
    review queue for the operator, worst (lowest-confidence) first.

    max_confidence defaults to 0.93: MATCH_THRESHOLD (product_matching.py)
    is 90, and the documented residual false-positive band after the
    token_sort_ratio fix sits at roughly 90.2-92.7, i.e. matches that
    cleared the threshold but are still worth a human glance.
    """
    rows = (
        session.query(
            ProductListing.shop_id,
            ProductListing.code,
            ProductListing.first_seen_name,
            ProductListing.match_confidence,
            Product.canonical_name,
            Product.category,
        )
        .join(Product, Product.id == ProductListing.product_id)
        .filter(ProductListing.match_confidence < max_confidence)
        .order_by(ProductListing.match_confidence.asc())
        .limit(limit)
        .all()
    )
    return [
        {
            "shop_id": shop_id,
            "code": code,
            "listing_name": listing_name,
            "confidence": confidence,
            "product_name": canonical_name,
            "category": category,
        }
        for shop_id, code, listing_name, confidence, canonical_name, category in rows
    ]


def _catalog_scan_page(session, shop_labels_by_id, ids, q, category, sort, page, per_page, latest):
    """Shared body of search_products and get_category_page: one page of
    the full catalog (not just flagged bug/deal rows) across the latest
    snapshot of the given shops, filtered/sorted/paged in SQL. Column-only
    and LIMIT'd -- at most per_page rows are ever materialized. Callers
    are responsible for bounding the scan (a required q or category)
    before calling this -- it applies no guard of its own.

    `latest` lets a caller that already ran
    get_latest_snapshots_for_all_shops pass that result straight through
    instead of this function querying it again from scratch.
    """
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
    """Search the full catalog across the latest snapshot of every
    tracked shop. q is required: unlike a dashboard that always renders
    the same small flagged-row set, an open-ended scan of the whole
    catalog needs a real search term to bound it, so an empty q returns
    nothing rather than paging through the entire multi-shop inventory.
    """
    if not q:
        return [], 0
    ids = [shop_id] if shop_id is not None else list(shop_labels_by_id)
    return _catalog_scan_page(session, shop_labels_by_id, ids, q, category, sort, page, per_page, latest)


def get_category_page(
    session,
    shop_labels_by_id,
    category,
    shop_id=None,
    q=None,
    sort="price",
    page=1,
    per_page=50,
    latest=None,
):
    """One page of everything currently priced in one top-level category
    group, across the latest snapshot of every tracked shop -- the
    category-browse equivalent of search_products. category is required:
    it's the thing bounding this scan, the same role q plays there."""
    if not category:
        return [], 0
    ids = [shop_id] if shop_id is not None else list(shop_labels_by_id)
    return _catalog_scan_page(session, shop_labels_by_id, ids, q, category, sort, page, per_page, latest)


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
                ItemPrice.code,
            )
            .filter(
                ItemPrice.snapshot_id == snap.id,
                ItemPrice.price > 0,
                ItemPrice.product_id.isnot(None),
            )
        )
        query = _apply_text_filters(query, q=q, category=category)
        label = shop_labels_by_id[shop_id]
        for product_id, price, category_value, size_info, metric_unit_description, code in query.all():
            by_product[product_id].append(
                {
                    "shop_id": shop_id,
                    "shop_label": label,
                    "price": price,
                    "category": category_value,
                    "size_info": size_info,
                    "metric_unit_description": metric_unit_description,
                    "code": code,
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


def iter_all_bugs(session, shop_labels_by_id, shop_id=None, q=None, category=None):
    """Yield every currently-flagged price bug (zero-price or an
    implausible placeholder 30-day-low), across all shops or one
    specific shop -- same streaming, column-only, per-shop-latest-
    snapshot shape as iter_all_sales, for a bugs-only CSV export.
    Naturally bounded: bug rows are a small fraction of any snapshot.
    """
    ids = [shop_id] if shop_id is not None else list(shop_labels_by_id)
    latest = get_latest_snapshots_for_all_shops(session, ids)
    for sid, snap in latest.items():
        label = shop_labels_by_id[sid]
        query = session.query(
            ItemPrice.name,
            ItemPrice.category,
            ItemPrice.price,
            ItemPrice.l30d_price,
            ItemPrice.is_zero_price_bug,
            ItemPrice.is_placeholder_bug,
            ItemPrice.code,
        ).filter(
            ItemPrice.snapshot_id == snap.id,
            or_(ItemPrice.is_zero_price_bug.is_(True), ItemPrice.is_placeholder_bug.is_(True)),
        )
        query = _apply_text_filters(query, q=q, category=category)
        for name, cat, price, l30d, is_zero, is_placeholder, code in query:
            yield {
                "shop_id": sid,
                "shop_label": label,
                "name": name,
                "category": cat,
                "code": code,
                "price": price,
                "l30d_price": l30d,
                "bug_type": "zero_price" if is_zero else "placeholder_reference",
                "snapshot_date": snap.snapshot_date,
            }


def iter_all_drops(session, shop_labels_by_id, shop_id=None, q=None, category=None, min_drop_pct=None):
    """Yield every price drop between a shop's previous snapshot and its
    latest one, across all shops or one specific shop -- for a CSV
    export of the /drops view. Reuses _shop_price_drops_query (the same
    self-join /drops itself queries) per shop instead of UNION ALL-ing
    them the way get_price_drops does for pagination -- there's no
    OFFSET/LIMIT here to push into SQL, so a plain per-shop loop is
    simpler and just as bounded (a day's drops are a small fraction of
    the catalog).
    """
    ids = [shop_id] if shop_id is not None else list(shop_labels_by_id)
    latest = get_latest_snapshots_for_all_shops(session, ids)
    for sid, snap in latest.items():
        previous = _get_previous_snapshot(session, sid, snap.snapshot_date)
        if previous is None:
            continue
        label = shop_labels_by_id[sid]
        query = _shop_price_drops_query(
            session, sid, snap, previous, q=q, category=category, min_drop_pct=min_drop_pct
        ).order_by(literal_column("drop_pct").desc())
        for name, cat, code, price, prev_price, size_info, mud, row_shop_id, snapshot_date, drop_pct in query:
            yield {
                "shop_id": row_shop_id,
                "shop_label": label,
                "name": name,
                "category": cat,
                "code": code,
                "price": price,
                "prev_price": prev_price,
                "drop_pct": drop_pct,
                "snapshot_date": snapshot_date,
            }


def get_new_verified_deals(session, shop_labels_by_id, limit=50, latest=None):
    """Verified deals that appeared in a shop's latest snapshot but
    weren't already a verified deal in its previous one -- the "what's
    newly on offer today" set a deals feed should announce, as opposed
    to re-announcing every deal that's simply still running from
    yesterday. A shop with no previous snapshot yet has nothing to
    compare against, so every one of today's verified deals counts as
    new for it. Column-only, sorted by discount size, capped at `limit`
    across all shops combined -- a feed reader has no use for more.
    """
    ids = list(shop_labels_by_id)
    if latest is None:
        latest = get_latest_snapshots_for_all_shops(session, ids)

    results = []
    for sid, snap in latest.items():
        label = shop_labels_by_id[sid]
        previous = _get_previous_snapshot(session, sid, snap.snapshot_date)
        base_query = session.query(
            ItemPrice.name,
            ItemPrice.category,
            ItemPrice.code,
            ItemPrice.price,
            ItemPrice.full_price,
            ItemPrice.deal_pct,
        ).filter(
            ItemPrice.snapshot_id == snap.id,
            ItemPrice.is_verified_deal.is_(True),
            ItemPrice.code.isnot(None),
        )
        if previous is not None:
            Yesterday = aliased(ItemPrice)
            base_query = base_query.outerjoin(
                Yesterday,
                and_(Yesterday.code == ItemPrice.code, Yesterday.snapshot_id == previous.id),
            ).filter(or_(Yesterday.id.is_(None), Yesterday.is_verified_deal.isnot(True)))
        for name, cat, code, price, full_price, deal_pct in base_query:
            results.append(
                {
                    "shop_id": sid,
                    "shop_label": label,
                    "name": name,
                    "category": cat,
                    "code": code,
                    "price": price,
                    "full_price": full_price,
                    "deal_pct": deal_pct,
                    "snapshot_date": snap.snapshot_date,
                }
            )

    results.sort(key=lambda r: r["deal_pct"] or 0, reverse=True)
    return results[:limit]


BASKET_MAX_ITEMS = 25
BASKET_PER_TERM_LIMIT = 500


def get_basket_comparison(session, shop_labels_by_id, terms, latest=None):
    """For a shopping list of free-text product names, find each line's
    cheapest match in every shop's latest snapshot, then work out (a)
    each shop's running total for however many lines it actually
    stocks, and (b) the cheapest possible total if each line is bought
    at whichever shop has it cheapest ("the split").

    Each line runs its own bounded, LIMIT'd query (name_fold ILIKE,
    same matching rule as /search) across every shop's latest snapshot
    at once -- not one query per shop -- so cost scales with basket
    size, not shop count. Callers are expected to have already capped
    `terms` to BASKET_MAX_ITEMS; this function does not re-check that,
    since the caller (the /basket route) is what decides how to tell
    the user about the truncation.
    """
    ids = list(shop_labels_by_id)
    if latest is None:
        latest = get_latest_snapshots_for_all_shops(session, ids)
    if not latest:
        return {"lines": [], "shop_totals": [], "split": None}
    shop_by_snapshot = {snap.id: sid for sid, snap in latest.items()}
    snapshot_ids = list(shop_by_snapshot)

    lines = []
    for term in terms:
        query = (
            session.query(ItemPrice.snapshot_id, ItemPrice.name, ItemPrice.code, ItemPrice.price)
            .filter(
                ItemPrice.snapshot_id.in_(snapshot_ids),
                ItemPrice.price > 0,
                ItemPrice.name_fold.ilike(f"%{fold_name(term)}%"),
            )
            .order_by(ItemPrice.price.asc())
            .limit(BASKET_PER_TERM_LIMIT)
        )
        # Rows arrive cheapest-first, so the first row seen for a given
        # shop is that shop's cheapest match for this line -- later,
        # pricier rows from the same shop are simply skipped.
        matches = {}
        for snapshot_id, name, code, price in query:
            sid = shop_by_snapshot[snapshot_id]
            if sid not in matches:
                matches[sid] = {"name": name, "code": code, "price": price}
        lines.append({"term": term, "matches": matches})

    shop_totals = []
    for sid, label in shop_labels_by_id.items():
        if sid not in latest:
            continue
        found = [line["matches"][sid] for line in lines if sid in line["matches"]]
        if not found:
            continue
        shop_totals.append(
            {
                "shop_id": sid,
                "shop_label": label,
                "found_count": len(found),
                "missing_count": len(lines) - len(found),
                "total": sum(m["price"] for m in found),
            }
        )
    shop_totals.sort(key=lambda s: (-s["found_count"], s["total"]))

    split = None
    if lines and all(line["matches"] for line in lines):
        split_lines = []
        for line in lines:
            sid, m = min(line["matches"].items(), key=lambda kv: kv[1]["price"])
            split_lines.append(
                {"term": line["term"], "shop_id": sid, "shop_label": shop_labels_by_id[sid], **m}
            )
        split_total = sum(sl["price"] for sl in split_lines)
        shops_needed = len({sl["shop_id"] for sl in split_lines})
        best_single = min((s["total"] for s in shop_totals if s["found_count"] == len(lines)), default=None)
        split = {
            "lines": split_lines,
            "total": split_total,
            "shops_needed": shops_needed,
            "savings_vs_best_single_shop": (best_single - split_total) if best_single is not None else None,
        }

    return {"lines": lines, "shop_totals": shop_totals, "split": split}


def get_price_extremes(
    session,
    shop_labels_by_id,
    shop_id=None,
    category=None,
    min_swing_pct=None,
    page=1,
    per_page=50,
):
    """One page of the price_extremes rollup table (see
    ingest.update_price_extremes_rollup), sorted by biggest swing first
    -- "which currently-listed products have moved the most" across
    however much history retention.py has kept. This table is tiny
    (one row per currently-listed item) and precomputed nightly, so
    unlike most of this module's other filters there's no scan-size
    guard to worry about here."""
    query = session.query(PriceExtreme)
    if shop_id is not None:
        query = query.filter(PriceExtreme.shop_id == shop_id)
    else:
        query = query.filter(PriceExtreme.shop_id.in_(list(shop_labels_by_id)))
    if category:
        query = query.filter(PriceExtreme.category.ilike(f"{category}%"))
    if min_swing_pct:
        query = query.filter(PriceExtreme.swing_pct >= min_swing_pct)

    total = query.count()
    query = query.order_by(PriceExtreme.swing_pct.desc())

    last_page = max(1, math.ceil(total / per_page))
    page = min(max(1, page), last_page)
    rows = query.offset((page - 1) * per_page).limit(per_page).all()

    results = [
        {
            "shop_id": r.shop_id,
            "shop_label": shop_labels_by_id.get(r.shop_id, "?"),
            "name": r.name,
            "category": r.category,
            "code": r.code,
            "current_price": r.current_price,
            "min_price": r.min_price,
            "max_price": r.max_price,
            "swing_pct": r.swing_pct,
        }
        for r in rows
    ]
    return results, total


def get_category_trend(session, category, days=90):
    """Oldest-to-newest (date, avg_price, item_count, bug_count) history
    for one top-level category group, from the category_daily_summaries
    rollup (see ingest.update_category_daily_summary) -- kept forever,
    unlike item_prices, so this can look back further than retention.py's
    ~90-day window. One aggregate query over a small table."""
    rows = (
        session.query(
            CategoryDailySummary.snapshot_date,
            CategoryDailySummary.avg_price,
            CategoryDailySummary.item_count,
            CategoryDailySummary.bug_count,
        )
        .filter(CategoryDailySummary.category == category)
        .order_by(CategoryDailySummary.snapshot_date.desc())
        .limit(days)
        .all()
    )
    return list(reversed(rows))


def get_cheapest_unit_price_by_product(session, product_ids, shop_labels_by_id, latest=None):
    """For each of the given product_ids (the same real-world product,
    matched across chains by product_matching.py -- not just an exact
    name string), the cheapest current per-standard-unit price anywhere
    across the tracked shops right now, and which shop/listing has it.

    This is what catches a "verified deal" that's real (the price is
    genuinely down from this listing's own recent history) but still not
    a good buy -- a big % off a small, expensive-per-litre jar can still
    cost more per litre than another shop's everyday-priced large one.
    ItemPrice.unit_price is already normalized to a common weight/volume
    scale at ingest time (see price_utils.derive_unit_price), so this
    needs no per-row re-parsing.

    One query across every shop's latest snapshot for the WHOLE batch of
    product_ids, not one query per product -- same batch-by-shop
    discipline as get_bug_streaks/get_deal_streaks.

    Returns {product_id: {"unit_price", "shop_id", "shop_label", "code"}},
    omitting any product_id with no row carrying a known unit_price.
    """
    product_ids = [pid for pid in product_ids if pid is not None]
    if not product_ids:
        return {}
    if latest is None:
        latest = get_latest_snapshots_for_all_shops(session, list(shop_labels_by_id))
    if not latest:
        return {}
    shop_by_snapshot = {snap.id: sid for sid, snap in latest.items()}

    rows = session.query(
        ItemPrice.snapshot_id,
        ItemPrice.product_id,
        ItemPrice.unit_price,
        ItemPrice.code,
    ).filter(
        ItemPrice.snapshot_id.in_(list(shop_by_snapshot)),
        ItemPrice.product_id.in_(product_ids),
        ItemPrice.unit_price.isnot(None),
        ItemPrice.price > 0,
    )

    cheapest = {}
    for snapshot_id, product_id, unit_price, code in rows:
        current = cheapest.get(product_id)
        if current is None or unit_price < current["unit_price"]:
            sid = shop_by_snapshot[snapshot_id]
            cheapest[product_id] = {
                "unit_price": unit_price,
                "shop_id": sid,
                "shop_label": shop_labels_by_id[sid],
                "code": code,
            }
    return cheapest
