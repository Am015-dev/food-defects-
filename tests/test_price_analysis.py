"""Tests for the pricing-anomaly rules in price_analysis.py."""

from price_analysis import (
    find_placeholder_reference_price_bugs,
    find_verified_deep_discounts,
    find_zero_price_bugs,
    l30d_price,
    load_items,
)


def item(id_, price=1.0, full_price=None, tags=None, **extra):
    d = {"id": id_, "name": f"Item {id_}", "price": price}
    if full_price is not None:
        d["full_price"] = full_price
    if tags is not None:
        d["tags"] = tags
    d.update(extra)
    return d


def test_load_items_flattens_categories_and_dedupes_by_id():
    data = {
        "menu": {
            "categories": [
                {"name": "Cat A", "items": [item(1), item(2)]},
                # item 1 also listed under a second category -- should
                # only appear once, keeping the first category seen.
                {"name": "Cat B", "items": [item(1), item(3)]},
            ]
        }
    }
    items = load_items(data)
    assert [it["id"] for it in items] == [1, 2, 3]
    assert next(it for it in items if it["id"] == 1)["_category"] == "Cat A"


def test_l30d_price_parses_tag():
    assert l30d_price(item(1, tags=["l30d:5.57"])) == 5.57


def test_l30d_price_missing_tag_returns_none():
    assert l30d_price(item(1, tags=["sold_by_weight"])) is None
    assert l30d_price(item(1)) is None


def test_l30d_price_malformed_tag_returns_none():
    assert l30d_price(item(1, tags=["l30d:not-a-number"])) is None


def test_find_zero_price_bugs():
    items = [item(1, price=0), item(2, price=1.0), item(3, price=0.0)]
    bugs = find_zero_price_bugs(items)
    assert {it["id"] for it in bugs} == {1, 3}


def test_find_placeholder_reference_price_bugs():
    items = [
        item(1, tags=["l30d:0.01"]),  # implausible near-zero placeholder
        item(2, tags=["l30d:5.57"]),  # a real reference price
        item(3),  # no tag at all
    ]
    bugs = find_placeholder_reference_price_bugs(items)
    assert [it["id"] for it in bugs] == [1]


def test_find_verified_deep_discounts_flags_genuine_discount():
    items = [item(1, price=3.0, full_price=6.0, tags=["l30d:5.0"])]
    results = find_verified_deep_discounts(items)
    assert len(results) == 1
    assert results[0]["item"]["id"] == 1
    assert results[0]["pct"] == 40.0  # (5 - 3) / 5 * 100


def test_find_verified_deep_discounts_requires_discount_badge():
    # price below l30d but full_price == price -- no actual badge shown.
    items = [item(1, price=3.0, full_price=3.0, tags=["l30d:5.0"])]
    assert find_verified_deep_discounts(items) == []


def test_find_verified_deep_discounts_excludes_sold_by_weight():
    items = [item(1, price=3.0, full_price=6.0, tags=["l30d:5.0", "sold_by_weight"])]
    assert find_verified_deep_discounts(items) == []


def test_find_verified_deep_discounts_respects_min_pct():
    # (5 - 4.5) / 5 * 100 == 10%, below the default 20% threshold.
    items = [item(1, price=4.5, full_price=6.0, tags=["l30d:5.0"])]
    assert find_verified_deep_discounts(items) == []
    assert find_verified_deep_discounts(items, min_pct=5.0) != []
