"""Structuring: one payment split into several, to stay under the threshold.

The rule the business set — and the case brief with it — is a threshold on a
*payment*: over $10,000 and a person takes a second look. Applied to a document
it is trivially avoided by sending three documents, which is the oldest move in
accounts payable and the reason banks have a word for it.

This module supplies what the rule engine needs to apply that threshold to the
money rather than to the page: every other invoice the same vendor billed within
a few days of the one being decided. The policy lives in `rules.py`, beside the
threshold it is a rule about — the same split this project keeps between
`precedent.py` and the release decision.

It establishes a pattern and never an intent. Three invoices in one week may be
three deliveries, so nothing here rejects anything and nothing here forces a
human: the finding raises exactly the `requires_scrutiny` a single invoice of the
same size would have raised, which is the treatment the split was arranged to
avoid.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .config import Settings
from .models import Invoice, VendorWindow
from .precedent import vendor_key

if TYPE_CHECKING:
    from .runstore import RunStore


def lookup_vendor_window(store: RunStore, invoice: Invoice, settings: Settings) -> VendorWindow:
    """The same vendor's other invoices, close in time to this one.

    Three things have to be known before a window means anything: who is billing,
    how much, and when. Without a date there is nothing to be close *to*; without
    a total there is no sum to compare with the threshold, and that invoice is
    already headed for a person on the `missing_total` rule; without a vendor
    there is nobody whose invoices these are, and `missing_vendor` has already
    made it a rejection. Each of those returns an empty window rather than
    guessing, and an empty window changes no outcome.

    The invoice being decided is excluded by number, so a re-run or a revision
    can never find itself in its own window and clear the threshold alone.
    """
    days = settings.structuring_window_days
    if invoice.invoice_date is None or invoice.total is None or not invoice.vendor.strip():
        return VendorWindow(days=days)
    vendor = vendor_key(invoice.vendor)
    near = [
        rec
        for rec in store.invoices_dated_near(invoice.invoice_date, days)
        if vendor_key(rec.vendor) == vendor and rec.invoice_number != invoice.invoice_number
    ]
    # Chronological: the reason is read by a person, and "what else came in that
    # week" is a sequence, not a set.
    near.sort(key=lambda rec: (rec.invoice_date or invoice.invoice_date, rec.invoice_number))
    return VendorWindow(days=days, invoices=near)
