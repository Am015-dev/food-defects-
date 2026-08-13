"""Table-driven tests for price_utils.py, using real size_info and
metric_unit_description formats pulled from an actual e-food catalog
(see the audit that motivated this module)."""

import pytest

from price_utils import (
    derive_unit_price,
    fold_name,
    format_normalized_price,
    get_price_comparison_info,
    normalize_price,
    parse_metric_unit_price,
    parse_size_info,
)

PARSE_SIZE_INFO_CASES = [
    ("500g", (500.0, "weight_g")),
    ("1.5kg", (1500.0, "weight_g")),
    ("1.5l", (1500.0, "volume_ml")),
    ("500ml", (500.0, "volume_ml")),
    ("16x40g", (640.0, "weight_g")),
    ("6x2l", (12000.0, "volume_ml")),
    ("16pieces", (16.0, "count")),
    ("6pack", (6.0, "count")),
    ("45washes", (45.0, "count")),
    ("20sachets", (20.0, "count")),
    ("8rolls", (8.0, "count")),
    ("10capsules", (10.0, "count")),
    ("2 x 20pieces", (40.0, "count")),
    ("4 x 15pieces", (60.0, "count")),
    (None, (None, "unknown")),
    ("", (None, "unknown")),
    ("nonsense", (None, "unknown")),
]


@pytest.mark.parametrize("size_info,expected", PARSE_SIZE_INFO_CASES)
def test_parse_size_info(size_info, expected):
    assert parse_size_info(size_info) == expected


METRIC_UNIT_CASES = [
    ("17,00€ / kg", (17.0, "kg")),
    ("51,80€ / lt", (51.8, "lt")),
    ("8,98€ / τεμ.", (8.98, "τεμ")),
    ("0,56€ / καψ.", (0.56, "καψ")),
    ("0,19€ / m", (0.19, "m")),
    (None, (None, None)),
    ("", (None, None)),
    ("not a price", (None, None)),
]


@pytest.mark.parametrize("text,expected", METRIC_UNIT_CASES)
def test_parse_metric_unit_price(text, expected):
    assert parse_metric_unit_price(text) == expected


def test_normalize_price_weight():
    # 2.00 EUR for 500g -> 0.40 EUR/100g
    assert normalize_price(2.0, "500g") == pytest.approx(0.40)


def test_normalize_price_volume():
    assert normalize_price(2.0, "500ml") == pytest.approx(0.40)


def test_normalize_price_count():
    assert normalize_price(1.0, "16pieces") == pytest.approx(0.0625)


def test_normalize_price_unparseable_returns_none():
    assert normalize_price(2.0, None) is None
    assert normalize_price(2.0, "mystery") is None


def test_format_normalized_price_weight_label():
    assert format_normalized_price(0.40, unit="per_100g") == "0,40€/100g"


def test_format_normalized_price_volume_label_is_100ml_not_100g():
    # Regression: volumes were previously mislabeled €/100g.
    display = format_normalized_price(0.40, unit="auto", size_info="500ml")
    assert display == "0,40€/100ml"


def test_format_normalized_price_item_label_is_greek_not_english():
    # Regression: was previously the English word "piece".
    display = format_normalized_price(0.0625, unit="auto", size_info="16pieces")
    assert display == "0,06€/τεμ."


def test_format_normalized_price_does_not_mangle_trailing_period():
    # Regression: a blind .replace('.', ',') on the whole string turned
    # "τεμ." into "τεμ," because both the decimal point and the Greek
    # abbreviation's trailing dot are literal '.' characters.
    display = format_normalized_price(1.0, unit="per_item")
    assert display.endswith("τεμ.")
    assert "τεμ," not in display


def test_get_price_comparison_info_prefers_metric_unit_description():
    info = get_price_comparison_info(1.68, "500g", "3,36€ / kg")
    assert info["normalized_price"] == pytest.approx(3.36)
    assert info["normalized_display"] == "3,36€/kg"


def test_get_price_comparison_info_falls_back_to_size_info():
    info = get_price_comparison_info(2.0, "500ml", None)
    assert info["normalized_display"] == "0,40€/100ml"


def test_get_price_comparison_info_no_data():
    info = get_price_comparison_info(2.0, None, None)
    assert info["normalized_price"] is None
    assert info["normalized_display"] == "—"
    assert info["has_size_info"] is False


def test_derive_unit_price_prefers_metric_unit_description():
    assert derive_unit_price(1.68, "500g", "3,36€ / kg") == (3.36, "kg")


def test_derive_unit_price_falls_back_to_size_info():
    unit_price, unit_kind = derive_unit_price(2.0, "500ml", None)
    assert unit_price == pytest.approx(0.40)
    assert unit_kind == "100ml"


def test_derive_unit_price_none_when_nothing_parses():
    assert derive_unit_price(2.0, None, None) == (None, None)


FOLD_NAME_CASES = [
    ("Γάλα ΦΡΕΣΚΟ", "γαλα φρεσκο"),
    ("γάλα", "γαλα"),
    ("ΑΣ", "ασ"),
    (None, ""),
    ("", ""),
]


@pytest.mark.parametrize("name,expected", FOLD_NAME_CASES)
def test_fold_name(name, expected):
    assert fold_name(name) == expected


def test_fold_name_makes_unaccented_search_possible():
    # Regression: typing unaccented Greek previously matched nothing
    # against SQL ILIKE on the raw name.
    folded = fold_name("Γάλα Φρέσκο Πλήρες")
    assert "γαλα" in folded
    assert "φρεσκο" in folded
