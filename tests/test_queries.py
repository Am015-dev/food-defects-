"""Direct tests for queries.py functions that need database access,
bypassing the Flask route/template layer for precision on things like
exact pagination math and join semantics that a route-level test (which
mostly just checks page text) can't easily pin down."""

import pytest

from db import SessionLocal
from ingest import store_snapshot
from queries import compare_across_shops, get_price_drops, get_product_across_shops, search_products
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
