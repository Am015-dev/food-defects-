"""Tests for ingest.update_price_extremes_rollup -- the nightly rollup
that pre-aggregates each currently-listed item's min/max price so the
web service never has to scan full item_prices history live."""

from db import PriceExtreme, SessionLocal
from ingest import store_snapshot, update_price_extremes_rollup
from shops import SHOPS

SHOP_A = SHOPS[0]["id"]
SHOP_A_LABEL = SHOPS[0]["label"]
SHOP_B = SHOPS[1]["id"]
SHOP_B_LABEL = SHOPS[1]["label"]


def _catalog(items):
    return {
        "information": {"title": "T", "address": {"description": "A"}, "is_open": True},
        "menu": {"categories": [{"name": "Τρόφιμα || Test", "items": items}]},
    }


def test_rollup_computes_min_max_swing_across_snapshots():
    session = SessionLocal()
    try:
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-11",
            _catalog([{"id": 1, "code": "c1", "name": "Swingy", "price": 5.0, "tags": []}]),
        )
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-12",
            _catalog([{"id": 1, "code": "c1", "name": "Swingy", "price": 2.0, "tags": []}]),
        )
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-13",
            _catalog([{"id": 1, "code": "c1", "name": "Swingy", "price": 3.0, "tags": []}]),
        )
        session.commit()
    finally:
        session.close()

    written = update_price_extremes_rollup()
    assert written == 1

    session = SessionLocal()
    try:
        row = session.query(PriceExtreme).filter_by(shop_id=SHOP_A, code="c1").one()
        assert row.current_price == 3.0
        assert row.min_price == 2.0
        assert row.max_price == 5.0
        assert row.swing_pct == 60.0  # (5 - 2) / 5 * 100
    finally:
        session.close()


def test_rollup_excludes_delisted_items_and_zero_price_rows():
    session = SessionLocal()
    try:
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-12",
            _catalog(
                [
                    {"id": 1, "code": "c1", "name": "Delisted Tomorrow", "price": 2.0, "tags": []},
                    {"id": 2, "code": "c2", "name": "Broken", "price": 0.0, "tags": []},
                ]
            ),
        )
        # Today's snapshot no longer carries c1 -- it's been delisted.
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-13",
            _catalog([{"id": 3, "code": "c3", "name": "Still Listed", "price": 1.0, "tags": []}]),
        )
        session.commit()
    finally:
        session.close()

    update_price_extremes_rollup()

    session = SessionLocal()
    try:
        codes = {r.code for r in session.query(PriceExtreme).filter_by(shop_id=SHOP_A)}
        assert codes == {"c3"}
    finally:
        session.close()


def test_rollup_is_idempotent_and_replaces_stale_rows():
    session = SessionLocal()
    try:
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-13",
            _catalog([{"id": 1, "code": "c1", "name": "Item", "price": 4.0, "tags": []}]),
        )
        session.commit()
    finally:
        session.close()

    update_price_extremes_rollup()
    update_price_extremes_rollup()  # rerun must not duplicate the row

    session = SessionLocal()
    try:
        assert session.query(PriceExtreme).filter_by(shop_id=SHOP_A, code="c1").count() == 1
    finally:
        session.close()


def test_rollup_covers_every_shop_independently():
    session = SessionLocal()
    try:
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-13",
            _catalog([{"id": 1, "code": "c1", "name": "Shop A Item", "price": 1.0, "tags": []}]),
        )
        store_snapshot(
            session,
            SHOP_B,
            SHOP_B_LABEL,
            "2026-08-13",
            _catalog([{"id": 2, "code": "c2", "name": "Shop B Item", "price": 2.0, "tags": []}]),
        )
        session.commit()
    finally:
        session.close()

    written = update_price_extremes_rollup()
    assert written == 2

    session = SessionLocal()
    try:
        shops = {r.shop_id for r in session.query(PriceExtreme)}
        assert shops == {SHOP_A, SHOP_B}
    finally:
        session.close()
