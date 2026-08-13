"""Flask test-client smoke tests: every route, plus representative filter
combinations, against a small seeded SQLite database."""

from datetime import datetime, timezone

import pytest

from db import SessionLocal
from ingest import store_snapshot
from shops import SHOPS

SHOP_A = SHOPS[0]["id"]
SHOP_A_LABEL = SHOPS[0]["label"]
SHOP_B = SHOPS[1]["id"]
SHOP_B_LABEL = SHOPS[1]["label"]

TODAY = "2026-08-13"

COMMON_PRODUCT = "Κοινό Προϊόν Δοκιμής"


def _catalog(items):
    return {
        "information": {
            "title": "Test Shop",
            "address": {"description": "Test Address 1"},
            "is_open": True,
        },
        "menu": {"categories": [{"name": "Τρόφιμα || Δοκιμαστικά", "items": items}]},
    }


def _item(id_, name, price, **extra):
    d = {"id": id_, "code": f"code-{id_}", "name": name, "price": price, "tags": []}
    d.update(extra)
    return d


@pytest.fixture
def client():
    from webapp import app

    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture
def seeded():
    """Two shops carrying the same product at different prices (for
    /compare and cross-shop comparison), plus one of each bug type in
    shop A (for the dashboard/deals views)."""
    session = SessionLocal()
    try:
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            TODAY,
            _catalog(
                [
                    _item(
                        1,
                        COMMON_PRODUCT,
                        1.68,
                        size_info="500g",
                        metric_unit_description="3,36€ / kg",
                    ),
                    _item(2, "Μηδενική Τιμή", 0.0),
                    _item(3, "Πλασματική Τιμή", 2.0, tags=["l30d:0.01"]),
                    _item(
                        4,
                        "Πραγματική Προσφορά",
                        3.0,
                        full_price=6.0,
                        tags=["l30d:5.0"],
                    ),
                ]
            ),
        )
        store_snapshot(
            session,
            SHOP_B,
            SHOP_B_LABEL,
            TODAY,
            _catalog(
                [
                    _item(
                        101,
                        COMMON_PRODUCT,
                        2.10,
                        size_info="500g",
                        metric_unit_description="4,20€ / kg",
                    ),
                ]
            ),
        )
        session.commit()
    finally:
        session.close()
    return {"shop_a": SHOP_A, "shop_b": SHOP_B}


def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"


def test_robots_txt(client):
    resp = client.get("/robots.txt")
    assert resp.status_code == 200
    assert b"Disallow: /compare" in resp.data


def test_dashboard_empty(client):
    resp = client.get("/")
    assert resp.status_code == 200


def test_dashboard_with_data(client, seeded):
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Μηδενική Τιμή" in body
    assert "Πλασματική Τιμή" in body
    assert "Πραγματική Προσφορά" in body


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"q": "Μηδενική"},
        {"shop": str(SHOP_A)},
        {"type": "zero"},
        {"type": "placeholder"},
        {"type": "deal"},
        {"type": "bogus"},  # invalid value should be ignored, not 500
        {"shop": "999999"},  # unknown shop id should be ignored, not 500
    ],
)
def test_dashboard_filters(client, seeded, params):
    resp = client.get("/", query_string=params)
    assert resp.status_code == 200


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"min_pct": "10"},
        {"sort": "price"},
        {"sort": "name"},
        {"page": "2"},
        {"page": "0"},  # should clamp to page 1, not error
        {"page": "abc"},  # non-numeric, should fall back to default
        {"shop": str(SHOP_A)},
    ],
)
def test_deals_filters(client, seeded, params):
    resp = client.get("/deals", query_string=params)
    assert resp.status_code == 200


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"min_spread": "10"},
        {"q": COMMON_PRODUCT},
        {"category": "Τρόφιμα"},
    ],
)
def test_compare_filters(client, seeded, params):
    resp = client.get("/compare", query_string=params)
    assert resp.status_code == 200


def test_compare_shows_cross_shop_spread(client, seeded):
    resp = client.get("/compare")
    body = resp.get_data(as_text=True)
    assert COMMON_PRODUCT in body
    # Both shops' prices should appear since the spread exceeds 5%.
    assert "1,68" in body or "1,68 €" in body


def test_history_known_shop(client, seeded):
    resp = client.get(f"/history/{SHOP_A}")
    assert resp.status_code == 200


def test_history_unknown_shop_404s(client):
    resp = client.get("/history/999999")
    assert resp.status_code == 404


def test_download_csv(client, seeded):
    resp = client.get("/download/sales.csv")
    assert resp.status_code == 200
    assert resp.mimetype == "text/csv"


def test_download_csv_filtered_by_shop(client, seeded):
    resp = client.get("/download/sales.csv", query_string={"shop_id": str(SHOP_A)})
    assert resp.status_code == 200


def test_item_page_unknown_shop_404s(client):
    resp = client.get("/item/999999/some-code")
    assert resp.status_code == 404


def test_item_page_shows_stored_product_name(client, seeded, monkeypatch):
    # The live e-food call is out of scope for a route smoke test --
    # stub it so the test doesn't depend on network access.
    monkeypatch.setattr(
        "webapp.fetch_menu_item",
        lambda shop_id, code, timeout=20: {
            "price": 1.68,
            "full_price": None,
            "calculated_price": None,
            "is_available": True,
            "tags": [],
        },
    )
    resp = client.get(f"/item/{SHOP_A}/code-1")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert COMMON_PRODUCT in body
    # Regression: the page used to show only the shop, not the product.
    assert SHOP_A_LABEL in body


def test_item_page_cross_shop_comparison_survives_live_fetch_error(client, seeded, monkeypatch):
    def _raise(*_args, **_kwargs):
        raise RuntimeError("simulated e-food outage")

    monkeypatch.setattr("webapp.fetch_menu_item", _raise)
    # Regression: this used to 500 the whole page.
    resp = client.get(f"/item/{SHOP_A}/code-1")
    assert resp.status_code == 200
    assert COMMON_PRODUCT in resp.get_data(as_text=True)


def test_refresh_requires_post(client):
    resp = client.get("/refresh")
    assert resp.status_code == 405


def test_refresh_post_returns_json_by_default(client):
    resp = client.post("/refresh")
    assert resp.status_code == 200
    assert resp.get_json() is not None


def test_refresh_post_html_redirects_to_dashboard(client):
    resp = client.post("/refresh", headers={"Accept": "text/html"})
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/"


def test_dashboard_stale_banner_absent_for_fresh_data(client, seeded):
    resp = client.get("/")
    body = resp.get_data(as_text=True)
    assert "δεδομένα μπορεί να είναι παλιά" not in body


def test_dashboard_stale_banner_shown_for_old_data(client):
    session = SessionLocal()
    try:
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            TODAY,
            _catalog([_item(1, COMMON_PRODUCT, 1.0)]),
        )
        # Backdate fetched_at well past the staleness threshold.
        from db import Snapshot

        snap = session.query(Snapshot).filter_by(shop_id=SHOP_A).one()
        snap.fetched_at = datetime(2000, 1, 1, tzinfo=timezone.utc).replace(tzinfo=None)
        session.commit()
    finally:
        session.close()

    resp = client.get("/")
    assert "δεδομένα μπορεί να είναι παλιά" in resp.get_data(as_text=True)
