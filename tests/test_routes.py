"""Flask test-client smoke tests: every route, plus representative filter
combinations, against a small seeded SQLite database."""

from datetime import datetime, timezone

import pytest

from db import SessionLocal
from ingest import store_snapshot
from shops import SHOPS

# Captured at collection time, before the autouse _no_outbound_refresh
# fixture (conftest.py) monkeypatches webapp._shops_needing_refresh for
# every test -- this reference is unaffected by that patch, so tests
# that need the real logic can call it directly.
from webapp import _shops_needing_refresh as _real_shops_needing_refresh

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


DROP_PRODUCT = "Ελαιόλαδο Δοκιμής"
STEADY_PRODUCT = "Σταθερό Προϊόν"


@pytest.fixture
def seeded_with_drop():
    """Two consecutive snapshots for shop A: one product's price fell,
    another's stayed the same -- for /drops and the dashboard section."""
    session = SessionLocal()
    try:
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-12",
            _catalog([_item(1, DROP_PRODUCT, 10.0), _item(2, STEADY_PRODUCT, 2.0)]),
        )
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-13",
            _catalog([_item(1, DROP_PRODUCT, 7.5), _item(2, STEADY_PRODUCT, 2.0)]),
        )
        session.commit()
    finally:
        session.close()
    return {"shop_a": SHOP_A}


def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"


def test_healthz_db_failure_does_not_leak_exception_detail(client, monkeypatch):
    # Regression: /healthz used to return str(exc) verbatim, which for a
    # real connection failure commonly includes host/port/DSN details --
    # unacceptable for a public, unauthenticated endpoint.
    class _BrokenSession:
        def execute(self, *args, **kwargs):
            raise RuntimeError("connection to server at db.internal.example:5432 failed")

        def close(self):
            pass

    monkeypatch.setattr("webapp.SessionLocal", lambda: _BrokenSession())
    resp = client.get("/healthz")
    assert resp.status_code == 503
    body = resp.get_json()
    assert body == {"status": "error"}
    assert "db.internal.example" not in resp.get_data(as_text=True)


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


def test_dashboard_search_is_accent_insensitive(client, seeded):
    # "Μηδενική" carries a tone mark; searching unaccented, wrong-case
    # Greek should still find it via the name_fold column.
    resp = client.get("/", query_string={"q": "μηδενικη"})
    assert resp.status_code == 200
    assert "Μηδενική Τιμή" in resp.get_data(as_text=True)


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
        {"sort": "unit_price"},
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


def test_deals_out_of_range_page_shows_real_rows_not_empty(client, seeded):
    # Regression: get_deals_page used to run the SQL query at the
    # caller's raw, unclamped page (offset far past the last real row),
    # while the template's "page N of pages" label was clamped and so
    # claimed real data was shown when the table was actually empty.
    resp = client.get("/deals", query_string={"page": "999"})
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Πραγματική Προσφορά" in body  # the one seeded verified deal
    assert "Καμία προσφορά" not in body  # empty-state text must not show


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


def test_compare_groups_differently_phrased_names_across_chains(client):
    # Regression for the product identity layer: two different chains
    # phrasing the same product differently (word order, size format)
    # must still land in one comparison group -- the old exact-name
    # matching would have missed this entirely.
    session = SessionLocal()
    try:
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            TODAY,
            _catalog([_item(1, "Anatoli Κουρκουμάς 60g", 1.50)]),
        )
        store_snapshot(
            session,
            SHOP_B,
            SHOP_B_LABEL,
            TODAY,
            _catalog([_item(2, "Κουρκουμάς Anatoli 100g", 1.90)]),
        )
        session.commit()
    finally:
        session.close()

    resp = client.get("/compare")
    body = resp.get_data(as_text=True)
    assert "Anatoli Κουρκουμάς 60g" in body
    assert SHOP_A_LABEL in body and SHOP_B_LABEL in body


def test_compare_guards_unfiltered_scan_past_threshold(client, seeded, monkeypatch):
    # The seeded fixture stores 5 priced rows total -- well under any
    # real threshold, so drop it low enough to force the guard on.
    monkeypatch.setattr("webapp.COMPARE_SCAN_GUARD_ROWS", 1)
    resp = client.get("/compare")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "πολύ μεγάλος για σύγκριση" in body
    assert COMMON_PRODUCT not in body


def test_compare_guard_bypassed_by_query(client, seeded, monkeypatch):
    monkeypatch.setattr("webapp.COMPARE_SCAN_GUARD_ROWS", 1)
    resp = client.get("/compare", query_string={"q": COMMON_PRODUCT})
    body = resp.get_data(as_text=True)
    assert "πολύ μεγάλος για σύγκριση" not in body
    assert COMMON_PRODUCT in body


def test_compare_guard_bypassed_by_category(client, seeded, monkeypatch):
    monkeypatch.setattr("webapp.COMPARE_SCAN_GUARD_ROWS", 1)
    resp = client.get("/compare", query_string={"category": "Τρόφιμα"})
    body = resp.get_data(as_text=True)
    assert "πολύ μεγάλος για σύγκριση" not in body
    assert COMMON_PRODUCT in body


def test_compare_guard_exact_threshold_boundary(client, seeded, monkeypatch):
    # Regression guard: the comparator is `catalog_size > threshold`, so
    # a catalog exactly AT the threshold must NOT trigger the guard, and
    # one row past it MUST -- an easy > vs >= flip to get wrong later.
    # Computed dynamically from the actual seeded data rather than a
    # guessed row count, so this stays correct if the fixture changes.
    from db import ItemPrice
    from queries import get_latest_snapshots_for_all_shops

    shop_labels = {s["id"]: s["label"] for s in SHOPS}
    session = SessionLocal()
    try:
        latest = get_latest_snapshots_for_all_shops(session, list(shop_labels))
        catalog_size = (
            session.query(ItemPrice.id)
            .filter(ItemPrice.snapshot_id.in_([s.id for s in latest.values()]), ItemPrice.price > 0)
            .count()
        )
    finally:
        session.close()
    assert catalog_size > 0  # sanity: the fixture must have actually seeded priced rows

    monkeypatch.setattr("webapp.COMPARE_SCAN_GUARD_ROWS", catalog_size)
    resp = client.get("/compare")
    assert "πολύ μεγάλος για σύγκριση" not in resp.get_data(as_text=True)

    monkeypatch.setattr("webapp.COMPARE_SCAN_GUARD_ROWS", catalog_size - 1)
    resp = client.get("/compare")
    assert "πολύ μεγάλος για σύγκριση" in resp.get_data(as_text=True)


@pytest.mark.parametrize(
    "params",
    [
        {},  # no q -- should not scan the whole catalog, just render empty
        {"q": COMMON_PRODUCT},
        {"q": "κοινο προϊον δοκιμης"},  # unaccented/wrong-case, same regression as elsewhere
        {"q": COMMON_PRODUCT, "sort": "unit_price"},
        {"q": COMMON_PRODUCT, "sort": "name"},
        {"q": COMMON_PRODUCT, "shop": str(SHOP_A)},
        {"q": COMMON_PRODUCT, "category": "Τρόφιμα"},
        {"q": "nothing matches this"},
        {"q": COMMON_PRODUCT, "sort": "bogus"},  # invalid sort should fall back, not 500
    ],
)
def test_search_filters(client, seeded, params):
    resp = client.get("/search", query_string=params)
    assert resp.status_code == 200


def test_search_without_query_does_not_list_everything(client, seeded):
    resp = client.get("/search")
    body = resp.get_data(as_text=True)
    assert COMMON_PRODUCT not in body


def test_search_finds_product_across_shops(client, seeded):
    resp = client.get("/search", query_string={"q": COMMON_PRODUCT})
    body = resp.get_data(as_text=True)
    assert SHOP_A_LABEL in body
    assert SHOP_B_LABEL in body


def test_search_out_of_range_page_shows_real_rows_not_empty(client, seeded):
    # Same regression as /deals: the query must be clamped to the real
    # last page before running, not after.
    resp = client.get("/search", query_string={"q": COMMON_PRODUCT, "page": "999"})
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert COMMON_PRODUCT in body
    assert "Κανένα αποτέλεσμα" not in body


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"q": DROP_PRODUCT},
        {"shop": str(SHOP_A)},
        {"category": "Τρόφιμα"},
        {"page": "2"},
    ],
)
def test_drops_filters(client, seeded_with_drop, params):
    resp = client.get("/drops", query_string=params)
    assert resp.status_code == 200


def test_drops_finds_the_drop_but_not_the_steady_price(client, seeded_with_drop):
    resp = client.get("/drops")
    body = resp.get_data(as_text=True)
    assert DROP_PRODUCT in body
    assert STEADY_PRODUCT not in body
    assert "-25%" in body  # (10.0 - 7.5) / 10.0 * 100


def test_drops_empty_without_two_snapshots(client, seeded):
    # `seeded` only stores one snapshot per shop -- no prior day to diff
    # against, so there should be nothing to show, not an error.
    resp = client.get("/drops")
    assert resp.status_code == 200
    assert "Καμία πτώση" in resp.get_data(as_text=True)


def test_drops_paginates_across_the_50_per_page_boundary(client):
    # Regression: get_price_drops used to pull every drop across every
    # shop into Python before sorting/slicing -- this proves pagination
    # now genuinely happens in SQL: 54 real drops, page size 50, so page
    # 1 and page 2 must show disjoint, non-overlapping sets of items.
    import re

    session = SessionLocal()
    try:
        yesterday = [_item(i, f"Item {i:03d}", 10.0) for i in range(60)]
        today = [_item(i, f"Item {i:03d}", 10.0 - (i % 10) * 0.1) for i in range(60)]
        store_snapshot(session, SHOP_A, SHOP_A_LABEL, "2026-08-12", _catalog(yesterday))
        store_snapshot(session, SHOP_A, SHOP_A_LABEL, "2026-08-13", _catalog(today))
        session.commit()
    finally:
        session.close()

    page1_body = client.get("/drops", query_string={"page": "1"}).get_data(as_text=True)
    page2_body = client.get("/drops", query_string={"page": "2"}).get_data(as_text=True)
    page1_items = set(re.findall(r"Item \d{3}", page1_body))
    page2_items = set(re.findall(r"Item \d{3}", page2_body))

    assert len(page1_items) == 50
    assert len(page2_items) == 4  # 60 items, 6 unchanged (i % 10 == 0), 54 real drops
    assert page1_items.isdisjoint(page2_items)


def test_dashboard_price_drops_teaser_is_capped_at_5(client):
    session = SessionLocal()
    try:
        yesterday = [_item(i, f"Item {i:03d}", 10.0) for i in range(20)]
        today = [_item(i, f"Item {i:03d}", 5.0) for i in range(20)]  # all 20 dropped
        store_snapshot(session, SHOP_A, SHOP_A_LABEL, "2026-08-12", _catalog(yesterday))
        store_snapshot(session, SHOP_A, SHOP_A_LABEL, "2026-08-13", _catalog(today))
        session.commit()
    finally:
        session.close()

    import re

    body = client.get("/").get_data(as_text=True)
    assert len(set(re.findall(r"Item \d{3}", body))) == 5


def test_dashboard_shows_price_drops(client, seeded_with_drop):
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert DROP_PRODUCT in body
    assert STEADY_PRODUCT not in body


def test_history_known_shop(client, seeded):
    resp = client.get(f"/history/{SHOP_A}")
    assert resp.status_code == 200


def test_history_unknown_shop_404s(client):
    resp = client.get("/history/999999")
    assert resp.status_code == 404


def test_shop_page_known_shop(client, seeded):
    resp = client.get(f"/shop/{SHOP_A}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Μηδενική Τιμή" in body
    assert "Πραγματική Προσφορά" in body


def test_shop_page_unknown_shop_404s(client):
    resp = client.get("/shop/999999")
    assert resp.status_code == 404


def test_shop_page_no_data_shop_shows_empty_state(client):
    # A tracked shop that's never had a snapshot at all -- must not 500.
    from shops import SHOPS

    untouched = next(s["id"] for s in SHOPS if s["id"] != SHOP_A and s["id"] != SHOP_B)
    resp = client.get(f"/shop/{untouched}")
    assert resp.status_code == 200


def test_shop_jump_redirects_to_shop_page(client):
    resp = client.get("/shop", query_string={"shop": str(SHOP_A)})
    assert resp.status_code == 302
    assert resp.headers["Location"] == f"/shop/{SHOP_A}"


def test_shop_jump_unknown_shop_redirects_to_dashboard(client):
    resp = client.get("/shop", query_string={"shop": "999999"})
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/"


def test_download_csv(client, seeded):
    resp = client.get("/download/sales.csv")
    assert resp.status_code == 200
    assert resp.mimetype == "text/csv"


def test_download_csv_filtered_by_shop(client):
    # Regression: this used to only assert status_code == 200, which
    # would keep passing even if shop_id were silently ignored and every
    # shop's rows came back mixed together.
    session = SessionLocal()
    try:
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            TODAY,
            _catalog([_item(1, "Discount In Shop A", 3.0, full_price=6.0)]),
        )
        store_snapshot(
            session,
            SHOP_B,
            SHOP_B_LABEL,
            TODAY,
            _catalog([_item(2, "Discount In Shop B", 4.0, full_price=8.0)]),
        )
        session.commit()
    finally:
        session.close()

    resp = client.get("/download/sales.csv", query_string={"shop_id": str(SHOP_A)})
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Discount In Shop A" in body
    assert "Discount In Shop B" not in body


def test_download_csv_category_filter_applies_to_every_shop(client):
    # Regression: iter_all_sales' per-shop loop reused the name `category`
    # for both the filter parameter AND the row-unpacking variable in the
    # inner "for name, category, ... in discounted" loop. Since Python
    # for-loops don't scope, that clobbered the filter parameter with the
    # last row's raw category value after the first shop -- so a category
    # filter silently stopped matching every shop after the first, unless
    # they happened to share the exact same subcategory string.
    def catalog_with_category(category_group, item):
        return {
            "information": {"title": "T", "address": {"description": "A"}, "is_open": True},
            "menu": {"categories": [{"name": category_group, "items": [item]}]},
        }

    session = SessionLocal()
    try:
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            TODAY,
            catalog_with_category(
                "Τρόφιμα || Ζυμαρικά",
                {"id": 1, "code": "c1", "name": "Pasta Deal", "price": 1.0, "full_price": 2.0, "tags": []},
            ),
        )
        store_snapshot(
            session,
            SHOP_B,
            SHOP_B_LABEL,
            TODAY,
            catalog_with_category(
                "Τρόφιμα || Κρέας",  # different subcategory, still under "Τρόφιμα"
                {"id": 2, "code": "c2", "name": "Meat Deal", "price": 3.0, "full_price": 5.0, "tags": []},
            ),
        )
        session.commit()
    finally:
        session.close()

    resp = client.get("/download/sales.csv", query_string={"category": "Τρόφιμα"})
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Pasta Deal" in body
    assert "Meat Deal" in body


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


def test_shops_needing_refresh_detects_missing_name_fold(client):
    # Regression: _shops_needing_refresh only checked `code` for
    # usability, so rows from before the name_fold/unit_price/
    # metric_unit_description migration (code already populated, those
    # three still NULL) were wrongly treated as fully usable -- a manual
    # refresh wouldn't heal search or unit-price sorting until the next
    # scheduled ingest overwrote the day's snapshot regardless.
    from db import ItemPrice, Snapshot

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    session = SessionLocal()
    try:
        store_snapshot(session, SHOP_A, SHOP_A_LABEL, today, _catalog([_item(1, COMMON_PRODUCT, 1.0)]))
        snap = session.query(Snapshot).filter_by(shop_id=SHOP_A, snapshot_date=today).one()
        row = session.query(ItemPrice).filter_by(snapshot_id=snap.id).one()
        assert row.code is not None  # sanity: code alone would have marked this usable
        row.name_fold = None  # simulate a pre-migration row
        session.commit()
    finally:
        session.close()

    pending_ids = {s["id"] for s in _real_shops_needing_refresh()}
    assert SHOP_A in pending_ids


def test_shops_needing_refresh_skips_fully_usable_shop(client):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    session = SessionLocal()
    try:
        store_snapshot(session, SHOP_A, SHOP_A_LABEL, today, _catalog([_item(1, COMMON_PRODUCT, 1.0)]))
    finally:
        session.close()

    pending_ids = {s["id"] for s in _real_shops_needing_refresh()}
    assert SHOP_A not in pending_ids


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


def test_dashboard_stale_banner_not_masked_by_one_fresh_shop(client):
    # Regression: is_stale used to be driven by MAX(fetched_at) across
    # ALL shops, so one shop refreshing recently hid every other shop
    # having gone stale. Shop A is old, shop B is fresh -- the banner
    # must still show because shop A's own data is stale.
    session = SessionLocal()
    try:
        store_snapshot(
            session, SHOP_A, SHOP_A_LABEL, "2000-01-01", _catalog([_item(1, COMMON_PRODUCT, 1.0)])
        )
        store_snapshot(session, SHOP_B, SHOP_B_LABEL, TODAY, _catalog([_item(2, COMMON_PRODUCT, 1.0)]))

        from db import Snapshot

        old_snap = session.query(Snapshot).filter_by(shop_id=SHOP_A).one()
        old_snap.fetched_at = datetime(2000, 1, 1, tzinfo=timezone.utc).replace(tzinfo=None)
        session.commit()
    finally:
        session.close()

    resp = client.get("/")
    assert "δεδομένα μπορεί να είναι παλιά" in resp.get_data(as_text=True)
