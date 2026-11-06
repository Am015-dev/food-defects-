import csv
import gzip
import io

from efood_client import HEADERS, auth_headers
from snapshot_files import rows_for_shop, to_csv_gz


def _payload():
    def item(i, price, **kw):
        return {"id": i, "code": f"c{i}", "name": f"Item {i}", "price": price, **kw}

    return {"menu": {"categories": [{"name": "Γαλακτοκομικά", "items": [
        item(1, 0.0, tags=["l30d:2.00"]),                                   # zero price
        item(2, 1.50, tags=["l30d:0.01"]),                                  # placeholder l30d
        item(3, 1.00, full_price=2.00, tags=["l30d:2.00"], size_info="1 lt"),  # 50% verified deal
        item(4, 2.00, tags=["l30d:2.00"]),                                  # normal
    ]}]}}


def test_rows_flags_match_price_analysis_rules():
    rows = {r["code"]: r for r in rows_for_shop({"id": 7, "label": "Shop"}, _payload(), "2026-11-06")}
    assert rows["c1"]["zero_price_bug"] == 1
    assert rows["c2"]["placeholder_bug"] == 1
    assert rows["c3"]["deal_pct"] == 50.0
    assert rows["c4"]["zero_price_bug"] == 0 and rows["c4"]["deal_pct"] is None


def test_csv_round_trip():
    rows = rows_for_shop({"id": 7, "label": "Shop"}, _payload(), "2026-11-06")
    back = list(csv.DictReader(io.StringIO(gzip.decompress(to_csv_gz(rows)).decode())))
    assert len(back) == 4 and back[0]["name"] == "Item 1"


def test_auth_header_parsing():
    assert auth_headers("") == HEADERS
    assert auth_headers("Authorization: Bearer abc:def")["Authorization"] == "Bearer abc:def"
