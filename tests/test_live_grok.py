"""Smoke tests against the real xAI Grok API.

Deselected by default (`addopts = -m 'not live'`). Run with:
    XAI_API_KEY=... uv run pytest -m live --no-header
"""

import os

import pytest

from invoiceflow.config import Settings
from invoiceflow.db import Database
from invoiceflow.models import FinalStatus
from tests.conftest import INVOICES_DIR

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get("XAI_API_KEY"), reason="XAI_API_KEY not set"),
]


@pytest.fixture
def grok_pipeline(tmp_path):
    from invoiceflow.pipeline import Pipeline

    settings = Settings(
        llm_backend="grok", db_path=tmp_path / "inventory.db", results_dir=tmp_path / "results"
    )
    Database(settings.db_path).init()
    return Pipeline(settings)


def test_clean_invoice_is_paid(grok_pipeline):
    result = grok_pipeline.run(INVOICES_DIR / "invoice_1001.txt", persist=False)
    assert result.final_status == FinalStatus.PAID
    assert result.invoice.invoice_number == "INV-1001"


def test_messy_overstock_invoice_is_not_paid(grok_pipeline):
    result = grok_pipeline.run(INVOICES_DIR / "invoice_1002.txt", persist=False)
    # Grok phrasing may vary; the outcome must not: never pay an over-stock order
    assert result.final_status in (FinalStatus.REJECTED, FinalStatus.NEEDS_REVIEW)
    assert result.payment is None
    assert result.invoice.invoice_number == "INV-1002"
