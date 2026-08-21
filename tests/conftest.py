from pathlib import Path

import pytest

from invoiceflow.config import PROJECT_ROOT, Settings
from invoiceflow.db import Database

INVOICES_DIR = PROJECT_ROOT / "data" / "invoices"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        llm_backend="stub",
        db_path=tmp_path / "inventory.db",
        results_dir=tmp_path / "results",
    )


@pytest.fixture
def db(settings: Settings) -> Database:
    database = Database(settings.db_path)
    database.init()
    return database


@pytest.fixture
def pipeline(settings: Settings, db: Database):
    from invoiceflow.pipeline import Pipeline

    return Pipeline(settings)
