"""Tests for retention.prune_old_item_prices -- a destructive, bulk-DELETE
function auto-invoked after every ingest run, so a cutoff-boundary bug
here would silently delete live, irreplaceable price history in
production with nothing to catch it beforehand."""

from datetime import datetime, timedelta, timezone

from db import ItemPrice, SessionLocal, Snapshot
from ingest import store_snapshot
from retention import prune_old_item_prices
from shops import SHOPS

SHOP_A = SHOPS[0]["id"]
SHOP_A_LABEL = SHOPS[0]["label"]

RETENTION_DAYS = 90


def _catalog(items):
    return {
        "information": {"title": "T", "address": {"description": "A"}, "is_open": True},
        "menu": {"categories": [{"name": "Cat", "items": items}]},
    }


def _seed_snapshot_on(session, date_str):
    store_snapshot(
        session,
        SHOP_A,
        SHOP_A_LABEL,
        date_str,
        _catalog([{"id": 1, "code": "c1", "name": "Item", "price": 1.0, "tags": []}]),
    )


def _date_offset(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")


def test_prune_deletes_item_prices_older_than_retention_days():
    session = SessionLocal()
    try:
        _seed_snapshot_on(session, _date_offset(RETENTION_DAYS + 10))
        session.commit()
    finally:
        session.close()

    deleted = prune_old_item_prices(retention_days=RETENTION_DAYS)
    assert deleted == 1

    session = SessionLocal()
    try:
        assert session.query(ItemPrice).count() == 0
    finally:
        session.close()


def test_prune_keeps_recent_item_prices():
    session = SessionLocal()
    try:
        _seed_snapshot_on(session, _date_offset(1))  # yesterday, well within retention
        session.commit()
    finally:
        session.close()

    deleted = prune_old_item_prices(retention_days=RETENTION_DAYS)
    assert deleted == 0

    session = SessionLocal()
    try:
        assert session.query(ItemPrice).count() == 1
    finally:
        session.close()


def test_prune_keeps_snapshot_summary_rows_even_when_pruned():
    # The whole point of the function: item-level rows are disposable,
    # the small per-day summary row is not (queries.get_trend depends on
    # it existing indefinitely).
    session = SessionLocal()
    try:
        _seed_snapshot_on(session, _date_offset(RETENTION_DAYS + 10))
        session.commit()
    finally:
        session.close()

    prune_old_item_prices(retention_days=RETENTION_DAYS)

    session = SessionLocal()
    try:
        assert session.query(Snapshot).count() == 1
        assert session.query(ItemPrice).count() == 0
    finally:
        session.close()


def test_prune_boundary_exactly_at_cutoff_is_not_pruned():
    # A snapshot dated exactly `today - retention_days` is not yet
    # strictly older than the cutoff -- an off-by-one here (< vs <=)
    # would delete data one day earlier than intended on every run.
    session = SessionLocal()
    try:
        _seed_snapshot_on(session, _date_offset(RETENTION_DAYS))
        session.commit()
    finally:
        session.close()

    deleted = prune_old_item_prices(retention_days=RETENTION_DAYS)
    assert deleted == 0


def test_prune_boundary_one_day_past_cutoff_is_pruned():
    session = SessionLocal()
    try:
        _seed_snapshot_on(session, _date_offset(RETENTION_DAYS + 1))
        session.commit()
    finally:
        session.close()

    deleted = prune_old_item_prices(retention_days=RETENTION_DAYS)
    assert deleted == 1


def test_prune_only_deletes_old_rows_when_mixed_with_recent():
    session = SessionLocal()
    try:
        _seed_snapshot_on(session, _date_offset(RETENTION_DAYS + 10))
        # A second shop's recent snapshot must survive pruning.
        store_snapshot(
            session,
            SHOPS[1]["id"],
            SHOPS[1]["label"],
            _date_offset(0),
            _catalog([{"id": 2, "code": "c2", "name": "Recent Item", "price": 2.0, "tags": []}]),
        )
        session.commit()
    finally:
        session.close()

    deleted = prune_old_item_prices(retention_days=RETENTION_DAYS)
    assert deleted == 1

    session = SessionLocal()
    try:
        remaining = session.query(ItemPrice).all()
        assert len(remaining) == 1
        assert remaining[0].name == "Recent Item"
    finally:
        session.close()
