"""Tests for product_matching.py: the fuzzy cross-shop/cross-chain
product-identity matcher that replaced exact-name matching in
compare_across_shops / get_product_across_shops (queries.py)."""

import pytest

from db import SessionLocal
from product_matching import (
    MATCH_THRESHOLD,
    best_match,
    match_or_create_product,
    normalize_for_matching,
    top_level_category,
)

# ---------- normalize_for_matching ----------


NORMALIZE_CASES = [
    ("Anatoli Κουρκουμάς 60g", "anatoli κουρκουμασ"),
    ("Pedigree Markies Μπισκότα Γεμιστά με Μεδούλι 500g", "pedigree markies μπισκοτα γεμιστα με μεδουλι"),
    ("Νουνού Kid Γάλα Εβαπορέ 6x400g", "νουνου kid γαλα εβαπορε"),
    ("ΧΑΙΤΟΓΛΟΥ ΜΑΚΕΔΟΝΙΚΟΣ ΧΑΛΒΑΣ ΒΑΝΙΛΙΑ 16X40G", "χαιτογλου μακεδονικοσ χαλβασ βανιλια"),
    ("Χαρτί Υγείας 24 τεμ.", "χαρτι υγειασ"),
    ("Λάχανο Άσπρο Εγχώριο Α'", "λαχανο ασπρο εγχωριο α'"),  # no trailing size -- unchanged but folded
]


@pytest.mark.parametrize("name,expected", NORMALIZE_CASES)
def test_normalize_for_matching(name, expected):
    assert normalize_for_matching(name) == expected


def test_normalize_for_matching_empty_name():
    assert normalize_for_matching("") == ""
    assert normalize_for_matching(None) == ""


# ---------- top_level_category ----------


def test_top_level_category_splits_on_double_pipe():
    assert top_level_category("Τρόφιμα || Μπαχαρικά") == "Τρόφιμα"


def test_top_level_category_no_subcategory():
    assert top_level_category("Τρόφιμα") == "Τρόφιμα"


def test_top_level_category_none():
    assert top_level_category(None) is None
    assert top_level_category("") is None


# ---------- best_match (pure, no DB) ----------


def test_best_match_links_reordered_same_product():
    candidates = [(1, "Anatoli Κουρκουμάς 60g")]
    product_id, confidence = best_match("Κουρκουμάς Anatoli 100g", candidates)
    assert product_id == 1
    assert confidence >= MATCH_THRESHOLD / 100


def test_best_match_rejects_different_product_same_brand():
    candidates = [(1, "Pedigree Markies Μπισκότα Γεμιστά με Μεδούλι 500g")]
    product_id, confidence = best_match("Pedigree Ξηρά Τροφή Σκύλου 3kg", candidates)
    assert product_id is None
    assert confidence is None


def test_best_match_picks_the_highest_scoring_candidate():
    candidates = [
        (1, "Ζάχαρη Άχνη 500g"),
        (2, "Anatoli Κουρκουμάς 60g"),
    ]
    product_id, _ = best_match("Κουρκουμάς Anatoli 100g", candidates)
    assert product_id == 2


def test_best_match_empty_candidates():
    assert best_match("Anything", []) == (None, None)


# ---------- numeric-token guard ----------
#
# Regression tests for a real bug found at real-catalog scale: long,
# near-identical descriptive names with a single differing embedded
# number (fat %, diaper/shoe size, candle/balloon number) scored well
# above MATCH_THRESHOLD on pure text similarity, because that one
# significant token got diluted by the surrounding shared text -- e.g.
# "Pampers ... No1 2-5kg" wrongly linked to "Pampers ... No4 9-14kg".
# See MISTAKES.md.


def test_best_match_rejects_different_fat_percentage():
    candidates = [(1, "Δωδώνη Γιαούρτι Στραγγιστό Κλασικό 2% Λιπαρά 1kg")]
    product_id, confidence = best_match("Δωδώνη Γιαούρτι Στραγγιστό Κλασικό 8% Λιπαρά 1kg", candidates)
    assert product_id is None
    assert confidence is None


def test_best_match_rejects_different_size_number():
    candidates = [(1, "Relaxed Feet Πάτοι Παπουτσιών No37 1τεμ")]
    product_id, confidence = best_match("Relaxed Feet Πάτοι Παπουτσιών No45 1τεμ", candidates)
    assert product_id is None
    assert confidence is None


def test_best_match_rejects_different_variant_number():
    candidates = [(1, "Procos Decorata Party Gold Foil No 8 Μπαλόνι - Χρυσό 0")]
    product_id, confidence = best_match(
        "Procos Decorata Party Gold Foil No 2 Μπαλόνι - Χρυσό 0", candidates
    )
    assert product_id is None
    assert confidence is None


def test_best_match_allows_same_leftover_numbers():
    # The guard only rejects on a DIFFERENCE -- identical leftover
    # numbers (or none at all, the common case once the pack size is
    # stripped) must not block an otherwise-good match.
    candidates = [(1, "Δωδώνη Γιαούρτι Στραγγιστό Κλασικό 2% Λιπαρά 1kg")]
    product_id, confidence = best_match("Γιαούρτι Στραγγιστό Κλασικό 2% Λιπαρά Δωδώνη 1kg", candidates)
    assert product_id == 1
    assert confidence == 1.0


# ---------- match_or_create_product (DB-backed) ----------


def test_match_or_create_product_creates_new_on_first_item():
    session = SessionLocal()
    try:
        product_id, confidence = match_or_create_product(
            session, "Anatoli Κουρκουμάς 60g", "Τρόφιμα || Μπαχαρικά", {}
        )
        assert product_id is not None
        assert confidence == 1.0
    finally:
        session.close()


def test_match_or_create_product_links_similar_name_across_calls():
    session = SessionLocal()
    try:
        block_cache = {}
        first_id, _ = match_or_create_product(
            session, "Anatoli Κουρκουμάς 60g", "Τρόφιμα || Μπαχαρικά", block_cache
        )
        # Different chain's own subcategory phrasing, reordered words,
        # different size -- still the same real product.
        second_id, confidence = match_or_create_product(
            session, "Κουρκουμάς Anatoli 100g", "Τρόφιμα || Καρυκεύματα", block_cache
        )
        assert second_id == first_id
        assert confidence >= MATCH_THRESHOLD / 100
    finally:
        session.close()


def test_match_or_create_product_does_not_link_different_products():
    session = SessionLocal()
    try:
        block_cache = {}
        pasta_id, _ = match_or_create_product(
            session, "Ζυμαρικά Πένες 500g", "Τρόφιμα || Ζυμαρικά", block_cache
        )
        sugar_id, _ = match_or_create_product(session, "Ζάχαρη Άχνη 500g", "Τρόφιμα || Ζυμαρικά", block_cache)
        assert pasta_id != sugar_id
    finally:
        session.close()


def test_match_or_create_product_blocks_by_top_level_category():
    # Same core text, but genuinely different top-level categories --
    # blocking must keep them from ever being compared, even though the
    # text alone would score a perfect match.
    session = SessionLocal()
    try:
        block_cache = {}
        food_id, _ = match_or_create_product(session, "Χαρτί Υγείας", "Τρόφιμα", block_cache)
        household_id, _ = match_or_create_product(session, "Χαρτί Υγείας", "Είδη Σπιτιού", block_cache)
        assert food_id != household_id
    finally:
        session.close()


def test_match_or_create_product_new_product_visible_within_same_run():
    # A product created earlier in the same block_cache must be
    # matchable by a later call in the same run, without a fresh query --
    # this is what makes the ingest-time per-shop caching correct.
    session = SessionLocal()
    try:
        block_cache = {}
        first_id, _ = match_or_create_product(session, "Anatoli Κουρκουμάς 60g", "Τρόφιμα", block_cache)
        # Not committed yet -- still must be visible via block_cache.
        second_id, confidence = match_or_create_product(
            session, "Κουρκουμάς Anatoli 100g", "Τρόφιμα", block_cache
        )
        assert second_id == first_id
        assert confidence >= MATCH_THRESHOLD / 100
    finally:
        session.close()


def test_match_or_create_product_no_category():
    session = SessionLocal()
    try:
        product_id, confidence = match_or_create_product(session, "Ανώνυμο Προϊόν", None, {})
        assert product_id is not None
        assert confidence == 1.0
    finally:
        session.close()
