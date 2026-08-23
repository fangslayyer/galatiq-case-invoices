"""Tests against the real xAI Grok API — extraction correctness lives here,
since the LLM is the only parser in the system.

Deselected by default (`addopts = -m 'not live'`). Run with:
    uv run pytest -m live --no-header

The key is read the same way the app reads it: XAI_API_KEY from the environment
or from .env.
"""

import pytest

from invoiceflow.config import Settings
from invoiceflow.db import Database
from invoiceflow.models import FinalStatus
from tests.conftest import INVOICES_DIR

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not Settings().resolve_api_key(), reason="XAI_API_KEY not set"),
]


@pytest.fixture
def grok_pipeline(tmp_path):
    from invoiceflow.pipeline import Pipeline

    settings = Settings(
        db_path=tmp_path / "inventory.db",
        runs_db_path=tmp_path / "invoiceflow.db",
        results_dir=tmp_path / "results",
    )
    Database(settings.db_path).init()
    return Pipeline(settings)


def test_clean_invoice_is_paid(grok_pipeline):
    result = grok_pipeline.run(INVOICES_DIR / "invoice_1001.txt")
    assert result.final_status == FinalStatus.PAID
    assert result.invoice.invoice_number == "INV-1001"


def test_messy_overstock_invoice_is_not_paid(grok_pipeline):
    result = grok_pipeline.run(INVOICES_DIR / "invoice_1002.txt")
    # Grok phrasing may vary; the outcome must not: never pay an over-stock order
    assert result.final_status in (FinalStatus.REJECTED, FinalStatus.NEEDS_REVIEW)
    assert result.payment is None
    assert result.invoice.invoice_number == "INV-1002"


def test_ocr_artifacts_are_understood(grok_pipeline):
    """invoice_1012 has 'INV 1012', '2O26', '$3,500.O0', 'Widget A' spacing —
    exactly the messiness the LLM must normalize into structured data."""
    result = grok_pipeline.run(INVOICES_DIR / "invoice_1012.txt")
    inv = result.invoice
    assert inv.invoice_number == "INV-1012"
    assert {li.item for li in inv.line_items} == {"WidgetA", "WidgetB", "GadgetX"}
    assert inv.total == 9975.0
    assert result.final_status == FinalStatus.PAID


def test_corrupt_invoice_is_rejected(grok_pipeline):
    result = grok_pipeline.run(INVOICES_DIR / "invoice_1009.json")
    assert result.final_status == FinalStatus.REJECTED
    assert result.payment is None
    # negative quantity must be preserved as evidence, not "fixed" by the LLM
    assert any(li.quantity < 0 for li in result.invoice.line_items)
