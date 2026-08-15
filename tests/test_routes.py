"""Flask test-client smoke tests: every route, plus representative filter
combinations, against a small seeded SQLite database."""

import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import pytest
import requests
from flask import g

from db import SessionLocal
from ingest import store_snapshot
from shops import SHOPS

# Captured at collection time, before the autouse _no_outbound_refresh
# fixture (conftest.py) monkeypatches webapp._shops_needing_refresh for
# every test -- this reference is unaffected by that patch, so tests
# that need the real logic can call it directly.
from webapp import _shops_needing_refresh as _real_shops_needing_refresh
from webapp import app as flask_app
from webapp import server_error

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


def _multi_catalog(categories):
    """categories: {category_name: [items]} -- for tests that need more
    than one distinct category in a single snapshot."""
    return {
        "information": {
            "title": "Test Shop",
            "address": {"description": "Test Address 1"},
            "is_open": True,
        },
        "menu": {"categories": [{"name": name, "items": items} for name, items in categories.items()]},
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
                    # size_info well below the category's other listings
                    # so this clears the category-competitiveness gate
                    # (see queries.get_category_unit_price_medians) as
                    # well as the plain discount-pct rule: 0.10€/100g
                    # against COMMON_PRODUCT's 0.336/0.42€/100g.
                    _item(
                        4,
                        "Πραγματική Προσφορά",
                        3.0,
                        full_price=6.0,
                        size_info="3kg",
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


MISLEADING_DEAL_PRODUCT_A = "Παγωτό Δοκιμής Βανίλια 1kg"
MISLEADING_DEAL_PRODUCT_B = "Βανίλια Παγωτό Δοκιμής 1kg"  # same product, reordered, for fuzzy match


@pytest.fixture
def seeded_with_misleading_deal():
    """A verified deal in shop A (real discount off its own 30-day low)
    whose matched product -- the same real-world item, per
    product_matching.py -- sells for meaningfully less per kilo in shop
    B at an everyday, non-discounted price. The scenario the user
    reported: a "good price" badge that's true by percentage but still
    not the best per-unit price around.

    Two unrelated, pricier ice creams are seeded alongside so the deal
    also clears the category-competitiveness gate (see
    queries.get_category_unit_price_medians) -- 0.5€/100g against a
    [0.3, 0.5, 0.9, 1.1] category bucket, median 0.7 -- while still
    costing more per unit than its own identical match in shop B."""
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
                        MISLEADING_DEAL_PRODUCT_A,
                        5.0,
                        full_price=16.0,
                        size_info="1kg",
                        tags=["l30d:8.0"],
                    ),
                    _item(2, "Παγωτό Σοκολάτα Οικογενειακό 1kg", 9.0, size_info="1kg"),
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
                    _item(101, MISLEADING_DEAL_PRODUCT_B, 3.0, size_info="1kg"),
                    _item(102, "Παγωτό Φράουλα Οικογενειακό 1kg", 11.0, size_info="1kg"),
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


def test_security_headers_present(client):
    resp = client.get("/")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    csp = resp.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "https://cdn.e-food.gr" in csp  # product/shop thumbnails
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp


def test_html_pages_are_not_cacheable(client, seeded):
    # Regression: every page here shows data that changes daily (or
    # within the hour via manual /refresh) -- without an explicit
    # Cache-Control, a browser or intermediate proxy could serve a
    # stale page indefinitely (reported live: "I still see the old
    # site" after a deploy the server had already picked up).
    for path in ("/", "/deals", "/drops", "/search", "/extremes", "/compare", "/basket"):
        resp = client.get(path)
        assert resp.headers.get("Cache-Control") == "no-store", path


def test_static_assets_keep_their_own_cache_control(client):
    # The blanket no-store above must not clobber static assets' own
    # (correct) revalidate-based caching -- CSS/JS aren't HTML.
    resp = client.get("/static/style.css")
    assert resp.headers.get("Cache-Control") != "no-store"


def test_csp_script_src_has_no_unsafe_inline(client):
    # The CSP must rely on the per-request nonce, not a blanket
    # 'unsafe-inline', or it stops meaningfully restricting inline
    # scripts at all.
    csp = client.get("/").headers["Content-Security-Policy"]
    assert "'unsafe-inline'" not in csp
    assert "nonce-" in csp


def test_csp_nonce_matches_inline_script_tag(client):
    resp = client.get("/")
    csp = resp.headers["Content-Security-Policy"]
    nonce = csp.split("nonce-", 1)[1].split("'", 1)[0]
    body = resp.get_data(as_text=True)
    assert f'nonce="{nonce}"' in body


def test_csp_nonce_differs_per_request(client):
    nonce1 = client.get("/").headers["Content-Security-Policy"]
    nonce2 = client.get("/").headers["Content-Security-Policy"]
    assert nonce1 != nonce2


def test_robots_txt(client):
    resp = client.get("/robots.txt")
    assert resp.status_code == 200
    assert b"Disallow: /compare" in resp.data


def test_404_page_is_themed(client):
    resp = client.get("/shop/999999")
    assert resp.status_code == 404
    body = resp.get_data(as_text=True)
    assert "404" in body
    assert "Πίνακας" in body  # base.html nav present, not Werkzeug's default page


def test_500_page_is_themed():
    # Flask's TESTING=True makes real requests propagate exceptions
    # instead of hitting the errorhandler, so this calls the handler
    # directly rather than triggering a real unhandled exception.
    # test_request_context() doesn't run before_request (only a real
    # dispatched request does), so set what it would have: the CSP
    # nonce inject_globals() reads when rendering 500.html.
    with flask_app.test_request_context():
        g.csp_nonce = "test-nonce"
        body, status = server_error(Exception("simulated failure"))
        assert status == 500
        assert "500" in body
        assert "Πίνακας" in body


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
    # A bug seen for the first time today must not show a streak badge.
    assert "🔁" not in body


def test_dashboard_hero_shows_best_deal(client, seeded):
    body = client.get("/").get_data(as_text=True)
    assert "Καλύτερη προσφορά σήμερα" in body
    assert "Πραγματική Προσφορά" in body
    assert "-40%" in body  # (5.0 - 3.0) / 5.0 * 100


def test_dashboard_flags_misleading_deal_in_hero_and_bargains(client, seeded_with_misleading_deal):
    # The only bargain seeded is the misleading one, so it becomes both
    # the hero pick and the sole bargains-table row -- both must carry
    # the caveat, not just a raw "great discount" headline.
    body = client.get("/").get_data(as_text=True)
    assert "Καλύτερη προσφορά σήμερα" in body
    assert "φθηνότερα ανά μονάδα" in body  # hero caveat sentence
    assert "φθηνότερα στο" in body  # bargains-table caveat badge
    assert SHOP_B_LABEL in body


def test_dashboard_hero_absent_without_deals_or_drops(client):
    body = client.get("/").get_data(as_text=True)
    assert "Καλύτερη προσφορά σήμερα" not in body
    assert "Μεγαλύτερη πτώση τιμής σήμερα" not in body


def test_dashboard_hero_shows_drop_when_no_deals_exist(client, seeded_with_drop):
    body = client.get("/").get_data(as_text=True)
    assert "Μεγαλύτερη πτώση τιμής σήμερα" in body
    assert DROP_PRODUCT in body
    assert "-25%" in body  # (10.0 - 7.5) / 10.0 * 100


def test_dashboard_hero_picks_larger_percentage_between_deal_and_drop(client):
    session = SessionLocal()
    try:
        # A 25% price drop vs. a 60% verified deal -- the deal must win.
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-12",
            _catalog([_item(1, "Dropping Item", 10.0)]),
        )
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            "2026-08-13",
            _catalog(
                [
                    _item(1, "Dropping Item", 7.5),
                    # size_info + two pricier same-category peers so this
                    # clears the category-competitiveness gate (see
                    # queries.get_category_unit_price_medians) as well as
                    # the plain discount-pct rule -- 0.2€/100g against a
                    # [0.2, 0.6, 0.6] bucket median of 0.6.
                    _item(2, "Huge Deal Item", 2.0, full_price=8.0, size_info="1kg", tags=["l30d:5.0"]),
                    _item(3, "Peer Item A", 3.0, size_info="500g"),
                    _item(4, "Peer Item B", 6.0, size_info="1kg"),
                ]
            ),
        )
        session.commit()
    finally:
        session.close()

    body = client.get("/").get_data(as_text=True)
    assert "Καλύτερη προσφορά σήμερα" in body
    assert "Huge Deal Item" in body
    assert "-60%" in body  # (5.0 - 2.0) / 5.0 * 100


def test_dashboard_shows_streak_badge_for_recurring_bug(client):
    session = SessionLocal()
    try:
        for day in ("2026-08-11", "2026-08-12", "2026-08-13"):
            store_snapshot(
                session,
                SHOP_A,
                SHOP_A_LABEL,
                day,
                _catalog([_item(1, "Χρόνιο Σφάλμα", 0.0)]),
            )
        session.commit()
    finally:
        session.close()

    body = client.get("/").get_data(as_text=True)
    assert "🔁 3ημ." in body


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


def test_dashboard_type_deal_filter_hides_bug_panels_shows_bargains(client, seeded):
    # Regression: selecting "Επαληθευμένες προσφορές" used to empty both
    # bug panels (rendering "Καμία." -- indistinguishable from a clean
    # day) while leaving the bargains table unfiltered by type. The bug
    # panels must not render at all under this filter, and the seeded
    # verified deal must still show in the bargains table.
    body = client.get("/", query_string={"type": "deal"}).get_data(as_text=True)
    assert 'id="bugs-summary"' not in body
    assert "Πραγματική Προσφορά" in body


def test_dashboard_type_zero_filter_hides_bargains_panel(client, seeded):
    body = client.get("/", query_string={"type": "zero"}).get_data(as_text=True)
    assert 'id="bargains"' not in body
    assert "Μηδενική Τιμή" in body
    # Placeholder subsection must not render either -- the filter is for
    # zero-price bugs only.
    assert "Πλασματική Τιμή" not in body


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


def test_deals_shows_great_tier_badge_for_40_pct_discount(client, seeded):
    # Seeded "Πραγματική Προσφορά" is 3.0 vs a 5.0 30-day low = 40% off,
    # which clears the 35% "great" tier threshold.
    body = client.get("/deals").get_data(as_text=True)
    assert "ΣΠΟΥΔΑΙΑ" in body


def test_deals_flags_deal_that_is_still_pricier_per_unit_elsewhere(client, seeded_with_misleading_deal):
    body = client.get("/deals").get_data(as_text=True)
    # "⚠" (not just the phrase, which also appears as a plain sort-dropdown
    # option) pins this to the actual caveat badge.
    assert "⚠ Φθηνότερα ανά μονάδα" in body
    assert SHOP_B_LABEL in body
    assert "0,50€/100g" in body  # shop A's deal price normalized: 5.0€ / 1kg


def test_deals_no_caveat_for_deal_without_a_cheaper_match(client, seeded):
    # Seeded "Πραγματική Προσφορά" has no matched listing in another
    # shop at all -- nothing to compare against, so no caveat.
    body = client.get("/deals").get_data(as_text=True)
    assert "⚠ Φθηνότερα ανά μονάδα" not in body


def test_deals_and_dashboard_omit_deal_not_cheap_for_its_category(client):
    # A real discount off its own history, but still the priciest listing
    # in its category, must not appear as a "good deal" anywhere it's
    # advertised -- not the /deals list, not the dashboard bargains
    # table or hero.
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
                        "Ice Cream Deal",
                        5.0,
                        full_price=16.0,
                        size_info="1kg",
                        tags=["l30d:8.0"],
                    ),
                    _item(2, "Peer A", 1.0, size_info="1kg"),
                    _item(3, "Peer B", 2.0, size_info="1kg"),
                    _item(4, "Peer C", 3.0, size_info="1kg"),
                ]
            ),
        )
        session.commit()
    finally:
        session.close()

    deals_body = client.get("/deals").get_data(as_text=True)
    assert "Ice Cream Deal" not in deals_body

    dashboard_body = client.get("/").get_data(as_text=True)
    assert "Ice Cream Deal" not in dashboard_body
    assert "Καλύτερη προσφορά σήμερα" not in dashboard_body


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


def test_match_review_page_loads_empty(client, seeded):
    resp = client.get("/matches")
    assert resp.status_code == 200
    assert "Καμία." in resp.get_data(as_text=True)


def test_match_review_shows_low_confidence_listing(client, seeded):
    from db import ProductListing

    session = SessionLocal()
    try:
        listing = session.query(ProductListing).first()
        assert listing is not None
        listing.match_confidence = 0.91
        session.commit()
    finally:
        session.close()

    body = client.get("/matches").get_data(as_text=True)
    assert "91.0%" in body


def test_basket_empty_shows_form_only(client):
    resp = client.get("/basket")
    assert resp.status_code == 200
    assert "Καλάθι" in resp.get_data(as_text=True) or "καλάθ" in resp.get_data(as_text=True).lower()


def test_basket_matches_items_across_shops(client, seeded):
    # COMMON_PRODUCT is seeded in both shop A (1.68) and shop B (2.10).
    resp = client.get("/basket", query_string={"items": COMMON_PRODUCT})
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert COMMON_PRODUCT in body
    assert SHOP_A_LABEL in body
    assert SHOP_B_LABEL in body


def test_basket_dedupes_and_ignores_blank_lines(client, seeded):
    resp = client.get("/basket", query_string={"items": f"{COMMON_PRODUCT}\n\n{COMMON_PRODUCT}\n  \n"})
    assert resp.status_code == 200
    # One product line rendered once, not twice, despite the duplicate.
    assert resp.get_data(as_text=True).count(f"<h3>{COMMON_PRODUCT}</h3>") == 1


def test_basket_truncates_past_max_items(client, seeded):
    from queries import BASKET_MAX_ITEMS

    items = "\n".join(f"nonexistent-item-{i}" for i in range(BASKET_MAX_ITEMS + 5))
    resp = client.get("/basket", query_string={"items": items})
    assert resp.status_code == 200
    assert "πρώτες" in resp.get_data(as_text=True)  # truncation notice


def test_basket_no_match_shows_empty_state(client, seeded):
    resp = client.get("/basket", query_string={"items": "nothing matches this at all"})
    body = resp.get_data(as_text=True)
    assert "Δεν βρέθηκε" in body


def test_extremes_page_reads_the_rollup_table(client):
    from datetime import datetime, timezone

    from db import PriceExtreme

    session = SessionLocal()
    try:
        session.add(
            PriceExtreme(
                shop_id=SHOP_A,
                code="c1",
                name="Rollup Item",
                category="Τρόφιμα || Test",
                current_price=3.0,
                min_price=2.0,
                max_price=10.0,
                swing_pct=80.0,
                updated_at=datetime.now(timezone.utc),
            )
        )
        session.commit()
    finally:
        session.close()

    resp = client.get("/extremes")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Rollup Item" in body
    assert "80%" in body


def test_extremes_page_empty_is_not_a_500(client):
    resp = client.get("/extremes")
    assert resp.status_code == 200
    assert "Καμία διαθέσιμη" in resp.get_data(as_text=True)


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


def test_category_browse_lists_products_without_a_search_term(client, seeded):
    # Unlike /search, /category needs no q -- the category itself bounds
    # the scan (seeded's items are all under "Τρόφιμα || Δοκιμαστικά").
    resp = client.get("/category/Τρόφιμα")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert COMMON_PRODUCT in body


def test_category_browse_unknown_category_is_empty_not_500(client, seeded):
    resp = client.get("/category/Δεν%20υπάρχει")
    assert resp.status_code == 200
    assert "Κανένα προϊόν" in resp.get_data(as_text=True)


def test_category_browse_filters_by_shop_and_q(client, seeded):
    # "Μηδενική Τιμή" only exists in shop A's seeded catalog -- filtering
    # to shop B must exclude it while still showing the shared product.
    resp = client.get("/category/Τρόφιμα", query_string={"shop": str(SHOP_B)})
    body = resp.get_data(as_text=True)
    assert "Μηδενική Τιμή" not in body
    assert COMMON_PRODUCT in body

    resp = client.get("/category/Τρόφιμα", query_string={"q": "nothing matches this"})
    assert COMMON_PRODUCT not in resp.get_data(as_text=True)


def test_category_browse_out_of_range_page_shows_real_rows_not_empty(client, seeded):
    resp = client.get("/category/Τρόφιμα", query_string={"page": "999"})
    assert resp.status_code == 200
    assert COMMON_PRODUCT in resp.get_data(as_text=True)


def test_category_browse_shows_trend_chart_when_history_exists(client, seeded):
    from db import CategoryDailySummary

    session = SessionLocal()
    try:
        session.add_all(
            [
                CategoryDailySummary(
                    category="Τρόφιμα", snapshot_date="2026-08-12", avg_price=1.5, item_count=4, bug_count=0
                ),
                CategoryDailySummary(
                    category="Τρόφιμα", snapshot_date="2026-08-13", avg_price=2.5, item_count=4, bug_count=1
                ),
            ]
        )
        session.commit()
    finally:
        session.close()

    body = client.get("/category/Τρόφιμα").get_data(as_text=True)
    assert "Μέση τιμή κατηγορίας" in body
    assert "chart" in body


def test_category_browse_no_trend_chart_when_no_history(client, seeded):
    body = client.get("/category/Τρόφιμα").get_data(as_text=True)
    assert "Μέση τιμή κατηγορίας" not in body


def test_deals_category_link_points_at_category_browse(client, seeded):
    body = client.get("/deals").get_data(as_text=True)
    assert "/category/" in body


def test_deals_shows_streak_badge_for_multi_day_verified_deal(client):
    # size_info + two pricier same-category peers so this clears the
    # category-competitiveness gate (see
    # queries.get_category_unit_price_medians), not just the plain
    # discount-pct rule.
    item = {
        "id": 1,
        "code": "c1",
        "name": "Streak Deal",
        "price": 3.0,
        "full_price": 6.0,
        "size_info": "1kg",
        "tags": ["l30d:5.0"],
    }
    peers = [
        _item(2, "Peer Item A", 3.0, size_info="500g"),
        _item(3, "Peer Item B", 6.0, size_info="1kg"),
    ]
    session = SessionLocal()
    try:
        store_snapshot(session, SHOP_A, SHOP_A_LABEL, "2026-08-12", _catalog([item, *peers]))
        store_snapshot(session, SHOP_A, SHOP_A_LABEL, "2026-08-13", _catalog([item, *peers]))
        session.commit()
    finally:
        session.close()

    body = client.get("/deals").get_data(as_text=True)
    assert "Streak Deal" in body
    assert "2ημ." in body


def test_deals_no_streak_badge_for_first_day_deal(client, seeded):
    # seeded's "Πραγματική Προσφορά" appears for the first time today --
    # a streak of 1 must not render a "Nημ." badge (that's the point of
    # the >= 2 gate: today alone isn't a "streak").
    body = client.get("/deals").get_data(as_text=True)
    assert "Πραγματική Προσφορά" in body
    assert "1ημ." not in body


def test_feed_xml_lists_verified_deal(client, seeded):
    resp = client.get("/feed.xml")
    assert resp.status_code == 200
    assert resp.mimetype == "application/rss+xml"
    body = resp.get_data(as_text=True)
    assert "<rss" in body
    assert "Πραγματική Προσφορά" in body  # seeded's verified deal
    assert "Μηδενική Τιμή" not in body  # a bug, not a deal -- must not leak in
    ET.fromstring(body)  # well-formed XML


def test_feed_xml_advertised_in_page_head(client):
    body = client.get("/").get_data(as_text=True)
    assert 'type="application/rss+xml"' in body
    assert "/feed.xml" in body


def test_feed_xml_omits_deal_still_running_from_yesterday():
    item = {
        "id": 1,
        "code": "c1",
        "name": "Stale Deal",
        "price": 3.0,
        "full_price": 6.0,
        "tags": ["l30d:5.0"],
    }
    session = SessionLocal()
    try:
        store_snapshot(session, SHOP_A, SHOP_A_LABEL, "2026-08-12", _catalog([item]))
        store_snapshot(session, SHOP_A, SHOP_A_LABEL, "2026-08-13", _catalog([item]))
        session.commit()
    finally:
        session.close()

    from webapp import app

    with app.test_client() as c:
        body = c.get("/feed.xml").get_data(as_text=True)
    assert "Stale Deal" not in body


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


def test_drops_min_drop_pct_filters_out_smaller_drops(client, seeded_with_drop):
    # DROP_PRODUCT fell 25% -- a 30%+ floor must exclude it.
    body = client.get("/drops", query_string={"min_drop_pct": "30"}).get_data(as_text=True)
    assert DROP_PRODUCT not in body


def test_drops_min_drop_pct_keeps_matching_drops(client, seeded_with_drop):
    body = client.get("/drops", query_string={"min_drop_pct": "20"}).get_data(as_text=True)
    assert DROP_PRODUCT in body


def test_drops_sort_price_accepted_and_orders_cheapest_first(client, seeded_with_drop):
    resp = client.get("/drops", query_string={"sort": "price"})
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # STEADY_PRODUCT (2.0, unchanged) isn't a drop at all -- only
    # DROP_PRODUCT (7.5 now) should render regardless of sort order.
    assert DROP_PRODUCT in body
    assert STEADY_PRODUCT not in body


def test_drops_invalid_sort_falls_back_not_500(client, seeded_with_drop):
    resp = client.get("/drops", query_string={"sort": "bogus"})
    assert resp.status_code == 200


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


CDN = "https://cdn.e-food.gr/cdn-cgi/image"


def test_dashboard_renders_product_thumbnails_and_shop_logos(client, seeded):
    body = client.get("/").get_data(as_text=True)
    # Zero-price bug item (id 2 -> code-2) gets a CDN thumbnail...
    assert f"{CDN}/h=64,fit=cover,f=auto/restaurants/{SHOP_A}/menu_item/code-2" in body
    # ...and the shops table row gets the shop's logo.
    assert f"{CDN}/h=44,fit=cover,f=auto/shop/{SHOP_A}/logo" in body


def test_thumbnails_are_lazy_and_referrerless(client, seeded):
    body = client.get("/").get_data(as_text=True)
    assert 'loading="lazy"' in body
    assert 'referrerpolicy="no-referrer"' in body


def test_search_results_render_thumbnails(client, seeded):
    body = client.get("/search", query_string={"q": COMMON_PRODUCT}).get_data(as_text=True)
    assert f"restaurants/{SHOP_A}/menu_item/code-1" in body


def test_shop_page_renders_thumbnails(client, seeded):
    body = client.get(f"/shop/{SHOP_A}").get_data(as_text=True)
    assert f"restaurants/{SHOP_A}/menu_item/code-2" in body  # zero-price bug thumb
    assert f"shop/{SHOP_A}/logo" in body  # header logo


def test_base_includes_vendored_assets(client):
    body = client.get("/").get_data(as_text=True)
    assert "vendor/aos.js" in body
    assert "vendor/countUp.umd.js" in body
    assert "vendor/aos.css" in body


def test_page_theme_classes_on_single_purpose_pages(client):
    assert '<body class="theme-deals">' in client.get("/deals").get_data(as_text=True)
    assert '<body class="theme-deals">' in client.get("/drops").get_data(as_text=True)
    assert '<body class="theme-extremes">' in client.get("/extremes").get_data(as_text=True)


def test_page_theme_absent_on_mixed_content_dashboard(client):
    # The dashboard mixes bug/deal/drop sections (already color-coded
    # per-section) -- it must stay untheme'd rather than tint everything
    # one color and clash with its own panel-good/panel-bad sections.
    body = client.get("/").get_data(as_text=True)
    assert '<body class="">' in body


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


def test_download_bugs_csv(client, seeded):
    resp = client.get("/download/bugs.csv")
    assert resp.status_code == 200
    assert resp.mimetype == "text/csv"
    assert resp.headers["Content-Disposition"] == "attachment; filename=food_defects_bugs.csv"
    body = resp.get_data(as_text=True)
    assert "Μηδενική Τιμή" in body
    assert "zero_price" in body
    assert "Πλασματική Τιμή" in body
    assert "placeholder_reference" in body
    # A verified deal is not a "bug" -- it must not leak into this export.
    assert "Πραγματική Προσφορά" not in body


def test_download_bugs_csv_filtered_by_shop(client, seeded):
    resp = client.get("/download/bugs.csv", query_string={"shop_id": str(SHOP_B)})
    body = resp.get_data(as_text=True)
    assert "Μηδενική Τιμή" not in body  # that bug lives in shop A, not B


def test_download_drops_csv(client, seeded_with_drop):
    resp = client.get("/download/drops.csv")
    assert resp.status_code == 200
    assert resp.mimetype == "text/csv"
    assert resp.headers["Content-Disposition"] == "attachment; filename=food_defects_drops.csv"
    body = resp.get_data(as_text=True)
    assert DROP_PRODUCT in body
    assert STEADY_PRODUCT not in body  # unchanged price, not a drop


def test_download_drops_csv_min_drop_pct_filters(client, seeded_with_drop):
    # The seeded drop is 10.0 -> 7.5, a 25% fall.
    resp = client.get("/download/drops.csv", query_string={"min_drop_pct": "50"})
    assert DROP_PRODUCT not in resp.get_data(as_text=True)

    resp = client.get("/download/drops.csv", query_string={"min_drop_pct": "10"})
    assert DROP_PRODUCT in resp.get_data(as_text=True)


def test_deals_page_links_to_filtered_csv_export(client, seeded):
    resp = client.get("/deals", query_string={"shop": str(SHOP_A)})
    body = resp.get_data(as_text=True)
    assert f"/download/sales.csv?shop_id={SHOP_A}" in body


def test_drops_page_links_to_filtered_csv_export(client, seeded_with_drop):
    resp = client.get("/drops", query_string={"shop": str(SHOP_A)})
    body = resp.get_data(as_text=True)
    assert f"/download/drops.csv?shop_id={SHOP_A}" in body


def test_dashboard_links_to_bugs_csv_export(client, seeded):
    body = client.get("/").get_data(as_text=True)
    assert "/download/bugs.csv" in body


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


def test_item_page_live_error_is_plain_language_not_raw_exception(client, seeded, monkeypatch):
    # Regression: live_error used to be str(exc) -- a raw Python/requests
    # exception message shown straight to a non-technical operator on the
    # tool's core evidentiary page.
    def _raise(*_args, **_kwargs):
        raise RuntimeError("HTTPSConnectionPool(host='api.e-food.gr', port=443): Read timed out.")

    monkeypatch.setattr("webapp.fetch_menu_item", _raise)
    body = client.get(f"/item/{SHOP_A}/code-1").get_data(as_text=True)
    assert "HTTPSConnectionPool" not in body
    assert "Δεν ήταν δυνατή η ζωντανή επαλήθευση" in body


def test_item_page_shows_great_deal_badge(client, seeded, monkeypatch):
    # code-4 is the seeded "Πραγματική Προσφορά" (3.0 vs a 5.0 30-day low
    # = 40% off), which clears the 35% "great" tier threshold.
    monkeypatch.setattr(
        "webapp.fetch_menu_item",
        lambda shop_id, code, timeout=20: {
            "price": 3.0,
            "full_price": 6.0,
            "calculated_price": None,
            "is_available": True,
            "tags": [],
        },
    )
    body = client.get(f"/item/{SHOP_A}/code-4").get_data(as_text=True)
    assert "ΣΠΟΥΔΑΙΑ ΠΡΟΣΦΟΡΑ" in body


def _stub_live(monkeypatch, price=1.0):
    monkeypatch.setattr(
        "webapp.fetch_menu_item",
        lambda shop_id, code, timeout=20: {
            "price": price,
            "full_price": None,
            "calculated_price": None,
            "is_available": True,
            "tags": [],
        },
    )


def test_item_page_explains_zero_price_bug(client, seeded, monkeypatch):
    _stub_live(monkeypatch, price=0.0)
    body = client.get(f"/item/{SHOP_A}/code-2").get_data(as_text=True)  # Μηδενική Τιμή
    assert "Γιατί επισημάνθηκε" in body
    assert "τιμή ≤ 0,00 €" in body


def test_item_page_explains_placeholder_bug(client, seeded, monkeypatch):
    _stub_live(monkeypatch, price=2.0)
    body = client.get(f"/item/{SHOP_A}/code-3").get_data(as_text=True)  # Πλασματική Τιμή
    assert "κατώτατη 30 ημερών ≤ 0,05 €" in body
    assert "0,01 €" in body  # the seeded l30d value


def test_item_page_explains_verified_deal(client, seeded, monkeypatch):
    _stub_live(monkeypatch, price=3.0)
    body = client.get(f"/item/{SHOP_A}/code-4").get_data(as_text=True)  # Πραγματική Προσφορά
    assert "τουλάχιστον 20% κάτω" in body
    assert "40.0% έκπτωση" in body  # (5.0 - 3.0) / 5.0 * 100
    # This deal has no matched listing in another shop -- nothing to
    # compare against, so no "still pricier per unit" caveat.
    assert "Ακριβό ανά μονάδα" not in body


def test_item_page_flags_verified_deal_that_is_pricier_per_unit_elsewhere(
    client, seeded_with_misleading_deal, monkeypatch
):
    _stub_live(monkeypatch, price=5.0)
    body = client.get(f"/item/{SHOP_A}/code-1").get_data(as_text=True)
    assert "Ακριβό ανά μονάδα" in body
    assert SHOP_B_LABEL in body
    assert f"/item/{SHOP_B}/code-101" in body


def test_item_page_hides_badge_and_explains_deal_not_cheap_for_category(client, monkeypatch):
    # Same scenario as queries.test_get_deals_page_excludes_deal_not_cheap_for_its_category,
    # exercised through the item page: a real discount off its own
    # history, but still the priciest listing in its category -- the
    # ΣΠΟΥΔΑΙΑ/ΠΡΟΣΦΟΡΑ badge must not appear, and the disclosure must
    # say why instead of claiming "Προσφορά".
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
                        "Ice Cream Deal",
                        5.0,
                        full_price=16.0,
                        size_info="1kg",
                        tags=["l30d:8.0"],
                    ),
                    _item(2, "Peer A", 1.0, size_info="1kg"),
                    _item(3, "Peer B", 2.0, size_info="1kg"),
                    _item(4, "Peer C", 3.0, size_info="1kg"),
                ]
            ),
        )
        session.commit()
    finally:
        session.close()

    _stub_live(monkeypatch, price=5.0)
    body = client.get(f"/item/{SHOP_A}/code-1").get_data(as_text=True)
    assert "ΣΠΟΥΔΑΙΑ ΠΡΟΣΦΟΡΑ" not in body
    assert "Όχι πραγματικά φθηνό" in body


def test_item_page_no_disclosure_for_unflagged_item(client, seeded, monkeypatch):
    _stub_live(monkeypatch, price=1.68)
    body = client.get(f"/item/{SHOP_A}/code-1").get_data(as_text=True)  # Κοινό Προϊόν Δοκιμής
    assert "Γιατί επισημάνθηκε" not in body


def test_item_page_shows_all_time_low_high_from_history(client, monkeypatch):
    session = SessionLocal()
    try:
        for date_, price in [("2026-08-10", 2.00), ("2026-08-11", 1.00), ("2026-08-12", 3.00)]:
            store_snapshot(
                session,
                SHOP_A,
                SHOP_A_LABEL,
                date_,
                _catalog([_item(1, "Ιστορικό Προϊόν", price)]),
            )
        session.commit()
    finally:
        session.close()

    monkeypatch.setattr(
        "webapp.fetch_menu_item",
        lambda shop_id, code, timeout=20: {
            "price": 3.0,
            "full_price": None,
            "calculated_price": None,
            "is_available": True,
            "tags": [],
        },
    )
    body = client.get(f"/item/{SHOP_A}/code-1").get_data(as_text=True)
    assert "Χαμηλότερη τιμή ποτέ" in body
    assert "1,00 €" in body  # the real all-time low
    assert "Υψηλότερη τιμή ποτέ" in body
    assert "3,00 €" in body  # the real all-time high


def test_item_page_no_all_time_badge_with_single_snapshot(client, seeded, monkeypatch):
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
    body = client.get(f"/item/{SHOP_A}/code-1").get_data(as_text=True)
    assert "Χαμηλότερη τιμή ποτέ" not in body
    assert "Μέση τιμή" not in body


def test_item_page_shows_average_price_and_chart_series(client, monkeypatch):
    session = SessionLocal()
    try:
        for date_, price in [("2026-08-10", 2.00), ("2026-08-11", 1.00), ("2026-08-12", 3.00)]:
            store_snapshot(
                session,
                SHOP_A,
                SHOP_A_LABEL,
                date_,
                _catalog([_item(1, "Ιστορικό Προϊόν", price)]),
            )
        session.commit()
    finally:
        session.close()

    monkeypatch.setattr(
        "webapp.fetch_menu_item",
        lambda shop_id, code, timeout=20: {
            "price": 3.0,
            "full_price": None,
            "calculated_price": None,
            "is_available": True,
            "tags": [],
        },
    )
    body = client.get(f"/item/{SHOP_A}/code-1").get_data(as_text=True)
    assert "Μέση τιμή" in body
    assert "2,00 €" in body  # (2.00 + 1.00 + 3.00) / 3
    assert "chart-green" in body  # the average series actually rendered into the chart


def test_item_page_live_timeout_gets_specific_message(client, seeded, monkeypatch):
    def _raise(*_args, **_kwargs):
        raise requests.exceptions.Timeout("timed out")

    monkeypatch.setattr("webapp.fetch_menu_item", _raise)
    body = client.get(f"/item/{SHOP_A}/code-1").get_data(as_text=True)
    assert "δεν απάντησε έγκαιρα" in body


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


def test_refresh_and_csv_routes_declare_rate_limits():
    # Flask-Limiter's Limiter(enabled=False) -- required so the test
    # suite's own repeated /refresh and /download/sales.csv calls don't
    # 429 each other (see conftest.py) -- makes init_app() return before
    # registering anything, including the @limiter.limit(...) decorator's
    # own bookkeeping: there is no way to introspect the *real* app's
    # limits once built this way. Assert the decorators are still there
    # in source instead of at runtime; the mechanism itself is verified
    # in isolation by the next test.
    import inspect

    import webapp

    source = inspect.getsource(webapp)
    assert '@limiter.limit("5 per hour")\ndef refresh():' in source
    assert '@limiter.limit("20 per hour")\ndef download_sales_csv():' in source


def test_flask_limiter_enforces_per_hour_limit():
    # Proves the "N per hour" mechanism Flask-Limiter applies to
    # /refresh and /download/sales.csv actually works, using an
    # isolated throwaway app (Limiter(enabled=True), the real default)
    # rather than the shared, test-suite-disabled webapp singleton.
    from flask import Flask
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address

    app = Flask(__name__)
    limiter = Limiter(get_remote_address, app=app, storage_uri="memory://", default_limits=[])

    @app.route("/ping")
    @limiter.limit("5 per hour")
    def ping():
        return "ok"

    with app.test_client() as c:
        for _ in range(5):
            assert c.get("/ping").status_code == 200
        assert c.get("/ping").status_code == 429


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


def test_cheap_ranks_best_value_first(client):
    session = SessionLocal()
    try:
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            TODAY,
            _catalog(
                [
                    _item(1, "Mid Price", 2.0, size_info="1kg"),
                    _item(2, "Best Value", 1.0, size_info="1kg"),
                    _item(3, "Priciest", 3.0, size_info="1kg"),
                ]
            ),
        )
        session.commit()
    finally:
        session.close()

    body = client.get("/cheap").get_data(as_text=True)
    assert body.index("Best Value") < body.index("Mid Price") < body.index("Priciest")
    assert "-50% από τον μέσο όρο" in body  # Best Value: (0.20 - 0.10) / 0.20 * 100


def test_cheap_category_filter_narrows_results(client):
    session = SessionLocal()
    try:
        store_snapshot(
            session,
            SHOP_A,
            SHOP_A_LABEL,
            TODAY,
            _multi_catalog(
                {
                    "Cat A": [
                        _item(1, "A1", 1.0, size_info="1kg"),
                        _item(2, "A2", 2.0, size_info="1kg"),
                        _item(3, "A3", 3.0, size_info="1kg"),
                    ],
                    "Cat B": [
                        _item(4, "B1", 1.0, size_info="1kg"),
                        _item(5, "B2", 2.0, size_info="1kg"),
                        _item(6, "B3", 3.0, size_info="1kg"),
                    ],
                }
            ),
        )
        session.commit()
    finally:
        session.close()

    body = client.get("/cheap", query_string={"category": "Cat A"}).get_data(as_text=True)
    assert "A1" in body and "A2" in body and "A3" in body
    assert "B1" not in body and "B2" not in body and "B3" not in body


def test_cheap_guards_unfiltered_scan_past_threshold(client, seeded, monkeypatch):
    monkeypatch.setattr("webapp.CHEAP_SCAN_GUARD_ROWS", 1)
    resp = client.get("/cheap")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "πολύ μεγάλος για κατάταξη" in body
    assert COMMON_PRODUCT not in body


def test_cheap_guard_bypassed_by_category(client, seeded, monkeypatch):
    monkeypatch.setattr("webapp.CHEAP_SCAN_GUARD_ROWS", 1)
    resp = client.get("/cheap", query_string={"category": "Τρόφιμα"})
    body = resp.get_data(as_text=True)
    assert "πολύ μεγάλος για κατάταξη" not in body


def test_cheap_nav_link_present(client):
    body = client.get("/").get_data(as_text=True)
    assert 'href="/cheap"' in body
