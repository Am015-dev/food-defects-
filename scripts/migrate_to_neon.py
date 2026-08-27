"""One-off migration: copy every table from the source Postgres
(RENDER_DATABASE_URL) to a fresh target (NEON_DATABASE_URL), row for
row, in FK-safe order.

Deliberately NOT DATABASE_URL for the source: that secret's value
changes over the course of this migration (it starts out pointing at
Render and, after cutover, ends up pointing at Neon), so a script that
read it mid-migration could easily end up copying a database into
itself -- which is exactly what happened the first time this ran, when
DATABASE_URL had already been switched to Neon ahead of the actual
copy. RENDER_DATABASE_URL is a distinct, unambiguous secret that only
ever means "the old database," so this can't recur.

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

Also caps how much of item_prices it copies: both the 90-day and the
45-day attempt hit Neon's free-tier 512MB project limit partway
through that table (recent days are denser than the 90-day average --
the catalog has grown over time -- so 45 days wasn't actually half the
size). MIGRATE_ITEM_PRICES_DAYS is deliberately conservative as a
result; the run's own before/after pg_database_size print is what
should inform what retention.py's real steady-state window can be,
not a flat proportion of the original 90-day figure.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url

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
MIGRATE_ITEM_PRICES_DAYS = 14

# FK-safe order: a table only appears after every table it references,
# and reversed gives a safe wipe order (children before parents).
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


def _copy_table(table, source_conn, target_conn, extra_where=None):
    """Keyset-paginated copy (WHERE id > last_id), not OFFSET -- item_prices
    alone is several hundred MB, and OFFSET pagination gets quadratically
    slower as it grows. extra_where, when given, further restricts which
    rows are copied (see MIGRATE_ITEM_PRICES_DAYS)."""
    total = 0
    last_id = 0
    while True:
        stmt = select(table).where(table.c.id > last_id)
        if extra_where is not None:
            stmt = stmt.where(extra_where)
        stmt = stmt.order_by(table.c.id).limit(BATCH_SIZE)
        rows = source_conn.execute(stmt).mappings().all()
        if not rows:
            break
        target_conn.execute(table.insert(), [dict(row) for row in rows])
        total += len(rows)
        last_id = rows[-1]["id"]
    return total


def _count(conn, table, extra_where=None):
    stmt = select(func.count()).select_from(table)
    if extra_where is not None:
        stmt = stmt.where(extra_where)
    return conn.execute(stmt).scalar()


def _wipe_target(target_conn):
    """Deletes every row from every migrated table, children first (MODELS
    reversed). Makes a retry after a failed/partial run (e.g. hitting
    Neon's size cap mid-copy) safe to just run again, instead of hitting
    duplicate-key errors on rows that already landed."""
    for model in reversed(MODELS):
        target_conn.execute(model.__table__.delete())


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


def _pretty_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def _db_size(conn):
    return conn.execute(text("SELECT pg_database_size(current_database())")).scalar()


def _same_database(url_a, url_b):
    """Compares host+port+database only, not credentials -- two
    different users on the same database are still "the same database"
    for this check."""
    a, b = make_url(url_a), make_url(url_b)
    return (a.host, a.port, a.database) == (b.host, b.port, b.database)


def main():
    try:
        source_url = _normalize(os.environ["RENDER_DATABASE_URL"])
    except KeyError:
        print("RENDER_DATABASE_URL is not set -- nothing to migrate from.", file=sys.stderr)
        sys.exit(1)
    try:
        target_url = _normalize(os.environ["NEON_DATABASE_URL"])
    except KeyError:
        print("NEON_DATABASE_URL is not set -- nothing to migrate to.", file=sys.stderr)
        sys.exit(1)

    if _same_database(source_url, target_url):
        print(
            "RENDER_DATABASE_URL and NEON_DATABASE_URL point at the same "
            "database -- refusing to migrate a database into itself.",
            file=sys.stderr,
        )
        sys.exit(1)

    source_engine = create_engine(source_url, future=True)
    target_engine = create_engine(target_url, future=True)

    # Reuse db.py's own schema/index setup against the target, so it ends
    # up exactly as a fresh deploy would create it -- see db.init_db().
    db_module.engine = target_engine
    db_module.init_db()

    cutoff = (datetime.now(timezone.utc) - timedelta(days=MIGRATE_ITEM_PRICES_DAYS)).strftime("%Y-%m-%d")
    recent_snapshot_ids = select(Snapshot.__table__.c.id).where(Snapshot.__table__.c.snapshot_date >= cutoff)
    extra_where_by_model = {ItemPrice: ItemPrice.__table__.c.snapshot_id.in_(recent_snapshot_ids)}

    print(f"{'table':<28} {'copied':>10} {'source':>10} {'target':>10}")
    mismatched = False
    with source_engine.connect() as source_conn, target_engine.begin() as target_conn:
        _wipe_target(target_conn)
        for model in MODELS:
            table = model.__table__
            extra_where = extra_where_by_model.get(model)
            copied = _copy_table(table, source_conn, target_conn, extra_where)
            _reset_sequence(target_conn, table.name)
            source_count = _count(source_conn, table, extra_where)
            target_count = _count(target_conn, table, extra_where)
            status = "OK" if source_count == target_count else "MISMATCH"
            mismatched = mismatched or source_count != target_count
            note = f" (last {MIGRATE_ITEM_PRICES_DAYS}d only)" if model is ItemPrice else ""
            print(f"{table.name:<28} {copied:>10} {source_count:>10} {target_count:>10}  {status}{note}")

    with target_engine.connect() as conn:
        print(f"target database size after migration: {_pretty_bytes(_db_size(conn))}")

    if mismatched:
        print("row count mismatch on at least one table -- see above", file=sys.stderr)
        sys.exit(1)
    print("migration complete")


if __name__ == "__main__":
    main()
