"""Human review: what a person's decision actually does to a run.

The dashboard collects the click; everything it *means* is decided here — what
gets paid, what status the run takes, and whether the action goes down as a
confirmation or an override. None of that is presentation, and when a copy of
it lived in `ui/app.py` it drifted from the pipeline: approving an amendment
paid nothing, recorded itself as a duplicate, and cleared the original
invoice's paid flag. Keeping it in one testable place is the fix.

A review never edits what the agents wrote. It lands as a `human_reviews` row
(plus a payment and a registry update where money moves) and the effective
status is derived from it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .models import FinalStatus, InvoiceRunResult, PaymentResult, PaymentStatus
from .payment import execute_payment

if TYPE_CHECKING:
    from .runstore import RunStore


@dataclass(frozen=True)
class ReviewOutcome:
    """What a human review did, or why it did nothing."""

    recorded: bool
    #: Populated only when `recorded`; the caller renders these, never derives them.
    action: str = ""
    to_status: FinalStatus | None = None
    payment: PaymentResult | None = None
    #: Why the review was refused. Empty when it landed.
    message: str = ""


def apply_human_review(
    store: RunStore,
    result: InvoiceRunResult,
    *,
    approve: bool,
    note: str = "",
    reviewer: str = "dashboard",
) -> ReviewOutcome:
    """Act on a finished run: pay it, reject it, or confirm what it already says.

    Confirming is not a no-op — it lands a `human_reviews` row, which is how an
    auto-rejection stops counting as something nobody has checked.
    """
    invoice, decision = result.invoice, result.decision
    if invoice is None or decision is None:
        # This writes to the payment registry, so the guard is enforced here
        # rather than trusting whichever caller decided to offer the button.
        return ReviewOutcome(
            recorded=False,
            message="This run has no extracted invoice or decision to act on.",
        )

    was = result.final_status
    payment: PaymentResult | None = None
    if approve:
        # Sends the *balance* when this invoice number has been settled before,
        # so approving a revision releases what is still owed rather than the
        # restated total — see payment.outstanding_balance.
        payment = execute_payment(store, invoice, result.run_id)
        if payment.status != PaymentStatus.SUCCESS:
            # Nothing moved, so nothing is approved. Recording it as settled
            # would close the run while money is still owed in one direction or
            # the other — which is the failure this path exists to prevent.
            return ReviewOutcome(
                recorded=False,
                payment=payment,
                message=(
                    f"Nothing is outstanding on {invoice.invoice_number}, so there is nothing "
                    f"to pay: the registry already holds a settled payment covering the "
                    f"${invoice.total or 0:,.2f} claimed here. If the vendor is owed *less* "
                    "than was already sent, that is a credit note to request — not a payment "
                    "this system can make."
                ),
            )
        to_status = FinalStatus.PAID
        store.add_payment(result.run_id, payment, currency=invoice.currency)
    else:
        to_status = FinalStatus.REJECTED

    # "confirm" when the human agreed with what the pipeline already decided,
    # an override when they changed it — the trail says which happened.
    if to_status == was:  # noqa: SIM108
        action = "confirm"
    else:
        action = "override_approve" if approve else "override_reject"
    store.add_human_review(
        result.run_id,
        action=action,
        from_status=was,
        to_status=to_status,
        note=note,
        reviewer=reviewer,
    )
    store.record_settlement(
        invoice.invoice_number,
        invoice.content_hash(),
        invoice.vendor,
        invoice.total,
        to_status.value,
        None,
    )
    return ReviewOutcome(recorded=True, action=action, to_status=to_status, payment=payment)
