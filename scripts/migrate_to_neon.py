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

item_prices needs special handling: Neon's free tier caps a whole
project at 512MB, and this repo is only ~2 weeks old (first commit
2026-08-11), so despite RETENTION_DAYS=90 there's no 90 days of real
history to trim -- almost everything already falls inside any
reasonable day-based cutoff, which is why both a 45-day and a 14-day
attempt hit the exact same wall at the exact same row. Instead of
guessing a day count, item_prices is copied most-recent-snapshot-first,
checking the target's actual on-disk size before each snapshot and
stopping once ITEM_PRICES_SIZE_BUDGET_BYTES is reached -- so however
much real history exists, this always keeps as much of the most recent
of it as actually fits, without needing to know the true date range
up front or guess again if the catalog's density changes over time.

Meant to be run once, manually, from a GitHub Actions workflow_dispatch
(see .github/workflows/migrate-to-neon.yml) -- not part of the regular
pipeline. See the "Migrate off Render's free Postgres" plan for the
full picture.
"""

import os
import sys

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
# Leaves headroom under Neon's free-tier 512MB hard cap for whatever
# Postgres/Neon overhead isn't reflected in pg_database_size at the
# instant it's checked (WAL not yet flushed, in-flight index pages).
ITEM_PRICES_SIZE_BUDGET_BYTES = 400 * 1024 * 1024

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


def _copy_table(table, source_conn, target_conn):
    """Keyset-paginated copy (WHERE id > last_id), not OFFSET -- some of
    these tables are tens of thousands of rows, and OFFSET pagination
    gets quadratically slower as it grows."""
    total = 0
    last_id = 0
    while True:
        stmt = select(table).where(table.c.id > last_id).order_by(table.c.id).limit(BATCH_SIZE)
        rows = source_conn.execute(stmt).mappings().all()
        if not rows:
            break
        target_conn.execute(table.insert(), [dict(row) for row in rows])
        total += len(rows)
        last_id = rows[-1]["id"]
    return total


def _recent_snapshot_ids_desc(source_conn):
    """Every Snapshot id, most recent first (by snapshot_date, then id as
    a tiebreak between same-day multi-shop snapshots)."""
    rows = source_conn.execute(
        select(Snapshot.__table__.c.id).order_by(
            Snapshot.__table__.c.snapshot_date.desc(), Snapshot.__table__.c.id.desc()
        )
    ).all()
    return [r[0] for r in rows]


def _copy_item_prices_budgeted(source_conn, target_conn, budget_bytes):
    """Copies item_prices one whole snapshot at a time, most recent first,
    checking the target's real on-disk size before each one and stopping
    before it would cross budget_bytes. Whole snapshots only (never a
    partial one) so a shop's catalog for an included day is never
    missing some of its items."""
    table = ItemPrice.__table__
    total_rows = 0
    included_snapshots = 0
    for snapshot_id in _recent_snapshot_ids_desc(source_conn):
        if _db_size(target_conn) >= budget_bytes:
            break
        rows = source_conn.execute(select(table).where(table.c.snapshot_id == snapshot_id)).mappings().all()
        if rows:
            target_conn.execute(table.insert(), [dict(row) for row in rows])
            total_rows += len(rows)
        included_snapshots += 1
    return total_rows, included_snapshots


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

    print(f"{'table':<28} {'copied':>10} {'source':>10} {'target':>10}")
    mismatched = False
    with source_engine.connect() as source_conn, target_engine.begin() as target_conn:
        _wipe_target(target_conn)
        for model in MODELS:
            table = model.__table__
            if model is ItemPrice:
                copied, included_snapshots = _copy_item_prices_budgeted(
                    source_conn, target_conn, ITEM_PRICES_SIZE_BUDGET_BYTES
                )
                target_count = _count(target_conn, table)
                status = "OK" if copied == target_count else "MISMATCH"
                mismatched = mismatched or status != "OK"
                note = f" (most recent {included_snapshots} snapshot(s), size-budgeted)"
                print(f"{table.name:<28} {copied:>10} {'--':>10} {target_count:>10}  {status}{note}")
                continue

            copied = _copy_table(table, source_conn, target_conn)
            _reset_sequence(target_conn, table.name)
            source_count = _count(source_conn, table)
            target_count = _count(target_conn, table)
            status = "OK" if source_count == target_count else "MISMATCH"
            mismatched = mismatched or source_count != target_count
            print(f"{table.name:<28} {copied:>10} {source_count:>10} {target_count:>10}  {status}")

        # item_prices doesn't get its sequence reset in the loop above
        # (it `continue`s before that point) since it needs to happen
        # after every row for the table has actually landed.
        _reset_sequence(target_conn, ItemPrice.__table__.name)

    with target_engine.connect() as conn:
        print(f"target database size after migration: {_pretty_bytes(_db_size(conn))}")

    if mismatched:
        print("row count mismatch on at least one table -- see above", file=sys.stderr)
        sys.exit(1)
    print("migration complete")


if __name__ == "__main__":
    main()
