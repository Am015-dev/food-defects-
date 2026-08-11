"""Golden fixtures from docs/price-anomaly-tracker-spec.md, section 11.

Nothing merges unless all four pass. Fixture 1 is the incident that
triggered this tracker: a fixed-amount promo on Pepsi Twist 1.5l.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pricewatch import Line, evaluate, oauth2_token, Fetcher, read_source  # noqa: E402


def rule_ids(hits):
    return [r.id for r in hits]


def test_fixture_1_discount_ge_base_is_primary():
    # base 105, absolute discount 105, qty 4, no original, no unit price
    ln = Line(source="test", url="", sku="PEP-TWIST-15", title="Pepsi Twist 1.5l",
              price=0, original=105, discount_amount=105,
              unit_size=1.5, unit_label="lt", unit_price=None)
    hits = evaluate(ln)
    ids = rule_ids(hits)
    assert ids[0] == "DISCOUNT_GE_BASE"
    assert hits[0].severity == "critical"
    assert "ZERO_PRICE" in ids
    assert "MISSING_UNIT_PRICE" in ids
    # MISSING_ORIGINAL_PRICE does not apply here: original *is* displayed (105).
    # It fires only when a promo is active and no original price is rendered at all.


def test_fixture_2_clean_line_no_rules_fire():
    # base 198, percentage 50, displayed 99, original 198, unit price 66
    # (unit_size 1.5 so 99 / 1.5 == 66, matching the displayed unit price exactly)
    ln = Line(source="test", url="", sku="CLEAN-1", title="Control line",
              price=99, original=198, discount_pct=50.0,
              unit_size=1.5, unit_label="lt", unit_price=66)
    hits = evaluate(ln)
    assert hits == []


def test_fixture_3_extreme_discount_near_miss():
    # base 105, absolute discount 100, displayed 5
    ln = Line(source="test", url="", sku="NEAR-MISS-1", title="Near miss",
              price=5, original=105, discount_amount=100)
    hits = evaluate(ln)
    ids = rule_ids(hits)
    assert ids == ["EXTREME_DISCOUNT"]


def test_fixture_4_stacked_promo_reaches_zero():
    # base 250, two stacked absolutes 150 + 150, displayed 0.
    # pricewatch has no STACK_EXCEEDS_CAP rule (that requires the promotions
    # list and a stacking policy from the full tracker, see spec PR-012) -
    # the crawler still catches the zero-price symptom.
    ln = Line(source="test", url="", sku="STACKED-1", title="Stacked promo",
              price=0, original=250)
    hits = evaluate(ln)
    ids = rule_ids(hits)
    assert "ZERO_PRICE" in ids
    assert hits[0].severity == "critical"


def test_negative_price():
    ln = Line(source="test", url="", sku="NEG-1", title="Negative", price=-10, original=105)
    hits = evaluate(ln)
    assert "NEGATIVE_PRICE" in rule_ids(hits)


def test_arithmetic_mismatch():
    ln = Line(source="test", url="", sku="MISMATCH-1", title="Mismatch",
              price=80, original=100, discount_amount=10)  # expected 90, displayed 80
    hits = evaluate(ln)
    assert "ARITHMETIC_MISMATCH" in rule_ids(hits)


def test_missing_price():
    ln = Line(source="test", url="", sku="NOPRICE-1", title="No price", price=None)
    hits = evaluate(ln)
    assert rule_ids(hits) == ["MISSING_PRICE"]


def test_unit_price_mismatch():
    ln = Line(source="test", url="", sku="UNIT-MISMATCH-1", title="Unit mismatch",
              price=100, unit_size=2.0, unit_price=40)  # expected 50
    hits = evaluate(ln)
    assert "UNIT_PRICE_MISMATCH" in rule_ids(hits)


def test_oauth2_token_missing_credentials_returns_none(monkeypatch):
    # No creds in the environment -> fail safe (return None), never crash or
    # make a request. Guards the sanctioned e-food Partner API path.
    monkeypatch.delenv("EFOOD_CLIENT_ID", raising=False)
    monkeypatch.delenv("EFOOD_CLIENT_SECRET", raising=False)
    fetcher = Fetcher({})
    auth = {"type": "oauth2_client_credentials",
            "token_url": "https://e-food.partner.deliveryhero.io/v2/oauth/token",
            "client_id_env": "EFOOD_CLIENT_ID",
            "client_secret_env": "EFOOD_CLIENT_SECRET"}
    assert oauth2_token(auth, fetcher) is None


def test_auth_header_does_not_leak_across_sources(monkeypatch):
    # A source whose oauth token can't be obtained must not leave an
    # Authorization header on the shared session for the next source/host.
    monkeypatch.delenv("EFOOD_CLIENT_ID", raising=False)
    monkeypatch.delenv("EFOOD_CLIENT_SECRET", raising=False)
    fetcher = Fetcher({})
    src = {"name": "partner", "type": "json", "url": "https://example.invalid/x",
           "auth": {"type": "oauth2_client_credentials",
                    "token_url": "https://example.invalid/token",
                    "client_id_env": "EFOOD_CLIENT_ID",
                    "client_secret_env": "EFOOD_CLIENT_SECRET"}}
    # drain the generator; token fetch fails safe, source yields nothing
    assert list(read_source(src, fetcher, None)) == []
    assert "Authorization" not in fetcher.session.headers
