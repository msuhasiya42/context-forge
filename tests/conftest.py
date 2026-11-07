from pathlib import Path

import pytest

# Resolved from this file, not the CWD, so the fixtures load wherever pytest is launched.
FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture
def fixture_csv() -> Path:
    return FIXTURES_DIR / "transactions_sep_oct.csv"


@pytest.fixture
def fixture_request() -> str:
    return (FIXTURES_DIR / "request.txt").read_text().strip()
