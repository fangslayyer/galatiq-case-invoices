"""Payment stage: the mock banking API plus an idempotency guard."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from .models import FinalStatus, Invoice, PaymentResult, PaymentStatus

if TYPE_CHECKING:
    from .runstore import ProcessedRecord, RunStore

log = logging.getLogger(__name__)


def mock_payment(vendor: str, amount: float) -> dict:
    """The mock banking API from the case brief."""
    print(f"Paid {amount} to {vendor}")
    return {"status": "success"}


class UnpayableInvoiceError(ValueError):
    """Raised when an invoice reaches payment without an amount to pay."""


def outstanding_balance(prior: ProcessedRecord | None, invoice: Invoice) -> float | None:
    """What is still owed on `invoice`, or None when nothing should be sent.

    The stated total and the amount owed are the same number only until an
    invoice number has been settled once. After that a revision is owed its
    *balance*: paying the restated total would send the original sum a second
    time. None covers both refusals — the identical document arriving twice,
    and a revision claiming no more than has already gone out (which is a
    credit note to ask for, not a payment to make).
    """
    if invoice.total is None:
        return None
    if prior is None or prior.final_status != FinalStatus.PAID:
        return invoice.total
    if prior.content_hash == invoice.content_hash():
        return None
    balance = round(invoice.total - (prior.total or 0.0), 2)
    return balance if balance > 0 else None


def execute_payment(store: RunStore, invoice: Invoice, run_id: str) -> PaymentResult:
    """Send whatever is still outstanding on an approved invoice.

    Deliberately not "pay the total": see `outstanding_balance`. The registry
    is the record of what has already gone out, so this is the one place that
    can tell a first payment from the balance on a revision.
    """
    if invoice.total is None:
        # Unreachable: a missing total sets must_review, and the edge into `pay`
        # refuses a forced outcome. Loud here rather than `or 0.0`, which would
        # pay $0.00 and record the invoice as settled.
        raise UnpayableInvoiceError(
            f"{invoice.invoice_number} has no total amount; it must never reach payment"
        )
    owed = outstanding_balance(store.get_processed(invoice.invoice_number), invoice)
    now = datetime.now(UTC).isoformat(timespec="seconds")
    if owed is None:
        log.warning("Refusing to pay %s: nothing is outstanding", invoice.invoice_number)
        return PaymentResult(
            status=PaymentStatus.SKIPPED_ALREADY_PAID,
            vendor=invoice.vendor,
            # The claim, not the movement: nothing moved. `amount` on a skipped
            # payment is what was asked for and declined.
            amount=invoice.total,
            reference=run_id,
            paid_at=now,
        )
    result = mock_payment(invoice.vendor, owed)
    return PaymentResult(
        # Validated against PaymentStatus here: an unrecognised status from the
        # banking API raises rather than flowing on as an unpaid "not success".
        status=result["status"],
        vendor=invoice.vendor,
        amount=owed,
        reference=run_id,
        paid_at=now,
    )
