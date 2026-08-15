"""Tests for ingest.update_price_extremes_rollup and
update_category_daily_summary -- the nightly rollups that pre-aggregate
data the web service would otherwise have to scan full item_prices
history live to compute."""

import pytest

from db import CategoryDailySummary, CategoryUnitPriceMedian, PriceExtreme, SessionLocal
from ingest import (
    store_snapshot,
    update_category_daily_summary,
    update_category_unit_price_medians,
    update_price_extremes_rollup,
)
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


def _catalog_with_category(category, items):
    return {
        "information": {"title": "T", "address": {"description": "A"}, "is_open": True},
        "menu": {"categories": [{"name": category, "items": items}]},
    }


def test_category_summary_folds_subcategories_and_averages_price():
    session = SessionLocal()
    try:
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-13",
            _catalog_with_category(
                "Τρόφιμα || Ζυμαρικά",
                [{"id": 1, "code": "c1", "name": "Pasta", "price": 2.0, "tags": []}],
            ),
        )
        store_snapshot(
            session,
            SHOP_B,
            SHOP_B_LABEL,
            "2026-08-13",
            _catalog_with_category(
                "Τρόφιμα || Κρέας",  # different subcategory, same top-level group
                [{"id": 2, "code": "c2", "name": "Meat", "price": 6.0, "tags": []}],
            ),
        )
        session.commit()
    finally:
        session.close()

    written = update_category_daily_summary(today="2026-08-13")
    assert written == 1  # both fold into one "Τρόφιμα" row

    session = SessionLocal()
    try:
        row = session.query(CategoryDailySummary).filter_by(category="Τρόφιμα").one()
        assert row.item_count == 2
        assert row.avg_price == 4.0  # mean of 2.0 and 6.0
        assert row.bug_count == 0
    finally:
        session.close()


def test_category_summary_counts_bugs_and_excludes_zero_price_from_average():
    session = SessionLocal()
    try:
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-13",
            _catalog_with_category(
                "Καθαριότητα || Test",
                [
                    {"id": 1, "code": "c1", "name": "Broken", "price": 0.0, "tags": []},
                    {"id": 2, "code": "c2", "name": "Fine", "price": 5.0, "tags": []},
                ],
            ),
        )
        session.commit()
    finally:
        session.close()

    update_category_daily_summary(today="2026-08-13")

    session = SessionLocal()
    try:
        row = session.query(CategoryDailySummary).filter_by(category="Καθαριότητα").one()
        assert row.item_count == 2
        assert row.bug_count == 1  # the zero-price bug
        assert row.avg_price == 5.0  # only the real price counts
    finally:
        session.close()


def test_category_summary_reruns_for_same_day_replace_not_duplicate():
    session = SessionLocal()
    try:
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-13",
            _catalog_with_category(
                "Τρόφιμα || Test", [{"id": 1, "code": "c1", "name": "Item", "price": 2.0, "tags": []}]
            ),
        )
        session.commit()
    finally:
        session.close()

    update_category_daily_summary(today="2026-08-13")
    update_category_daily_summary(today="2026-08-13")

    session = SessionLocal()
    try:
        assert session.query(CategoryDailySummary).filter_by(category="Τρόφιμα").count() == 1
    finally:
        session.close()


def test_category_summary_keeps_separate_rows_per_day():
    session = SessionLocal()
    try:
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-12",
            _catalog_with_category(
                "Τρόφιμα || Test", [{"id": 1, "code": "c1", "name": "Item", "price": 2.0, "tags": []}]
            ),
        )
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-13",
            _catalog_with_category(
                "Τρόφιμα || Test", [{"id": 1, "code": "c1", "name": "Item", "price": 3.0, "tags": []}]
            ),
        )
        session.commit()
    finally:
        session.close()

    update_category_daily_summary(today="2026-08-12")
    update_category_daily_summary(today="2026-08-13")

    session = SessionLocal()
    try:
        rows = session.query(CategoryDailySummary).filter_by(category="Τρόφιμα").order_by(
            CategoryDailySummary.snapshot_date
        )
        prices = [r.avg_price for r in rows]
        assert prices == [2.0, 3.0]
    finally:
        session.close()


def test_category_summary_no_snapshot_for_day_returns_zero():
    written = update_category_daily_summary(today="2099-01-01")
    assert written == 0


def _item(id_, name, price, **extra):
    d = {"id": id_, "code": f"code-{id_}", "name": name, "price": price, "tags": []}
    d.update(extra)
    return d


def _multi_catalog(categories):
    """categories: {category_name: [items]}"""
    return {
        "information": {"title": "T", "address": {"description": "A"}, "is_open": True},
        "menu": {"categories": [{"name": name, "items": items} for name, items in categories.items()]},
    }


def test_category_unit_price_medians_computes_per_kind_median():
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
                ]
            ),
        )
        session.commit()
    finally:
        session.close()

    written = update_category_unit_price_medians()
    assert written == 1

    session = SessionLocal()
    try:
        row = session.query(CategoryUnitPriceMedian).filter_by(category="Τρόφιμα || Test").one()
        assert row.unit_kind == "100g"
        assert row.median_unit_price == pytest.approx(0.20)
        assert row.sample_count == 3
    finally:
        session.close()


def test_category_unit_price_medians_keeps_kinds_separate():
    session = SessionLocal()
    try:
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-13",
            _catalog(
                [
                    _item(1, "Weight A", 1.0, size_info="1kg"),
                    _item(2, "Weight B", 2.0, size_info="1kg"),
                    _item(3, "Weight C", 3.0, size_info="1kg"),
                    _item(4, "Volume A", 9.0, size_info="1l"),
                    _item(5, "Volume B", 10.0, size_info="1l"),
                    _item(6, "Volume C", 11.0, size_info="1l"),
                ]
            ),
        )
        session.commit()
    finally:
        session.close()

    written = update_category_unit_price_medians()
    assert written == 2

    session = SessionLocal()
    try:
        rows = {
            r.unit_kind: r.median_unit_price
            for r in session.query(CategoryUnitPriceMedian).filter_by(category="Τρόφιμα || Test")
        }
        assert rows["100g"] == pytest.approx(0.20)
        assert rows["100ml"] == pytest.approx(1.00)
    finally:
        session.close()


def test_category_unit_price_medians_omits_bucket_below_min_sample():
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
    finally:
        session.close()

    written = update_category_unit_price_medians()
    assert written == 0


def test_category_unit_price_medians_covers_every_tracked_shop():
    # The benchmark is "cheap for its type across the whole market", not
    # just one shop -- items from two different shops in the same
    # category must land in the same bucket.
    session = SessionLocal()
    try:
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-13",
            _catalog([_item(1, "A1", 1.0, size_info="1kg"), _item(2, "A2", 2.0, size_info="1kg")]),
        )
        store_snapshot(
            session,
            SHOP_B,
            SHOP_B_LABEL,
            "2026-08-13",
            _catalog([_item(3, "B1", 3.0, size_info="1kg")]),
        )
        session.commit()
    finally:
        session.close()

    written = update_category_unit_price_medians()
    assert written == 1

    session = SessionLocal()
    try:
        row = session.query(CategoryUnitPriceMedian).filter_by(category="Τρόφιμα || Test").one()
        assert row.sample_count == 3
        assert row.median_unit_price == pytest.approx(0.20)
    finally:
        session.close()


def test_category_unit_price_medians_reruns_replace_not_duplicate():
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
                    _item(3, "Item C", 3.0, size_info="1kg"),
                ]
            ),
        )
        session.commit()
    finally:
        session.close()

    update_category_unit_price_medians()
    update_category_unit_price_medians()  # rerun must not duplicate the row

    session = SessionLocal()
    try:
        assert session.query(CategoryUnitPriceMedian).filter_by(category="Τρόφιμα || Test").count() == 1
    finally:
        session.close()


def test_category_unit_price_medians_ignores_unparseable_listings():
    # No size_info and no metric_unit_description -- derive_unit_price
    # returns None for these, so they can't contribute to any bucket.
    session = SessionLocal()
    try:
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-13",
            _catalog([_item(1, "No Size", 1.0), _item(2, "Also No Size", 2.0)]),
        )
        session.commit()
    finally:
        session.close()

    written = update_category_unit_price_medians()
    assert written == 0


def test_category_unit_price_medians_multiple_categories_scored_independently():
    session = SessionLocal()
    try:
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-13",
            _multi_catalog(
                {
                    "Cat A": [
                        _item(1, "A1", 1.0, size_info="1kg"),
                        _item(2, "A2", 2.0, size_info="1kg"),
                        _item(3, "A3", 3.0, size_info="1kg"),
                    ],
                    "Cat B": [
                        _item(4, "B1", 10.0, size_info="1kg"),
                        _item(5, "B2", 20.0, size_info="1kg"),
                        _item(6, "B3", 30.0, size_info="1kg"),
                    ],
                }
            ),
        )
        session.commit()
    finally:
        session.close()

    written = update_category_unit_price_medians()
    assert written == 2

    session = SessionLocal()
    try:
        rows = {r.category: r.median_unit_price for r in session.query(CategoryUnitPriceMedian)}
        assert rows["Cat A"] == pytest.approx(0.20)
        assert rows["Cat B"] == pytest.approx(2.00)
    finally:
        session.close()
