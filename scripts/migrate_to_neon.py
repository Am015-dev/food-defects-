"""One-off migration: copy every table from the source Postgres
(DATABASE_URL, Render's free-tier database) to a fresh target
(NEON_DATABASE_URL), row for row, in FK-safe order.

Why not pg_dump/pg_restore: this database is disk-constrained enough
that a consistent dump isn't reliable here (see
scripts/reclaim_space.py -- VACUUM FULL itself fails with DiskFull),
and daily-ingest.yml's own pg_dump backup step is separately broken by
a client/server version mismatch. A plain SQLAlchemy copy sidesteps
both and needs nothing beyond what db.py already describes.

Meant to be run once, manually, from a GitHub Actions workflow_dispatch
(see .github/workflows/migrate-to-neon.yml) -- not part of the regular
pipeline. See the "Migrate off Render's free Postgres" plan for the
full picture.
"""

import os
import sys

from sqlalchemy import create_engine, func, select, text

import db as db_module
from db import (
    CategoryDailySummary,
    CategoryUnitPriceMedian,
    ItemPrice,
    PriceExtreme,
    Product,
    ProductListing,
    Shop,
    Snapshot,
)

BATCH_SIZE = 5000

# FK-safe order: a table only appears after every table it references.
MODELS = [
    Shop,
    Product,
    Snapshot,
    ProductListing,
    ItemPrice,
    PriceExtreme,
    CategoryDailySummary,
    CategoryUnitPriceMedian,
]


def _normalize(url):
    if url and url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql://", 1)
    return url


def _copy_table(table, source_conn, target_conn):
    """Keyset-paginated copy (WHERE id > last_id), not OFFSET -- item_prices
    alone is several hundred MB, and OFFSET pagination gets quadratically
    slower as it grows."""
    total = 0
    last_id = 0
    while True:
        rows = (
            source_conn.execute(
                select(table).where(table.c.id > last_id).order_by(table.c.id).limit(BATCH_SIZE)
            )
            .mappings()
            .all()
        )
        if not rows:
            break
        target_conn.execute(table.insert(), [dict(row) for row in rows])
        total += len(rows)
        last_id = rows[-1]["id"]
    return total


def _reset_sequence(conn, table_name):
    """Explicit-id inserts don't advance the target's own id sequence, so
    the next ordinary ORM insert (e.g. tomorrow's ingest) would try to
    reuse id=1 and hit a duplicate-key error. table_name always comes from
    MODELS above, never external input, so direct interpolation is safe."""
    conn.execute(
        text(
            f"SELECT setval(pg_get_serial_sequence('{table_name}', 'id'), "
            f"COALESCE((SELECT MAX(id) FROM {table_name}), 1))"
        )
    )


def main():
    source_engine = db_module.engine  # already built from DATABASE_URL at import time
    try:
        target_url = _normalize(os.environ["NEON_DATABASE_URL"])
    except KeyError:
        print("NEON_DATABASE_URL is not set -- nothing to migrate to.", file=sys.stderr)
        sys.exit(1)
    target_engine = create_engine(target_url, future=True)

    # Reuse db.py's own schema/index setup against the target, so it ends
    # up exactly as a fresh deploy would create it -- see db.init_db().
    db_module.engine = target_engine
    db_module.init_db()

    print(f"{'table':<28} {'copied':>10} {'source':>10} {'target':>10}")
    mismatched = False
    with source_engine.connect() as source_conn, target_engine.begin() as target_conn:
        for model in MODELS:
            table = model.__table__
            copied = _copy_table(table, source_conn, target_conn)
            _reset_sequence(target_conn, table.name)
            source_count = source_conn.execute(select(func.count()).select_from(table)).scalar()
            target_count = target_conn.execute(select(func.count()).select_from(table)).scalar()
            status = "OK" if source_count == target_count else "MISMATCH"
            mismatched = mismatched or source_count != target_count
            print(f"{table.name:<28} {copied:>10} {source_count:>10} {target_count:>10}  {status}")

    if mismatched:
        print("row count mismatch on at least one table -- see above", file=sys.stderr)
        sys.exit(1)
    print("migration complete")


if __name__ == "__main__":
    main()
