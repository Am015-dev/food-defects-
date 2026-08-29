"""Retention: drop old per-item price rows so the database stays within
the free-tier Postgres limit, while keeping the small per-day Snapshot
summary rows forever so the dashboard's trend chart (queries.get_trend)
keeps working indefinitely.

Sized for Neon's free tier (512MB per whole project, not just this
table): the 2026-08-29 migration copied only the most recent ~3.5 days
of item_prices (46 snapshots, 13 shops/day) and that alone used the
full 400MB budget -- roughly 114MB/day at this app's current row width
(unit_price, product_id, name_fold, etc. all populated and indexed).
3 days keeps item_prices under ~350MB, leaving real headroom for the
rest of the tables and future growth. (The old 90-day figure was sized
for Render's 1GB tier and, since this repo is only ~18 days old, had
never actually deleted a row in production -- see the 2026-08-29
MISTAKES.md entry.)
"""

from datetime import datetime, timedelta, timezone

from db import ItemPrice, SessionLocal, Snapshot

RETENTION_DAYS = 3


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
