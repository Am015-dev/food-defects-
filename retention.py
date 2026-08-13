"""Retention: drop old per-item price rows so the database stays within
Render's free-tier Postgres limit, while keeping the small per-day
Snapshot summary rows forever so the dashboard's trend chart
(queries.get_trend) keeps working indefinitely.

13 shops x ~7,500 items/day is roughly 90-100k ItemPrice rows/day --
at that rate the free 1GB tier fills up in a couple of months without
this running regularly.
"""

from datetime import datetime, timedelta, timezone

from db import ItemPrice, SessionLocal, Snapshot

RETENTION_DAYS = 90


def prune_old_item_prices(session=None, retention_days=RETENTION_DAYS):
    """Delete ItemPrice rows belonging to snapshots older than
    retention_days. Snapshot rows themselves are untouched -- a few dozen
    columns per shop per day, not a few thousand, and the dashboard's
    trend chart depends on them existing indefinitely.

    Returns the number of ItemPrice rows deleted.
    """
    owns_session = session is None
    session = session or SessionLocal()
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).strftime("%Y-%m-%d")
        old_snapshot_ids = [
            sid for (sid,) in session.query(Snapshot.id).filter(Snapshot.snapshot_date < cutoff)
        ]
        if not old_snapshot_ids:
            return 0
        deleted = (
            session.query(ItemPrice)
            .filter(ItemPrice.snapshot_id.in_(old_snapshot_ids))
            .delete(synchronize_session=False)
        )
        session.commit()
        return deleted
    finally:
        if owns_session:
            session.close()


def main():
    deleted = prune_old_item_prices()
    print(f"retention: deleted {deleted} item_prices row(s) older than {RETENTION_DAYS} days")


if __name__ == "__main__":
    main()
