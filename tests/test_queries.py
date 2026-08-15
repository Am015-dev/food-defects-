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
    get_category_page,
    get_new_verified_deals,
    get_price_drops,
    get_product_across_shops,
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
                        "tags": ["l30d:5.0"],
                    }
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
                        "tags": ["l30d:5.0"],
                    }
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
