from pathlib import Path

import pytest

from invoiceflow.config import PROJECT_ROOT, Settings
from invoiceflow.db import Database
from invoiceflow.loaders import load_invoice_text
from invoiceflow.models import Invoice
from tests.fakes import FakeBrain

INVOICES_DIR = PROJECT_ROOT / "data" / "invoices"
EXTRACTIONS_DIR = Path(__file__).parent / "fixtures" / "extractions"


@pytest.fixture(scope="session")
def ground_truth() -> dict[str, Invoice]:
    """Recorded ground-truth extractions, keyed by exact raw document text —
    what the real LLM is expected to produce for each sample file."""
    mapping: dict[str, Invoice] = {}
    for path in sorted(INVOICES_DIR.iterdir()):
        fixture = EXTRACTIONS_DIR / f"{path.name}.json"
        if fixture.exists():
            mapping[load_invoice_text(path)] = Invoice.model_validate_json(fixture.read_text())
    return mapping


@pytest.fixture
def fake_brain(ground_truth) -> FakeBrain:
    return FakeBrain(extractions=ground_truth)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "inventory.db",
        runs_db_path=tmp_path / "invoiceflow.db",
        results_dir=tmp_path / "results",
    )


@pytest.fixture
def db(settings: Settings) -> Database:
    database = Database(settings.db_path)
    database.init()
    return database


@pytest.fixture
def store(settings: Settings):
    from invoiceflow.runstore import RunStore

    return RunStore(settings.runs_db_path)


@pytest.fixture
def pipeline(settings: Settings, db: Database, fake_brain: FakeBrain):
    from invoiceflow.pipeline import Pipeline

    return Pipeline(settings, llm=fake_brain)
