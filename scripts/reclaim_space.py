"""One-off emergency maintenance: Render emailed that food-defects-db is
over 90% of its free-tier storage. retention.prune_old_item_prices()
already runs at the start of every ingest and deletes rows past the
90-day retention window, but a plain DELETE in Postgres doesn't shrink
the table file on disk -- it just marks the space reusable for future
inserts within the same table. Only VACUUM FULL actually returns the
freed space to the filesystem.

By default this ONLY vacuums (no data loss at all -- every row still
inside the existing 90-day retention window is kept) to see how much of
the reported >90% usage is just unreclaimed dead space from ordinary
day-to-day deletes/updates, which is the common case and doesn't need
losing any history to fix.

Only if that isn't enough headroom does further pruning become a real
tradeoff (deleting more of the irreplaceable price history than the
normal 90-day window already does) -- that's a call for a human, not
this script, so it's opt-in via RECLAIM_PRUNE_DAYS rather than
automatic. Set that env var to a day count (e.g. 45) to prune to that
window before vacuuming, in the same run.

Meant to be run once, manually, from a GitHub Actions workflow_dispatch
(see .github/workflows/reclaim-space.yml) against the production
DATABASE_URL -- not part of the regular daily pipeline.
"""

import os

from sqlalchemy import text

from db import SessionLocal, engine
from retention import prune_old_item_prices

PRUNE_DAYS = os.environ.get("RECLAIM_PRUNE_DAYS")


def _pretty_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def _db_size(conn):
    return conn.execute(text("SELECT pg_database_size(current_database())")).scalar()


def _table_size(conn, table):
    return conn.execute(text("SELECT pg_total_relation_size(:t)"), {"t": table}).scalar()


def main():
    with engine.connect() as conn:
        before_db = _db_size(conn)
        before_table = _table_size(conn, "item_prices")
    print(f"before: database={_pretty_bytes(before_db)} item_prices={_pretty_bytes(before_table)}")

    if PRUNE_DAYS:
        session = SessionLocal()
        try:
            deleted = prune_old_item_prices(session, retention_days=int(PRUNE_DAYS))
        finally:
            session.close()
        print(f"pruned {deleted} item_prices row(s) older than {PRUNE_DAYS} days")
    else:
        print("RECLAIM_PRUNE_DAYS not set -- skipping extra pruning, vacuum only")

    # VACUUM can't run inside a transaction block; AUTOCOMMIT sidesteps that.
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        print("running VACUUM (FULL, ANALYZE) on item_prices -- this holds an exclusive lock...")
        conn.execute(text("VACUUM (FULL, ANALYZE) item_prices"))

    with engine.connect() as conn:
        after_db = _db_size(conn)
        after_table = _table_size(conn, "item_prices")
    print(f"after:  database={_pretty_bytes(after_db)} item_prices={_pretty_bytes(after_table)}")
    print(f"reclaimed: {_pretty_bytes(before_db - after_db)}")


if __name__ == "__main__":
    main()
