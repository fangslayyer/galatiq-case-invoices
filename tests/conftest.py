from pathlib import Path

import pytest

from invoiceflow.config import PROJECT_ROOT, Settings
from invoiceflow.db import Database
from invoiceflow.loaders import load_invoice_text
from invoiceflow.models import Invoice
from tests.fakes import FakeBrain

INVOICES_DIR = PROJECT_ROOT / "data" / "invoices"
#: Authored by us, unlike INVOICES_DIR, which is provided case material — the
#: learning demo's two vendor histories (data/demo/precedent/README.md).
DEMO_DIR = PROJECT_ROOT / "data" / "demo" / "precedent"
#: Likewise ours: one vendor's payment, split across three invoices
#: (data/demo/structuring/README.md).
STRUCTURING_DIR = PROJECT_ROOT / "data" / "demo" / "structuring"
EXTRACTIONS_DIR = Path(__file__).parent / "fixtures" / "extractions"
DOCUMENT_DIRS = (INVOICES_DIR, DEMO_DIR, STRUCTURING_DIR)


@pytest.fixture(scope="session")
def ground_truth() -> dict[str, Invoice]:
    """Recorded ground-truth extractions, keyed by exact raw document text —
    what the real LLM is expected to produce for each sample file."""
    mapping: dict[str, Invoice] = {}
    for directory in DOCUMENT_DIRS:
        for path in sorted(directory.iterdir()):
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
        uploads_dir=tmp_path / "uploads",
        # Never a thread in the test suite: it would outlive the test, keep
        # polling a tmp_path database pytest has deleted, and build a real
        # ChatXAI because .env exports XAI_API_KEY. The worker's behaviour is
        # tested through its synchronous half, inbox.drain().
        inbox_worker=False,
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
