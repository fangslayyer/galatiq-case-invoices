"""Payment stage: the mock banking API plus an idempotency guard."""

from __future__ import annotations

import logging

from .db import Database
from .models import Invoice, PaymentResult

log = logging.getLogger(__name__)


def mock_payment(vendor: str, amount: float) -> dict:
    """The mock banking API from the case brief."""
    print(f"Paid {amount} to {vendor}")
    return {"status": "success"}


def execute_payment(db: Database, invoice: Invoice, run_id: str) -> PaymentResult:
    """Pay an approved invoice — unless the registry says it was already paid."""
    prior = db.get_processed(invoice.invoice_number)
    if prior is not None and prior.final_status == "paid":
        log.warning("Refusing to double-pay %s (already paid)", invoice.invoice_number)
        return PaymentResult(
            status="skipped_already_paid",
            vendor=invoice.vendor,
            amount=invoice.total or 0.0,
            reference=run_id,
        )
    result = mock_payment(invoice.vendor, invoice.total or 0.0)
    return PaymentResult(
        status=result["status"],
        vendor=invoice.vendor,
        amount=invoice.total or 0.0,
        reference=run_id,
    )
