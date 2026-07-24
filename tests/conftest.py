import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent.parent / "scraper" / "fixtures"


@pytest.fixture(autouse=True)
def code_default_settings(monkeypatch):
    """Pin every Settings field to its code default for the duration of a test.

    settings() is an lru_cached singleton loaded from `.env` — the DEPLOYED
    configuration. Without this fixture the suite inherits whatever feature
    flags production is running (TOD_ENABLED=true broke two tests this way:
    fixtures minutes apart got different λs, and a synthetic league that only
    plays 00:00–09:45 UTC met a 12:30 kickoff outside its Fourier support).
    Tests that need a flag flip monkeypatch it themselves; that setattr lands
    after this one, so it wins. The singleton's identity is preserved because
    modules import `settings` at module load.
    """
    from core.config import Settings, settings
    live, defaults = settings(), Settings(_env_file=None)
    for name in type(defaults).model_fields:
        monkeypatch.setattr(live, name, getattr(defaults, name))


@pytest.fixture(scope="session")
def results_raw() -> list[dict]:
    return json.loads(
        (FIXTURES / "gtleagues_results_2026-07-07_page0.json").read_text(encoding="utf-8")
    )


@pytest.fixture(scope="session")
def betpawa_raw() -> bytes:
    return (FIXTURES / "betpawa_gtleagues_2026-07-08.bin").read_bytes()


@pytest.fixture(scope="session")
def sofifa_html() -> str:
    return (FIXTURES / "sofifa_teams_page.html").read_text(encoding="utf-8")


@pytest.fixture
def db(tmp_path):
    from store.db import connect

    conn = connect(tmp_path / "test.db")
    yield conn
    conn.close()
