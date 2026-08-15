"""Points DATABASE_URL at an isolated temp SQLite file before any test
module imports db.py/webapp.py, so tests never touch the developer's
local food_defects.db (or, worse, a real DATABASE_URL from the
environment). Must run before those imports, hence the env var is set
at collection time, ahead of the pytest imports below.
"""

import os
import tempfile

_fd, _DB_PATH = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"
# Flask-Limiter's per-IP counters are process-wide (memory:// storage),
# so without this every test hitting /refresh or /download/sales.csv
# would share one counter with every other such test in the run and
# could 429 each other depending on test order/count. webapp.py reads
# this at Limiter construction time, hence set before it's imported.
os.environ["DISABLE_RATE_LIMITING"] = "1"

import pytest  # noqa: E402

from db import Base, engine, init_db  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _test_database():
    init_db()
    yield
    try:
        os.remove(_DB_PATH)
    except OSError:
        pass


@pytest.fixture(autouse=True)
def _clean_tables():
    """Every test starts from an empty database -- cheap here (SQLite,
    a handful of tables) and avoids one test's fixture data leaking into
    another's assertions."""
    yield
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


@pytest.fixture(autouse=True)
def _no_outbound_refresh(monkeypatch):
    """POST /refresh normally decides what today's data is missing and
    fetches it from the real e-food API in a background thread. Tests
    only seed a shop or two out of the full tracked list, so without
    this every test hitting /refresh (or /) would kick off real network
    calls for the rest -- slow, flaky, and it leaks shared module-level
    refresh state across tests. Route tests only need to know /refresh
    responds correctly, not that it actually fetches anything."""
    import webapp

    monkeypatch.setattr(webapp, "_shops_needing_refresh", lambda: [])
    yield
