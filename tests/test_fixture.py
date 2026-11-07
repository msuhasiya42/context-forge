import csv
from pathlib import Path


def test_fixture_shape_and_planted_pairs(fixture_csv: Path) -> None:
    with fixture_csv.open(newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 60
    by_id = {int(r["row_id"]): r for r in rows}
    assert by_id[12]["amount"] == by_id[13]["amount"] == "25000.00"
    assert {by_id[12]["direction"], by_id[13]["direction"]} == {"DEBIT", "CREDIT"}
    assert by_id[40]["amount"] == by_id[41]["amount"] == "15000.00"
    assert by_id[50]["description"] == "HDFC card bill payment"
    assert by_id[51]["account_id"] == "ACC-CARD-HDFC"
    netflix = [r for r in rows if r["merchant"] == "Netflix"]
    assert {r["txn_date"][:7] for r in netflix} == {"2026-09", "2026-10"}


def test_fixture_request_is_the_spec_request(fixture_request: str) -> None:
    assert fixture_request.startswith("Compare October spending with September;")
    assert fixture_request.endswith("as expenses.")
