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
    is_zero_price_bug = Column(Boolean, default=False)
    is_placeholder_bug = Column(Boolean, default=False)
    is_verified_deal = Column(Boolean, default=False)
    deal_pct = Column(Float, nullable=True)

    snapshot = relationship("Snapshot", back_populates="item_prices")


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
    if "code" not in existing:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE item_prices ADD COLUMN code VARCHAR"))
        print("db: added item_prices.code")


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
            _ensure_indexes()
            return
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(delay_seconds)
    raise last_exc
