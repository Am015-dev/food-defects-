"""Database layer: one row per item per shop per day, plus a per-day
snapshot summary per shop. Backed by Postgres in production (Render sets
DATABASE_URL) and falling back to a local SQLite file for development.
"""

import os
import time

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///food_defects.db")
# Render (and some other hosts) hand out "postgres://", but SQLAlchemy 1.4+/2.x requires "postgresql://"
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, future=True)
SessionLocal = sessionmaker(bind=engine, future=True)
Base = declarative_base()


class Shop(Base):
    __tablename__ = "shops"

    id = Column(Integer, primary_key=True)  # e-food restaurant id
    label = Column(String, nullable=False)

    snapshots = relationship("Snapshot", back_populates="shop", cascade="all, delete-orphan")


class Snapshot(Base):
    """One fetch of one shop's full catalog, for one calendar day (UTC)."""

    __tablename__ = "snapshots"

    id = Column(Integer, primary_key=True)
    shop_id = Column(Integer, ForeignKey("shops.id"), nullable=False)
    snapshot_date = Column(String, nullable=False)  # "YYYY-MM-DD"
    fetched_at = Column(DateTime, nullable=False)
    store_title = Column(String)
    store_address = Column(String)
    is_open = Column(Boolean)
    total_items = Column(Integer)
    total_categories = Column(Integer)
    zero_price_bug_count = Column(Integer)
    placeholder_bug_count = Column(Integer)
    verified_deal_count = Column(Integer)

    shop = relationship("Shop", back_populates="snapshots")
    item_prices = relationship("ItemPrice", back_populates="snapshot", cascade="all, delete-orphan")

    __table_args__ = (UniqueConstraint("shop_id", "snapshot_date", name="uq_shop_snapshot_day"),)


class ItemPrice(Base):
    """One product's price on one day, within one shop's snapshot."""

    __tablename__ = "item_prices"

    id = Column(Integer, primary_key=True)
    snapshot_id = Column(Integer, ForeignKey("snapshots.id"), nullable=False)
    item_id = Column(Integer, nullable=False)  # e-food's id, store-specific (not shared across shops)
    code = Column(String)  # e-food's item uuid, used to link back to the live product
    name = Column(String, nullable=False)
    category = Column(String)
    price = Column(Float)
    full_price = Column(Float)
    l30d_price = Column(Float)
    size_info = Column(String)
    metric_unit_description = Column(String)  # e-food's own unit price, e.g. "3,36€ / kg"
    unit_price = Column(Float)  # parsed from metric_unit_description (or size_info fallback)
    name_fold = Column(String)  # accent-stripped, casefolded name, for search
    product_id = Column(Integer)  # denormalized from ProductListing at ingest time, see product_matching.py
    is_zero_price_bug = Column(Boolean, default=False)
    is_placeholder_bug = Column(Boolean, default=False)
    is_verified_deal = Column(Boolean, default=False)
    deal_pct = Column(Float, nullable=True)

    snapshot = relationship("Snapshot", back_populates="item_prices")


class Product(Base):
    """One real-world product, matched across shops/chains by
    product_matching.py. canonical_name is just the name of whichever
    listing first created it -- a display label, not authoritative."""

    __tablename__ = "products"

    id = Column(Integer, primary_key=True)
    canonical_name = Column(String, nullable=False)
    category = Column(String)  # top-level group, e.g. "Τρόφιμα"
    created_at = Column(DateTime, nullable=False)


class ProductListing(Base):
    """Stable (shop_id, code) -> Product cache. Written once, the first
    time a listing is seen, so later ingests look this up instead of
    re-matching -- see product_matching.py and ingest.py."""

    __tablename__ = "product_listings"

    id = Column(Integer, primary_key=True)
    shop_id = Column(Integer, ForeignKey("shops.id"), nullable=False)
    code = Column(String, nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    match_confidence = Column(Float)  # 1.0 = created a new product, else the fuzzy match score / 100
    first_seen_name = Column(String, nullable=False)
    created_at = Column(DateTime, nullable=False)

    __table_args__ = (UniqueConstraint("shop_id", "code", name="uq_shop_code"),)


class PriceExtreme(Base):
    """One row per currently-listed (shop_id, code): its lowest and
    highest real price across the retained item_prices history (see
    retention.py -- effectively a rolling ~90-day window). Computed
    once nightly by ingest.py's rollup step on the GitHub Actions
    runner, which has the RAM to aggregate the full history; the web
    service only ever reads this small precomputed table, never
    recomputes it live. Delisted items' rows are deleted at rollup
    time (see update_price_extremes in queries.py), not merely left
    stale."""

    __tablename__ = "price_extremes"

    id = Column(Integer, primary_key=True)
    shop_id = Column(Integer, ForeignKey("shops.id"), nullable=False)
    code = Column(String, nullable=False)
    name = Column(String, nullable=False)
    category = Column(String)
    current_price = Column(Float, nullable=False)
    min_price = Column(Float, nullable=False)
    max_price = Column(Float, nullable=False)
    swing_pct = Column(Float, nullable=False)
    updated_at = Column(DateTime, nullable=False)

    __table_args__ = (UniqueConstraint("shop_id", "code", name="uq_price_extreme_shop_code"),)


def _add_missing_columns():
    """Tiny forward-only migration.

    create_all() only creates missing TABLES, never missing columns, so a
    database created by an earlier version keeps its old shape and every
    query naming a new column fails. Rather than pull in a migration
    framework for one column, add whatever is missing directly.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if not inspector.has_table("item_prices"):
        return
    existing = {c["name"] for c in inspector.get_columns("item_prices")}
    new_columns = {
        "code": "VARCHAR",
        "metric_unit_description": "VARCHAR",
        "unit_price": "FLOAT",
        "name_fold": "VARCHAR",
        "product_id": "INTEGER",
    }
    for name, sql_type in new_columns.items():
        if name not in existing:
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE item_prices ADD COLUMN {name} {sql_type}"))
            print(f"db: added item_prices.{name}")


def _drop_removed_columns():
    """The reverse of _add_missing_columns(): drops columns that used to
    be part of the schema but no longer are. Needs SQLite 3.35+ (2021)
    or Postgres (DROP COLUMN has always been supported) -- both apply
    here, so no version guard.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if not inspector.has_table("item_prices"):
        return
    existing = {c["name"] for c in inspector.get_columns("item_prices")}
    # unit_kind was write-only: computed and stored at ingest time but
    # never read by any query, route, or template -- unit_price alone
    # is what /deals and /search actually sort by, and display already
    # shows the appropriate unit via get_price_comparison_info without
    # needing the stored column.
    removed_columns = ["unit_kind"]
    for name in removed_columns:
        if name in existing:
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE item_prices DROP COLUMN {name}"))
            print(f"db: dropped item_prices.{name}")


def _ensure_indexes():
    """Idempotent index creation, same forward-only philosophy as
    _add_missing_columns(). The filterable pages (deals, dashboard search,
    compare) all hit item_prices by snapshot with extra predicates, and
    the staleness check hits snapshots by (shop, date) on every page load.
    CREATE INDEX IF NOT EXISTS is understood by both SQLite and Postgres.
    """
    from sqlalchemy import text

    statements = [
        "CREATE INDEX IF NOT EXISTS ix_item_prices_snapshot ON item_prices (snapshot_id)",
        "CREATE INDEX IF NOT EXISTS ix_item_prices_snapshot_category ON item_prices (snapshot_id, category)",
        "CREATE INDEX IF NOT EXISTS ix_snapshots_shop_date ON snapshots (shop_id, snapshot_date)",
        # Powers get_item_history_by_code and the verify page's snapshot
        # fallback search, both of which filter by code across all of a
        # shop's history -- without this they full-scan item_prices.
        "CREATE INDEX IF NOT EXISTS ix_item_prices_code ON item_prices (code)",
        # compare_across_shops groups by product_id -- see product_matching.py.
        "CREATE INDEX IF NOT EXISTS ix_item_prices_product ON item_prices (product_id)",
        # get_flagged_items_filtered (dashboard bug lists, shop.html) runs
        # once per shop filtering snapshot_id plus exactly one of these
        # three flags -- a plain snapshot index alone still leaves that
        # second predicate to filter row-by-row after the scan.
        "CREATE INDEX IF NOT EXISTS ix_item_prices_snapshot_zero_bug "
        "ON item_prices (snapshot_id, is_zero_price_bug)",
        "CREATE INDEX IF NOT EXISTS ix_item_prices_snapshot_placeholder_bug "
        "ON item_prices (snapshot_id, is_placeholder_bug)",
        "CREATE INDEX IF NOT EXISTS ix_item_prices_snapshot_deal "
        "ON item_prices (snapshot_id, is_verified_deal)",
        # _shop_price_drops_query self-joins on (snapshot_id, code) from
        # both sides -- a composite serves that directly instead of
        # merging two single-column index scans.
        "CREATE INDEX IF NOT EXISTS ix_item_prices_snapshot_code ON item_prices (snapshot_id, code)",
        # get_product_across_shops and compare_across_shops both filter
        # snapshot_id together with product_id.
        "CREATE INDEX IF NOT EXISTS ix_item_prices_snapshot_product ON item_prices (snapshot_id, product_id)",
        # The /extremes leaderboard reads this table sorted by swing size.
        "CREATE INDEX IF NOT EXISTS ix_price_extremes_swing ON price_extremes (swing_pct)",
    ]
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))


def init_db(retries=5, delay_seconds=2):
    """Create tables if they don't exist yet. Retries briefly: on a fresh
    deploy the web service can start importing before a just-provisioned
    database is actually ready to accept connections."""
    last_exc = None
    for attempt in range(retries):
        try:
            Base.metadata.create_all(engine)
            _add_missing_columns()
            _drop_removed_columns()
            _ensure_indexes()
            return
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(delay_seconds)
    raise last_exc
