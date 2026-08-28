"""One-off migration: copy every table from the source Postgres
(RENDER_DATABASE_URL) to a fresh Turso database (TURSO_DATABASE_URL +
TURSO_AUTH_TOKEN), row for row, in FK-safe order.

Turso instead of Neon: Neon's free tier caps a whole project at 512MB,
and this app's real item_prices history didn't fit even after being cut
down to the most recent few days (see the "Migrate off Render's free
Postgres" plan and the abandoned scripts/migrate_to_neon.py for the
full story). Turso's free tier is 5GB -- comfortably more than this
database's entire current size (roughly 700MB) -- so this script copies
everything in full, no size budget or day-based cutoff needed.

Unlike migrate_to_neon.py, there's no sequence-reset step: SQLite (and
Turso, which is SQLite-compatible) doesn't use a separate sequence
object for a plain `INTEGER PRIMARY KEY` column the way Postgres does
-- after an explicit-id insert, the next id-less insert on the same
table just picks up MAX(id)+1 automatically (verified locally before
writing this).

Meant to be run once, manually, from a GitHub Actions workflow_dispatch
(see .github/workflows/migrate-to-turso.yml) -- not part of the regular
pipeline.
"""

import os
import sys

from sqlalchemy import create_engine, func, select
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

BATCH_SIZE = 1000

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
    these tables are hundreds of thousands of rows, and OFFSET
    pagination gets quadratically slower as it grows."""
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


def _count(conn, table):
    return conn.execute(select(func.count()).select_from(table)).scalar()


def _wipe_target(target_conn):
    """Deletes every row from every migrated table, children first (MODELS
    reversed). Makes a retry after a failed/partial run safe to just run
    again, instead of hitting duplicate-key errors on rows that already
    landed."""
    for model in reversed(MODELS):
        target_conn.execute(model.__table__.delete())


def _same_database(url_a, url_b):
    """Compares host+port+database only, not credentials -- belt and
    suspenders against ever copying a database into itself, the mistake
    that broke the first Neon migration attempt. Source and target here
    use different secret names and different URL schemes entirely, so
    this is much harder to hit by accident than it was there, but the
    check is cheap to keep anyway."""
    a, b = make_url(url_a), make_url(url_b)
    return (a.host, a.port, a.database) == (b.host, b.port, b.database)


def _require_env(name):
    """A GitHub Actions workflow's `env:` block always defines the
    variable name even when the underlying secret doesn't exist or is
    blank -- os.environ[name] would return "" rather than raise KeyError
    in that case, so check for blank explicitly instead of just presence."""
    value = os.environ.get(name, "")
    if not value:
        print(f"{name} is not set (or is blank) -- check the GitHub secret's value.", file=sys.stderr)
        sys.exit(1)
    return value


def main():
    source_url = _normalize(_require_env("RENDER_DATABASE_URL"))
    turso_url = _require_env("TURSO_DATABASE_URL")
    turso_token = _require_env("TURSO_AUTH_TOKEN")
    target_url = db_module.normalize_database_url(turso_url, auth_token=turso_token)

    if _same_database(source_url, target_url):
        print(
            "RENDER_DATABASE_URL and TURSO_DATABASE_URL point at the same "
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
            copied = _copy_table(table, source_conn, target_conn)
            source_count = _count(source_conn, table)
            target_count = _count(target_conn, table)
            status = "OK" if source_count == target_count else "MISMATCH"
            mismatched = mismatched or source_count != target_count
            print(f"{table.name:<28} {copied:>10} {source_count:>10} {target_count:>10}  {status}")

    if mismatched:
        print("row count mismatch on at least one table -- see above", file=sys.stderr)
        sys.exit(1)
    print("migration complete")


if __name__ == "__main__":
    main()
