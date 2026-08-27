"""One-off emergency maintenance: Render emailed that food-defects-db is
over 90% of its free-tier storage. retention.prune_old_item_prices()
already runs at the start of every ingest and deletes rows past the
retention window, but a plain DELETE in Postgres doesn't shrink the
table file on disk -- it just marks the space reusable for future
inserts within the same table. Only VACUUM FULL actually returns the
freed space to the filesystem.

This script (a) prunes down to a tighter interim retention window --
independent of retention.RETENTION_DAYS, so it isn't waiting on that
constant's own change to land -- and (b) VACUUM FULLs item_prices (by
far the largest table) to reclaim the space immediately, printing
before/after sizes so the effect is visible in the run's own log.

Meant to be run once, manually, from a GitHub Actions workflow_dispatch
(see .github/workflows/reclaim-space.yml) against the production
DATABASE_URL -- not part of the regular daily pipeline.
"""

from sqlalchemy import text

from db import SessionLocal, engine
from retention import prune_old_item_prices

# Tighter than the current RETENTION_DAYS=90 (or the planned 45) on
# purpose: this is a one-time emergency reclaim, not the new steady
# state -- better to free real headroom immediately than trim exactly
# to the eventual target.
EMERGENCY_RETENTION_DAYS = 30


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

    session = SessionLocal()
    try:
        deleted = prune_old_item_prices(session, retention_days=EMERGENCY_RETENTION_DAYS)
    finally:
        session.close()
    print(f"pruned {deleted} item_prices row(s) older than {EMERGENCY_RETENTION_DAYS} days")

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
