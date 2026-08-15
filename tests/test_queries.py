"""Direct tests for queries.py functions that need database access,
bypassing the Flask route/template layer for precision on things like
exact pagination math and join semantics that a route-level test (which
mostly just checks page text) can't easily pin down."""

import pytest

from db import SessionLocal
from ingest import store_snapshot
from queries import (
    compare_across_shops,
    deal_tier,
    get_basket_comparison,
    get_category_page,
    get_category_trend,
    get_category_unit_price_medians,
    get_cheapest_unit_price_by_product,
    get_deals_page,
    get_new_verified_deals,
    get_price_drops,
    get_price_extremes,
    get_product_across_shops,
    is_category_competitive,
    iter_all_bugs,
    iter_all_drops,
    search_products,
)
from shops import SHOPS

SHOP_A = SHOPS[0]["id"]
SHOP_A_LABEL = SHOPS[0]["label"]
SHOP_B = SHOPS[1]["id"]
SHOP_B_LABEL = SHOPS[1]["label"]


def _catalog(items):
    return {
        "information": {"title": "T", "address": {"description": "A"}, "is_open": True},
        "menu": {"categories": [{"name": "Cat", "items": items}]},
    }


def _item(id_, name, price, **extra):
    d = {"id": id_, "code": f"code-{id_}", "name": name, "price": price, "tags": []}
    d.update(extra)
    return d


def test_get_price_drops_matches_by_code_not_item_id():
    # get_price_drops must join snapshots by ItemPrice.code, not
    # item_id -- item_id is only unique within one shop's own catalog on
    # one day, and e-food can reassign it to an unrelated product the
    # next day. If the join ever regressed to item_id, this would
    # wrongly report "Bleach" as a price drop from "Milk"'s old price.
    session = SessionLocal()
    try:
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-12",
            _catalog([{"id": 42, "code": "milk-code", "name": "Milk", "price": 2.0, "tags": []}]),
        )
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-13",
            _catalog([{"id": 42, "code": "bleach-code", "name": "Bleach", "price": 1.0, "tags": []}]),
        )
        rows, total = get_price_drops(session, {SHOP_A: SHOP_A_LABEL})
        assert total == 0
        assert rows == []
    finally:
        session.close()


def test_get_price_drops_finds_real_code_matched_drop():
    session = SessionLocal()
    try:
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-12",
            _catalog([{"id": 1, "code": "c1", "name": "Milk", "price": 2.0, "tags": []}]),
        )
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-13",
            _catalog([{"id": 1, "code": "c1", "name": "Milk", "price": 1.5, "tags": []}]),
        )
        rows, total = get_price_drops(session, {SHOP_A: SHOP_A_LABEL})
        assert total == 1
        assert rows[0]["name"] == "Milk"
        assert rows[0]["drop_pct"] == pytest.approx(25.0)
    finally:
        session.close()


def test_get_price_drops_min_drop_pct_excludes_smaller_drops():
    session = SessionLocal()
    try:
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-12",
            _catalog([{"id": 1, "code": "c1", "name": "Milk", "price": 2.0, "tags": []}]),
        )
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-13",
            _catalog([{"id": 1, "code": "c1", "name": "Milk", "price": 1.5, "tags": []}]),
        )
        # 25% drop -- a 30% floor must exclude it, a 20% floor must not.
        _, total_high = get_price_drops(session, {SHOP_A: SHOP_A_LABEL}, min_drop_pct=30)
        assert total_high == 0
        _, total_low = get_price_drops(session, {SHOP_A: SHOP_A_LABEL}, min_drop_pct=20)
        assert total_low == 1
    finally:
        session.close()


def test_get_price_drops_sort_price_orders_cheapest_first():
    session = SessionLocal()
    try:
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-12",
            _catalog(
                [
                    {"id": 1, "code": "c1", "name": "Expensive", "price": 10.0, "tags": []},
                    {"id": 2, "code": "c2", "name": "Cheap", "price": 4.0, "tags": []},
                ]
            ),
        )
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-13",
            _catalog(
                [
                    {"id": 1, "code": "c1", "name": "Expensive", "price": 8.0, "tags": []},
                    {"id": 2, "code": "c2", "name": "Cheap", "price": 3.0, "tags": []},
                ]
            ),
        )
        session.commit()

        rows, _ = get_price_drops(session, {SHOP_A: SHOP_A_LABEL}, sort="price")
        assert [r["name"] for r in rows] == ["Cheap", "Expensive"]

        rows, _ = get_price_drops(session, {SHOP_A: SHOP_A_LABEL}, sort="drop_pct")
        # Expensive: 10 -> 8 = 20% drop; Cheap: 4 -> 3 = 25% drop.
        assert [r["name"] for r in rows] == ["Cheap", "Expensive"]
    finally:
        session.close()


def test_get_price_drops_sort_name_orders_alphabetically():
    session = SessionLocal()
    try:
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-12",
            _catalog(
                [
                    {"id": 1, "code": "c1", "name": "Zeta", "price": 10.0, "tags": []},
                    {"id": 2, "code": "c2", "name": "Alpha", "price": 10.0, "tags": []},
                ]
            ),
        )
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-13",
            _catalog(
                [
                    {"id": 1, "code": "c1", "name": "Zeta", "price": 5.0, "tags": []},
                    {"id": 2, "code": "c2", "name": "Alpha", "price": 5.0, "tags": []},
                ]
            ),
        )
        session.commit()

        rows, _ = get_price_drops(session, {SHOP_A: SHOP_A_LABEL}, sort="name")
        assert [r["name"] for r in rows] == ["Alpha", "Zeta"]
    finally:
        session.close()


def test_get_price_drops_excludes_null_code_rows():
    session = SessionLocal()
    try:
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-12",
            _catalog([{"id": 1, "name": "No Code Item", "price": 2.0, "tags": []}]),  # no "code" key
        )
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-13",
            _catalog([{"id": 1, "name": "No Code Item", "price": 1.0, "tags": []}]),
        )
        rows, total = get_price_drops(session, {SHOP_A: SHOP_A_LABEL})
        assert total == 0
    finally:
        session.close()


def test_get_price_drops_excludes_zero_price_rows():
    session = SessionLocal()
    try:
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-12",
            _catalog([{"id": 1, "code": "c1", "name": "Free Item", "price": 1.0, "tags": []}]),
        )
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-13",
            # Price fell to 0 -- a real drop by the raw numbers, but
            # excluded since a 0-priced listing is a data bug, not a deal.
            _catalog([{"id": 1, "code": "c1", "name": "Free Item", "price": 0.0, "tags": []}]),
        )
        rows, total = get_price_drops(session, {SHOP_A: SHOP_A_LABEL})
        assert total == 0
    finally:
        session.close()


def test_search_products_total_and_pagination_math():
    session = SessionLocal()
    try:
        items = [_item(i, f"Widget {i:02d}", float(i)) for i in range(1, 6)]  # 5 matching items
        store_snapshot(session, SHOP_A, SHOP_A_LABEL, "2026-08-13", _catalog(items))
        session.commit()

        rows, total = search_products(session, {SHOP_A: SHOP_A_LABEL}, "widget", page=1, per_page=2)
        assert total == 5
        assert len(rows) == 2

        rows2, total2 = search_products(session, {SHOP_A: SHOP_A_LABEL}, "widget", page=3, per_page=2)
        assert total2 == 5
        assert len(rows2) == 1  # last page, the remainder after 2 full pages of 2
    finally:
        session.close()


def test_compare_across_shops_groups_by_fuzzy_matched_product_not_exact_name():
    # The whole point of product_matching.py: two different chains
    # phrasing the same product differently (word order, size format)
    # must land in one comparison group, not be missed the way exact
    # name matching would miss it.
    session = SessionLocal()
    try:
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-13",
            _catalog([{"id": 1, "code": "a1", "name": "Anatoli Κουρκουμάς 60g", "price": 1.50, "tags": []}]),
        )
        store_snapshot(
            session,
            SHOP_B,
            SHOP_B_LABEL,
            "2026-08-13",
            _catalog([{"id": 2, "code": "b1", "name": "Κουρκουμάς Anatoli 100g", "price": 1.90, "tags": []}]),
        )
        session.commit()

        shop_labels = {SHOP_A: SHOP_A_LABEL, SHOP_B: SHOP_B_LABEL}
        groups = compare_across_shops(session, shop_labels, min_spread_pct=5)
        assert len(groups) == 1
        group = groups[0]
        assert {r["shop_id"] for r in group["rows"]} == {SHOP_A, SHOP_B}
        assert group["low"] == pytest.approx(1.50)
        assert group["high"] == pytest.approx(1.90)
    finally:
        session.close()


def test_compare_across_shops_excludes_unmatched_no_code_items():
    session = SessionLocal()
    try:
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-13",
            _catalog([{"id": 1, "name": "Χωρίς Κωδικό", "price": 1.0, "tags": []}]),  # no "code"
        )
        store_snapshot(
            session,
            SHOP_B,
            SHOP_B_LABEL,
            "2026-08-13",
            _catalog([{"id": 2, "name": "Χωρίς Κωδικό", "price": 2.0, "tags": []}]),  # no "code"
        )
        session.commit()

        shop_labels = {SHOP_A: SHOP_A_LABEL, SHOP_B: SHOP_B_LABEL}
        groups = compare_across_shops(session, shop_labels, min_spread_pct=5)
        assert groups == []
    finally:
        session.close()


def test_get_product_across_shops_finds_fuzzy_matched_product():
    session = SessionLocal()
    try:
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-13",
            _catalog([{"id": 1, "code": "a1", "name": "Anatoli Κουρκουμάς 60g", "price": 1.50, "tags": []}]),
        )
        store_snapshot(
            session,
            SHOP_B,
            SHOP_B_LABEL,
            "2026-08-13",
            _catalog([{"id": 2, "code": "b1", "name": "Κουρκουμάς Anatoli 100g", "price": 1.90, "tags": []}]),
        )
        session.commit()

        from db import ItemPrice

        stored = session.query(ItemPrice).filter_by(code="a1").one()
        shop_labels = {SHOP_A: SHOP_A_LABEL, SHOP_B: SHOP_B_LABEL}
        results = get_product_across_shops(session, stored.product_id, shop_labels, exclude_shop_id=SHOP_A)
        assert len(results) == 1
        assert results[0]["shop_id"] == SHOP_B
        assert results[0]["price"] == pytest.approx(1.90)
    finally:
        session.close()


def test_get_product_across_shops_none_product_id_returns_empty():
    session = SessionLocal()
    try:
        assert get_product_across_shops(session, None, {SHOP_A: SHOP_A_LABEL}) == []
    finally:
        session.close()


def test_get_product_across_shops_picks_cheapest_when_shop_has_two_listings():
    # Regression: the batched single-query rewrite (was one .first() query
    # per shop) must still keep only each shop's cheapest listing when a
    # shop carries two rows under the same matched product (e.g. deposit
    # vs. no-deposit bottles), not both.
    session = SessionLocal()
    try:
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-13",
            _catalog([{"id": 1, "code": "a1", "name": "Νερό Εμφιαλωμένο 1.5L", "price": 0.60, "tags": []}]),
        )
        store_snapshot(
            session,
            SHOP_B,
            SHOP_B_LABEL,
            "2026-08-13",
            _catalog(
                [
                    {"id": 2, "code": "b1", "name": "Νερό Εμφιαλωμένο 1.5L", "price": 0.65, "tags": []},
                    {
                        "id": 3,
                        "code": "b2",
                        "name": "Νερό Εμφιαλωμένο 1.5L Με Επιστροφή",
                        "price": 0.80,
                        "tags": [],
                    },
                ]
            ),
        )
        session.commit()

        from db import ItemPrice

        a1 = session.query(ItemPrice).filter_by(code="a1").one()
        b1 = session.query(ItemPrice).filter_by(code="b1").one()
        b2 = session.query(ItemPrice).filter_by(code="b2").one()
        # Force b2 onto the same product as b1, regardless of whether the
        # fuzzy matcher happened to merge them on its own.
        b2.product_id = b1.product_id
        session.commit()

        shop_labels = {SHOP_A: SHOP_A_LABEL, SHOP_B: SHOP_B_LABEL}
        results = get_product_across_shops(session, a1.product_id, shop_labels, exclude_shop_id=SHOP_A)
        assert len(results) == 1
        assert results[0]["shop_id"] == SHOP_B
        assert results[0]["price"] == pytest.approx(0.65)
    finally:
        session.close()


def test_get_product_across_shops_accepts_prefetched_latest():
    session = SessionLocal()
    try:
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-13",
            _catalog([{"id": 1, "code": "a1", "name": "Anatoli Κουρκουμάς 60g", "price": 1.50, "tags": []}]),
        )
        store_snapshot(
            session,
            SHOP_B,
            SHOP_B_LABEL,
            "2026-08-13",
            _catalog([{"id": 2, "code": "b1", "name": "Κουρκουμάς Anatoli 100g", "price": 1.90, "tags": []}]),
        )
        session.commit()

        from db import ItemPrice
        from queries import get_latest_snapshots_for_all_shops

        stored = session.query(ItemPrice).filter_by(code="a1").one()
        shop_labels = {SHOP_A: SHOP_A_LABEL, SHOP_B: SHOP_B_LABEL}
        latest = get_latest_snapshots_for_all_shops(session, list(shop_labels))
        results = get_product_across_shops(
            session, stored.product_id, shop_labels, exclude_shop_id=SHOP_A, latest=latest
        )
        assert len(results) == 1
        assert results[0]["shop_id"] == SHOP_B
    finally:
        session.close()


# ---------- deal_tier ----------


def test_deal_tier_none_pct():
    assert deal_tier(None) is None


def test_deal_tier_at_verified_deal_floor_is_good():
    # 20% is the floor find_verified_deep_discounts already enforces --
    # every real verified deal is at least this, and the "great" tier
    # only distinguishes an exceptional one from a merely-real one.
    assert deal_tier(20.0) == "good"


def test_deal_tier_below_great_threshold_is_good():
    assert deal_tier(34.9) == "good"


def test_deal_tier_at_great_threshold_is_great():
    assert deal_tier(35.0) == "great"


def test_deal_tier_well_above_threshold_is_great():
    assert deal_tier(60.0) == "great"


# ---------- get_shop_bug_rates ----------


def test_get_shop_bug_rates_ranks_by_rate_not_raw_count():
    # Shop A: 40 items, 2 bugs -> 5% rate. Shop B: 20 items, 3 bugs -> 15%
    # rate. Shop B has fewer raw bugs but the higher rate must rank first.
    session = SessionLocal()
    try:
        a_items = [_item(i, f"A Item {i:02d}", 1.0) for i in range(38)]
        a_items += [_item(90, "A Bug 1", 0.0), _item(91, "A Bug 2", 0.0)]
        store_snapshot(session, SHOP_A, SHOP_A_LABEL, "2026-08-13", _catalog(a_items))

        b_items = [_item(i, f"B Item {i:02d}", 1.0) for i in range(17)]
        b_items += [_item(90, "B Bug 1", 0.0), _item(91, "B Bug 2", 0.0), _item(92, "B Bug 3", 0.0)]
        store_snapshot(session, SHOP_B, SHOP_B_LABEL, "2026-08-13", _catalog(b_items))
        session.commit()

        from queries import get_latest_snapshots_for_all_shops, get_shop_bug_rates

        shop_labels = {SHOP_A: SHOP_A_LABEL, SHOP_B: SHOP_B_LABEL}
        latest = get_latest_snapshots_for_all_shops(session, list(shop_labels))
        rows = get_shop_bug_rates(latest, shop_labels, min_items=20)
        assert [r["shop_id"] for r in rows] == [SHOP_B, SHOP_A]
        assert rows[0]["bug_rate"] == pytest.approx(0.15)
        assert rows[1]["bug_rate"] == pytest.approx(0.05)
    finally:
        session.close()


def test_get_shop_bug_rates_excludes_shops_below_min_items():
    session = SessionLocal()
    try:
        store_snapshot(
            session, SHOP_A, SHOP_A_LABEL, "2026-08-13", _catalog([_item(1, "Tiny Bug", 0.0)])
        )
        session.commit()

        from queries import get_latest_snapshots_for_all_shops, get_shop_bug_rates

        shop_labels = {SHOP_A: SHOP_A_LABEL}
        latest = get_latest_snapshots_for_all_shops(session, list(shop_labels))
        assert get_shop_bug_rates(latest, shop_labels, min_items=20) == []
    finally:
        session.close()


# ---------- indexes ----------


def test_composite_indexes_exist_on_item_prices():
    from sqlalchemy import inspect

    from db import engine

    index_names = {ix["name"] for ix in inspect(engine).get_indexes("item_prices")}
    for name in (
        "ix_item_prices_snapshot_zero_bug",
        "ix_item_prices_snapshot_placeholder_bug",
        "ix_item_prices_snapshot_deal",
        "ix_item_prices_snapshot_code",
        "ix_item_prices_snapshot_product",
    ):
        assert name in index_names


# ---------- get_low_confidence_matches ----------


def test_get_low_confidence_matches_excludes_confident_matches_worst_first():
    session = SessionLocal()
    try:
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-13",
            _catalog([{"id": 1, "code": "a1", "name": "Anatoli Κουρκουμάς 60g", "price": 1.5, "tags": []}]),
        )
        store_snapshot(
            session,
            SHOP_B,
            SHOP_B_LABEL,
            "2026-08-13",
            _catalog(
                [
                    {"id": 2, "code": "b1", "name": "Κουρκουμάς Anatoli 100g", "price": 1.9, "tags": []},
                    {"id": 3, "code": "b2", "name": "Κάτι Άλλο 200g", "price": 2.0, "tags": []},
                ]
            ),
        )
        session.commit()

        from db import ProductListing
        from queries import get_low_confidence_matches

        b1 = session.query(ProductListing).filter_by(code="b1").one()
        b2 = session.query(ProductListing).filter_by(code="b2").one()
        # Force specific values regardless of what real matching produced
        # -- this test is about the query's filter/order, not the matcher.
        b1.match_confidence = 0.905  # in the reviewable band
        b2.match_confidence = 0.999  # above max_confidence, excluded
        session.commit()

        rows = get_low_confidence_matches(session, max_confidence=0.93)
        assert len(rows) == 1
        assert rows[0]["code"] == "b1"
        assert rows[0]["shop_id"] == SHOP_B
        assert rows[0]["confidence"] == pytest.approx(0.905)
    finally:
        session.close()


def test_get_low_confidence_matches_empty_when_none_below_threshold():
    session = SessionLocal()
    try:
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-13",
            _catalog([{"id": 1, "code": "a1", "name": "Anatoli Κουρκουμάς 60g", "price": 1.5, "tags": []}]),
        )
        session.commit()

        from queries import get_low_confidence_matches

        # A freshly created product always stores confidence 1.0.
        assert get_low_confidence_matches(session, max_confidence=0.93) == []
    finally:
        session.close()


# ---------- get_bug_streaks ----------


def test_get_bug_streaks_counts_consecutive_days():
    session = SessionLocal()
    try:
        from queries import get_bug_streaks

        # code "c1" is a zero-price bug on days 1-3 (consecutive,
        # streak 3); code "c2" only on day 3 (streak 1); code "c3" was
        # a bug on day 1 but recovered by day 2, then broke again on
        # day 3 -- the streak must stop at the first gap, not count
        # every day it was ever broken.
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-11",
            _catalog(
                [
                    {"id": 1, "code": "c1", "name": "Bug One", "price": 0.0, "tags": []},
                    {"id": 3, "code": "c3", "name": "Bug Three", "price": 0.0, "tags": []},
                ]
            ),
        )
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-12",
            _catalog(
                [
                    {"id": 1, "code": "c1", "name": "Bug One", "price": 0.0, "tags": []},
                    {"id": 3, "code": "c3", "name": "Bug Three", "price": 1.5, "tags": []},  # recovered
                ]
            ),
        )
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-13",
            _catalog(
                [
                    {"id": 1, "code": "c1", "name": "Bug One", "price": 0.0, "tags": []},
                    {"id": 2, "code": "c2", "name": "Bug Two", "price": 0.0, "tags": []},
                    {"id": 3, "code": "c3", "name": "Bug Three", "price": 0.0, "tags": []},  # broke again
                ]
            ),
        )
        session.commit()

        streaks = get_bug_streaks(session, SHOP_A, ["c1", "c2", "c3"], "zero")
        assert streaks["c1"] == 3
        assert streaks["c2"] == 1
        assert streaks["c3"] == 1
    finally:
        session.close()


def test_get_bug_streaks_empty_codes_returns_empty():
    session = SessionLocal()
    try:
        from queries import get_bug_streaks

        assert get_bug_streaks(session, SHOP_A, [], "zero") == {}
    finally:
        session.close()


def test_get_bug_streaks_respects_max_days_cap():
    session = SessionLocal()
    try:
        from queries import get_bug_streaks

        for day in ("2026-08-11", "2026-08-12", "2026-08-13"):
            store_snapshot(
                session,
                SHOP_A,
                SHOP_A_LABEL,
                day,
                _catalog([{"id": 1, "code": "c1", "name": "Bug One", "price": 0.0, "tags": []}]),
            )
        session.commit()

        # Only the most recent 2 snapshots count toward the streak.
        streaks = get_bug_streaks(session, SHOP_A, ["c1"], "zero", max_days=2)
        assert streaks["c1"] == 2
    finally:
        session.close()


def test_get_deal_streaks_counts_consecutive_verified_days():
    session = SessionLocal()
    try:
        from queries import get_deal_streaks

        deal_item = {
            "id": 1,
            "code": "c1",
            "name": "Ongoing Deal",
            "price": 3.0,
            "full_price": 6.0,
            "tags": ["l30d:5.0"],
        }
        not_deal_item = {"id": 1, "code": "c1", "name": "Ongoing Deal", "price": 6.0, "tags": []}

        # Verified on days 1-2, NOT verified on day 2... wait -- one day
        # at full price (not a deal) between two verified days, so the
        # streak must stop at the first gap looking backward from today.
        store_snapshot(session, SHOP_A, SHOP_A_LABEL, "2026-08-11", _catalog([deal_item]))
        store_snapshot(session, SHOP_A, SHOP_A_LABEL, "2026-08-12", _catalog([not_deal_item]))
        store_snapshot(session, SHOP_A, SHOP_A_LABEL, "2026-08-13", _catalog([deal_item]))
        session.commit()

        streaks = get_deal_streaks(session, SHOP_A, ["c1"])
        assert streaks["c1"] == 1  # only today -- yesterday broke the streak
    finally:
        session.close()


def test_get_deal_streaks_counts_a_real_multi_day_run():
    session = SessionLocal()
    try:
        from queries import get_deal_streaks

        deal_item = {
            "id": 1,
            "code": "c1",
            "name": "Real Streak",
            "price": 3.0,
            "full_price": 6.0,
            "tags": ["l30d:5.0"],
        }
        for day in ("2026-08-11", "2026-08-12", "2026-08-13"):
            store_snapshot(session, SHOP_A, SHOP_A_LABEL, day, _catalog([deal_item]))
        session.commit()

        streaks = get_deal_streaks(session, SHOP_A, ["c1"])
        assert streaks["c1"] == 3
    finally:
        session.close()


def test_iter_all_bugs_yields_zero_price_and_placeholder_rows():
    session = SessionLocal()
    try:
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-13",
            _catalog(
                [
                    {"id": 1, "code": "c1", "name": "Free Bug", "price": 0.0, "tags": []},
                    {
                        "id": 2,
                        "code": "c2",
                        "name": "Placeholder Bug",
                        "price": 2.0,
                        "tags": ["l30d:0.01"],
                    },
                    {"id": 3, "code": "c3", "name": "Clean Item", "price": 1.0, "tags": []},
                ]
            ),
        )
        session.commit()

        rows = list(iter_all_bugs(session, {SHOP_A: SHOP_A_LABEL}))
        names_and_types = {(r["name"], r["bug_type"]) for r in rows}
        assert ("Free Bug", "zero_price") in names_and_types
        assert ("Placeholder Bug", "placeholder_reference") in names_and_types
        assert not any(r["name"] == "Clean Item" for r in rows)
    finally:
        session.close()


def test_iter_all_bugs_filters_by_shop():
    session = SessionLocal()
    try:
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-13",
            _catalog([{"id": 1, "code": "c1", "name": "Bug In A", "price": 0.0, "tags": []}]),
        )
        store_snapshot(
            session,
            SHOP_B,
            SHOP_B_LABEL,
            "2026-08-13",
            _catalog([{"id": 2, "code": "c2", "name": "Bug In B", "price": 0.0, "tags": []}]),
        )
        session.commit()

        rows = list(iter_all_bugs(session, {SHOP_A: SHOP_A_LABEL, SHOP_B: SHOP_B_LABEL}, shop_id=SHOP_A))
        names = {r["name"] for r in rows}
        assert names == {"Bug In A"}
    finally:
        session.close()


def test_iter_all_drops_yields_only_price_drops():
    session = SessionLocal()
    try:
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-12",
            _catalog(
                [
                    {"id": 1, "code": "c1", "name": "Dropped", "price": 10.0, "tags": []},
                    {"id": 2, "code": "c2", "name": "Steady", "price": 3.0, "tags": []},
                ]
            ),
        )
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-13",
            _catalog(
                [
                    {"id": 1, "code": "c1", "name": "Dropped", "price": 7.5, "tags": []},
                    {"id": 2, "code": "c2", "name": "Steady", "price": 3.0, "tags": []},
                ]
            ),
        )
        session.commit()

        rows = list(iter_all_drops(session, {SHOP_A: SHOP_A_LABEL}))
        assert len(rows) == 1
        assert rows[0]["name"] == "Dropped"
        assert rows[0]["drop_pct"] == pytest.approx(25.0)
    finally:
        session.close()


def test_iter_all_drops_min_drop_pct_excludes_smaller_drops():
    session = SessionLocal()
    try:
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-12",
            _catalog([{"id": 1, "code": "c1", "name": "Small Drop", "price": 10.0, "tags": []}]),
        )
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-13",
            _catalog([{"id": 1, "code": "c1", "name": "Small Drop", "price": 9.0, "tags": []}]),
        )
        session.commit()

        rows = list(iter_all_drops(session, {SHOP_A: SHOP_A_LABEL}, min_drop_pct=50.0))
        assert rows == []
    finally:
        session.close()


def test_get_category_page_requires_a_category():
    session = SessionLocal()
    try:
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-13",
            _catalog([{"id": 1, "code": "c1", "name": "Item", "price": 1.0, "tags": []}]),
        )
        session.commit()

        rows, total = get_category_page(session, {SHOP_A: SHOP_A_LABEL}, None)
        assert (rows, total) == ([], 0)
    finally:
        session.close()


def test_get_category_page_matches_top_level_group_prefix():
    session = SessionLocal()
    try:
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-13",
            {
                "information": {"title": "T", "address": {"description": "A"}, "is_open": True},
                "menu": {
                    "categories": [
                        {
                            "name": "Τρόφιμα || Ζυμαρικά",
                            "items": [{"id": 1, "code": "c1", "name": "Pasta", "price": 1.0, "tags": []}],
                        },
                        {
                            "name": "Καθαριότητα || Απορρυπαντικά",
                            "items": [{"id": 2, "code": "c2", "name": "Soap", "price": 2.0, "tags": []}],
                        },
                    ]
                },
            },
        )
        session.commit()

        rows, total = get_category_page(session, {SHOP_A: SHOP_A_LABEL}, "Τρόφιμα")
        assert total == 1
        assert rows[0]["name"] == "Pasta"
    finally:
        session.close()


def test_get_basket_comparison_splits_across_cheapest_shops():
    session = SessionLocal()
    try:
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-13",
            _catalog(
                [
                    {"id": 1, "code": "c1", "name": "Milk A", "price": 1.5, "tags": []},
                    {"id": 2, "code": "c2", "name": "Bread A", "price": 2.0, "tags": []},
                ]
            ),
        )
        store_snapshot(
            session,
            SHOP_B,
            SHOP_B_LABEL,
            "2026-08-13",
            _catalog(
                [
                    {"id": 3, "code": "c3", "name": "Milk B", "price": 1.0, "tags": []},
                    {"id": 4, "code": "c4", "name": "Bread B", "price": 3.0, "tags": []},
                ]
            ),
        )
        session.commit()

        shops = {SHOP_A: SHOP_A_LABEL, SHOP_B: SHOP_B_LABEL}
        result = get_basket_comparison(session, shops, ["Milk", "Bread"])
        assert result["split"] is not None
        # Milk is cheaper at B (1.0), bread is cheaper at A (2.0) -- the
        # split should pick each line's own cheapest shop, not one shop
        # for everything.
        assert result["split"]["total"] == pytest.approx(3.0)
        assert result["split"]["shops_needed"] == 2

        shop_a_total = next(s for s in result["shop_totals"] if s["shop_id"] == SHOP_A)
        assert shop_a_total["total"] == pytest.approx(3.5)  # Milk A + Bread A
        assert shop_a_total["found_count"] == 2
    finally:
        session.close()


def test_get_basket_comparison_no_split_when_a_line_has_no_match():
    session = SessionLocal()
    try:
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-13",
            _catalog([{"id": 1, "code": "c1", "name": "Milk", "price": 1.5, "tags": []}]),
        )
        session.commit()

        result = get_basket_comparison(session, {SHOP_A: SHOP_A_LABEL}, ["Milk", "nothing matches this"])
        assert result["split"] is None
        assert result["lines"][1]["matches"] == {}
    finally:
        session.close()


def test_get_basket_comparison_ignores_zero_price_bug_rows():
    session = SessionLocal()
    try:
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-13",
            _catalog([{"id": 1, "code": "c1", "name": "Broken Item", "price": 0.0, "tags": []}]),
        )
        session.commit()

        result = get_basket_comparison(session, {SHOP_A: SHOP_A_LABEL}, ["Broken"])
        assert result["lines"][0]["matches"] == {}
    finally:
        session.close()


def _add_price_extreme(session, shop_id, code, name, category, current, lo, hi):
    from datetime import datetime, timezone

    from db import PriceExtreme

    session.add(
        PriceExtreme(
            shop_id=shop_id,
            code=code,
            name=name,
            category=category,
            current_price=current,
            min_price=lo,
            max_price=hi,
            swing_pct=(hi - lo) / hi * 100 if hi else 0.0,
            updated_at=datetime.now(timezone.utc),
        )
    )


def test_get_price_extremes_sorts_by_biggest_swing_first():
    session = SessionLocal()
    try:
        _add_price_extreme(session, SHOP_A, "c1", "Small Swing", "Cat", 9.0, 8.0, 10.0)
        _add_price_extreme(session, SHOP_A, "c2", "Big Swing", "Cat", 3.0, 2.0, 10.0)
        session.commit()

        rows, total = get_price_extremes(session, {SHOP_A: SHOP_A_LABEL})
        assert total == 2
        assert rows[0]["name"] == "Big Swing"
        assert rows[0]["swing_pct"] == pytest.approx(80.0)
    finally:
        session.close()


def test_get_price_extremes_filters_by_shop_and_min_swing():
    session = SessionLocal()
    try:
        _add_price_extreme(session, SHOP_A, "c1", "In Shop A", "Cat", 3.0, 2.0, 10.0)
        _add_price_extreme(session, SHOP_B, "c2", "In Shop B", "Cat", 9.0, 8.0, 10.0)
        session.commit()

        shops = {SHOP_A: SHOP_A_LABEL, SHOP_B: SHOP_B_LABEL}
        rows, total = get_price_extremes(session, shops, shop_id=SHOP_A)
        assert total == 1
        assert rows[0]["name"] == "In Shop A"

        rows, total = get_price_extremes(
            session, {SHOP_A: SHOP_A_LABEL, SHOP_B: SHOP_B_LABEL}, min_swing_pct=50.0
        )
        assert total == 1
        assert rows[0]["name"] == "In Shop A"
    finally:
        session.close()


def test_get_category_trend_returns_oldest_to_newest():
    from db import CategoryDailySummary

    session = SessionLocal()
    try:
        session.add_all(
            [
                CategoryDailySummary(
                    category="Τρόφιμα",
                    snapshot_date="2026-08-12",
                    avg_price=2.0,
                    item_count=5,
                    bug_count=0,
                ),
                CategoryDailySummary(
                    category="Τρόφιμα",
                    snapshot_date="2026-08-13",
                    avg_price=3.0,
                    item_count=6,
                    bug_count=1,
                ),
                CategoryDailySummary(
                    category="Άλλη Κατηγορία",
                    snapshot_date="2026-08-13",
                    avg_price=99.0,
                    item_count=1,
                    bug_count=0,
                ),
            ]
        )
        session.commit()

        rows = get_category_trend(session, "Τρόφιμα")
        assert [r[0] for r in rows] == ["2026-08-12", "2026-08-13"]  # oldest first
        assert [r[1] for r in rows] == [2.0, 3.0]
    finally:
        session.close()


def test_get_category_trend_unknown_category_is_empty():
    session = SessionLocal()
    try:
        assert get_category_trend(session, "Δεν υπάρχει") == []
    finally:
        session.close()


def test_get_new_verified_deals_counts_first_ever_snapshot_as_new():
    session = SessionLocal()
    try:
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-13",
            _catalog(
                [
                    {
                        "id": 1,
                        "code": "c1",
                        "name": "Fresh Deal",
                        "price": 3.0,
                        "full_price": 6.0,
                        "size_info": "3kg",
                        "tags": ["l30d:5.0"],
                    },
                    # Pricier same-category peers so this also clears the
                    # category-competitiveness gate (see
                    # queries.get_category_unit_price_medians), not just
                    # the plain discount-pct rule.
                    _item(2, "Peer Item A", 3.0, size_info="500g"),
                    _item(3, "Peer Item B", 6.0, size_info="1kg"),
                ]
            ),
        )
        session.commit()

        deals = get_new_verified_deals(session, {SHOP_A: SHOP_A_LABEL})
        assert [d["name"] for d in deals] == ["Fresh Deal"]
    finally:
        session.close()


def test_get_new_verified_deals_excludes_deals_still_running_from_yesterday():
    session = SessionLocal()
    try:
        item = {
            "id": 1,
            "code": "c1",
            "name": "Ongoing Deal",
            "price": 3.0,
            "full_price": 6.0,
            "tags": ["l30d:5.0"],
        }
        store_snapshot(session, SHOP_A, SHOP_A_LABEL, "2026-08-12", _catalog([item]))
        store_snapshot(session, SHOP_A, SHOP_A_LABEL, "2026-08-13", _catalog([item]))
        session.commit()

        deals = get_new_verified_deals(session, {SHOP_A: SHOP_A_LABEL})
        assert deals == []
    finally:
        session.close()


def test_get_new_verified_deals_includes_a_deal_that_just_started_today():
    session = SessionLocal()
    try:
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-12",
            _catalog([{"id": 1, "code": "c1", "name": "Full Price Item", "price": 6.0, "tags": []}]),
        )
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-13",
            _catalog(
                [
                    {
                        "id": 1,
                        "code": "c1",
                        "name": "Full Price Item",
                        "price": 3.0,
                        "full_price": 6.0,
                        "size_info": "3kg",
                        "tags": ["l30d:5.0"],
                    },
                    # Pricier same-category peers so this also clears the
                    # category-competitiveness gate (see
                    # queries.get_category_unit_price_medians), not just
                    # the plain discount-pct rule.
                    _item(2, "Peer Item A", 3.0, size_info="500g"),
                    _item(3, "Peer Item B", 6.0, size_info="1kg"),
                ]
            ),
        )
        session.commit()

        deals = get_new_verified_deals(session, {SHOP_A: SHOP_A_LABEL})
        assert [d["name"] for d in deals] == ["Full Price Item"]
    finally:
        session.close()


def test_get_category_page_optional_q_narrows_further():
    session = SessionLocal()
    try:
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-13",
            _catalog(
                [
                    {"id": 1, "code": "c1", "name": "Milk", "price": 1.0, "tags": []},
                    {"id": 2, "code": "c2", "name": "Bread", "price": 2.0, "tags": []},
                ]
            ),
        )
        session.commit()

        rows, total = get_category_page(session, {SHOP_A: SHOP_A_LABEL}, "Cat", q="Milk")
        assert total == 1
        assert rows[0]["name"] == "Milk"
    finally:
        session.close()


def test_get_cheapest_unit_price_by_product_finds_cheapest_across_shops():
    # The scenario the feature exists for: a matched product carried by
    # two shops at different package prices -- the caller needs the
    # per-unit cheapest, not just the per-listing cheapest, to catch a
    # "big discount, still not the best per-litre/per-kilo price" case.
    session = SessionLocal()
    try:
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-13",
            _catalog([_item(1, "Παγωτό Βανίλια 1kg", 6.0, size_info="1kg")]),
        )
        store_snapshot(
            session,
            SHOP_B,
            SHOP_B_LABEL,
            "2026-08-13",
            _catalog([_item(2, "Βανίλια Παγωτό 1kg", 3.0, size_info="1kg")]),
        )
        session.commit()

        from db import ItemPrice

        a1 = session.query(ItemPrice).filter_by(code="code-1").one()
        b1 = session.query(ItemPrice).filter_by(code="code-2").one()
        assert a1.product_id == b1.product_id  # sanity: fuzzy matcher merged them

        shop_labels = {SHOP_A: SHOP_A_LABEL, SHOP_B: SHOP_B_LABEL}
        result = get_cheapest_unit_price_by_product(session, [a1.product_id], shop_labels)
        assert result[a1.product_id]["shop_id"] == SHOP_B
        assert result[a1.product_id]["code"] == "code-2"
        assert result[a1.product_id]["unit_price"] == pytest.approx(0.3)
    finally:
        session.close()


def test_get_cheapest_unit_price_by_product_empty_ids_returns_empty():
    session = SessionLocal()
    try:
        assert get_cheapest_unit_price_by_product(session, [], {SHOP_A: SHOP_A_LABEL}) == {}
        assert get_cheapest_unit_price_by_product(session, [None], {SHOP_A: SHOP_A_LABEL}) == {}
    finally:
        session.close()


def test_get_cheapest_unit_price_by_product_omits_products_with_no_unit_price():
    # A listing with no size_info and no metric_unit_description has no
    # derivable unit_price -- it must not show up in the result at all
    # (not even with a None value), since callers treat a missing key as
    # "nothing to compare against".
    session = SessionLocal()
    try:
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-13",
            _catalog([_item(1, "Ασυσχέτιστο Προϊόν", 4.0)]),
        )
        session.commit()

        from db import ItemPrice

        a1 = session.query(ItemPrice).filter_by(code="code-1").one()
        result = get_cheapest_unit_price_by_product(session, [a1.product_id], {SHOP_A: SHOP_A_LABEL})
        assert result == {}
    finally:
        session.close()


def test_get_category_unit_price_medians_computes_per_kind_median():
    session = SessionLocal()
    try:
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-13",
            _catalog(
                [
                    _item(1, "Item A", 1.0, size_info="1kg"),  # 0.10 €/100g
                    _item(2, "Item B", 2.0, size_info="1kg"),  # 0.20 €/100g
                    _item(3, "Item C", 3.0, size_info="1kg"),  # 0.30 €/100g
                    _item(4, "Item D", 4.0, size_info="1kg"),  # 0.40 €/100g
                ]
            ),
        )
        session.commit()

        medians = get_category_unit_price_medians(session, {"Cat"}, {SHOP_A: SHOP_A_LABEL})
        assert medians[("Cat", "100g")] == pytest.approx(0.25)  # (0.20 + 0.30) / 2
    finally:
        session.close()


def test_get_category_unit_price_medians_omits_bucket_below_min_sample():
    # Only two priced, comparable items in the category -- too few to
    # trust a median against (see queries.MIN_CATEGORY_SAMPLE).
    session = SessionLocal()
    try:
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-13",
            _catalog(
                [
                    _item(1, "Item A", 1.0, size_info="1kg"),
                    _item(2, "Item B", 2.0, size_info="1kg"),
                ]
            ),
        )
        session.commit()

        medians = get_category_unit_price_medians(session, {"Cat"}, {SHOP_A: SHOP_A_LABEL})
        assert medians == {}
    finally:
        session.close()


def test_get_category_unit_price_medians_keeps_unit_kinds_separate():
    # A €/100g bucket and a €/100ml bucket in the same category must not
    # be pooled into one median -- the scales aren't interchangeable.
    session = SessionLocal()
    try:
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-13",
            _catalog(
                [
                    _item(1, "Weight A", 1.0, size_info="1kg"),  # 0.10 €/100g
                    _item(2, "Weight B", 2.0, size_info="1kg"),  # 0.20 €/100g
                    _item(3, "Weight C", 3.0, size_info="1kg"),  # 0.30 €/100g
                    _item(4, "Volume A", 9.0, size_info="1l"),  # 0.90 €/100ml
                    _item(5, "Volume B", 10.0, size_info="1l"),  # 1.00 €/100ml
                    _item(6, "Volume C", 11.0, size_info="1l"),  # 1.10 €/100ml
                ]
            ),
        )
        session.commit()

        medians = get_category_unit_price_medians(session, {"Cat"}, {SHOP_A: SHOP_A_LABEL})
        assert medians[("Cat", "100g")] == pytest.approx(0.20)
        assert medians[("Cat", "100ml")] == pytest.approx(1.00)
    finally:
        session.close()


def test_is_category_competitive_true_at_or_below_median():
    medians = {("Cat", "100g"): 0.30}
    assert is_category_competitive(3.0, "Cat", "1kg", None, medians) is True  # 0.30 €/100g, ties at median


def test_is_category_competitive_false_above_median():
    medians = {("Cat", "100g"): 0.30}
    assert is_category_competitive(5.0, "Cat", "1kg", None, medians) is False  # 0.50 €/100g


def test_is_category_competitive_false_without_a_benchmark():
    # No entry at all for this (category, kind) -- an unverifiable claim
    # of cheapness must not default to "competitive".
    assert is_category_competitive(3.0, "Cat", "1kg", None, {}) is False


def test_is_category_competitive_false_with_no_parseable_unit_price():
    medians = {("Cat", "100g"): 0.30}
    assert is_category_competitive(3.0, "Cat", None, None, medians) is False


def test_get_deals_page_excludes_deal_not_cheap_for_its_category():
    # The exact scenario a user reported live: an ice cream discounted
    # deeply off its own history, but still pricier per unit than
    # ordinary-priced peers in the same category -- it must not surface
    # as a "good deal" just because the percentage math checks out.
    session = SessionLocal()
    try:
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-13",
            _catalog(
                [
                    # Real 37.5% discount (8.0 -> 5.0), but 0.50 €/100g is
                    # still the priciest of the category's four listings.
                    _item(1, "Ice Cream Deal", 5.0, full_price=16.0, size_info="1kg", tags=["l30d:8.0"]),
                    _item(2, "Peer A", 1.0, size_info="1kg"),  # 0.10 €/100g
                    _item(3, "Peer B", 2.0, size_info="1kg"),  # 0.20 €/100g
                    _item(4, "Peer C", 3.0, size_info="1kg"),  # 0.30 €/100g
                ]
            ),
        )
        session.commit()

        rows, total = get_deals_page(session, {SHOP_A: SHOP_A_LABEL})
        assert total == 0
        assert rows == []
    finally:
        session.close()


def test_get_deals_page_includes_deal_that_is_cheap_for_its_category():
    session = SessionLocal()
    try:
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-13",
            _catalog(
                [
                    # Same 37.5% discount, but 0.10 €/100g is now the
                    # CHEAPEST of the category's four listings.
                    _item(1, "Ice Cream Deal", 1.0, full_price=3.2, size_info="1kg", tags=["l30d:1.6"]),
                    _item(2, "Peer A", 3.0, size_info="1kg"),  # 0.30 €/100g
                    _item(3, "Peer B", 4.0, size_info="1kg"),  # 0.40 €/100g
                    _item(4, "Peer C", 5.0, size_info="1kg"),  # 0.50 €/100g
                ]
            ),
        )
        session.commit()

        rows, total = get_deals_page(session, {SHOP_A: SHOP_A_LABEL})
        assert total == 1
        assert rows[0]["name"] == "Ice Cream Deal"
    finally:
        session.close()
