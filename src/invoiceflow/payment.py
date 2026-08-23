"""Payment stage: the mock banking API plus an idempotency guard."""

from __future__ import annotations

import logging

from .db import Database
from .models import FinalStatus, Invoice, PaymentResult, PaymentStatus

log = logging.getLogger(__name__)


def mock_payment(vendor: str, amount: float) -> dict:
    """The mock banking API from the case brief."""
    print(f"Paid {amount} to {vendor}")
    return {"status": "success"}


class UnpayableInvoiceError(ValueError):
    """Raised when an invoice reaches payment without an amount to pay."""


def execute_payment(db: Database, invoice: Invoice, run_id: str) -> PaymentResult:
    """Pay an approved invoice — unless the registry says it was already paid."""
    if invoice.total is None:
        # Unreachable: a missing total sets must_review, and the edge into `pay`
        # refuses a forced outcome. Loud here rather than `or 0.0`, which would
        # pay $0.00 and record the invoice as settled.
        raise UnpayableInvoiceError(
            f"{invoice.invoice_number} has no total amount; it must never reach payment"
        )
    prior = db.get_processed(invoice.invoice_number)
    if prior is not None and prior.final_status == FinalStatus.PAID:
        log.warning("Refusing to double-pay %s (already paid)", invoice.invoice_number)
        return PaymentResult(
            status=PaymentStatus.SKIPPED_ALREADY_PAID,
            vendor=invoice.vendor,
            amount=invoice.total,
            reference=run_id,
        )
    result = mock_payment(invoice.vendor, invoice.total)
    return PaymentResult(
        # Validated against PaymentStatus here: an unrecognised status from the
        # banking API raises rather than flowing on as an unpaid "not success".
        status=result["status"],
        vendor=invoice.vendor,
        amount=invoice.total,
        reference=run_id,
    )
