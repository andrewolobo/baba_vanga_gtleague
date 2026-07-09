import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent.parent / "scraper" / "fixtures"


@pytest.fixture(scope="session")
def results_raw() -> list[dict]:
    return json.loads(
        (FIXTURES / "gtleagues_results_2026-07-07_page0.json").read_text(encoding="utf-8")
    )


@pytest.fixture(scope="session")
def betpawa_raw() -> bytes:
    return (FIXTURES / "betpawa_gtleagues_2026-07-08.bin").read_bytes()


@pytest.fixture
def db(tmp_path):
    from store.db import connect

    conn = connect(tmp_path / "test.db")
    yield conn
    conn.close()
