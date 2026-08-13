"""Direct tests for queries.py functions that need database access,
bypassing the Flask route/template layer for precision on things like
exact pagination math and join semantics that a route-level test (which
mostly just checks page text) can't easily pin down."""

import pytest

from db import SessionLocal
from ingest import store_snapshot
from queries import get_price_drops, search_products
from shops import SHOPS

SHOP_A = SHOPS[0]["id"]
SHOP_A_LABEL = SHOPS[0]["label"]


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
