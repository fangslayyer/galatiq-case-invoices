import pytest

from invoiceflow.loaders import UnsupportedFormatError, load_invoice_text
from invoiceflow.models import PaymentStatus
from invoiceflow.payment import UnpayableInvoiceError, execute_payment
from tests.conftest import INVOICES_DIR
from tests.test_validation import make_invoice


class TestLoaders:
    def test_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_invoice_text(tmp_path / "ghost.txt")

    def test_unsupported_extension(self, tmp_path):
        weird = tmp_path / "invoice.docx"
        weird.write_text("hello")
        with pytest.raises(UnsupportedFormatError):
            load_invoice_text(weird)

    def test_pdf_extraction(self):
        text = load_invoice_text(INVOICES_DIR / "invoice_1011.pdf")
        assert "INV-1011" in text
        assert "Summit Manufacturing" in text


class TestPaymentIdempotency:
    def test_pays_unseen_invoice(self, store, capsys):
        result = execute_payment(store, make_invoice(), "run-1")
        assert result.status == PaymentStatus.SUCCESS
        assert result.paid_at
        assert "Paid 500.0 to Test Vendor" in capsys.readouterr().out

    def test_refuses_an_invoice_with_no_total(self, store, capsys):
        # Unreachable through the graph, but it must fail loudly rather than
        # pay $0.00 and record the invoice as settled.
        with pytest.raises(UnpayableInvoiceError):
            execute_payment(store, make_invoice(total=None), "run-x")
        assert "Paid" not in capsys.readouterr().out

    def test_revision_pays_only_the_balance(self, store, capsys):
        """The bug a dashboard approval shipped: an amendment approved after
        the original was paid sent the *restated total*, paying the original
        sum a second time."""
        original = make_invoice(total=1_890.0)
        store.record_processed(
            original.invoice_number, original.content_hash(), original.vendor, 1_890.0, "paid", None
        )
        revised = make_invoice(total=5_940.0)  # different content, same number
        result = execute_payment(store, revised, "run-rev")
        assert result.status == PaymentStatus.SUCCESS
        assert result.amount == 4_050.0  # 5,940 claimed - 1,890 already sent
        assert "Paid 4050.0 to Test Vendor" in capsys.readouterr().out

    def test_revision_below_what_was_paid_sends_nothing(self, store, capsys):
        # A negative balance is a credit note to request, never a payment to
        # make — and certainly not a negative one handed to the banking API.
        store.record_processed("INV-9999", "original-hash", "Test Vendor", 1_890.0, "paid", None)
        result = execute_payment(store, make_invoice(total=1_200.0), "run-down")
        assert result.status == PaymentStatus.SKIPPED_ALREADY_PAID
        assert "Paid" not in capsys.readouterr().out

    def test_revision_of_an_unpaid_invoice_pays_in_full(self, store, capsys):
        # Nothing was ever sent, so there is no balance to net off.
        store.record_processed(
            "INV-9999", "original-hash", "Test Vendor", 1_890.0, "rejected", None
        )
        result = execute_payment(store, make_invoice(total=500.0), "run-fresh")
        assert result.status == PaymentStatus.SUCCESS
        assert result.amount == 500.0
        assert "Paid 500.0 to Test Vendor" in capsys.readouterr().out

    def test_refuses_double_payment(self, store, capsys):
        inv = make_invoice()
        store.record_processed(
            inv.invoice_number, inv.content_hash(), inv.vendor, inv.total, "paid", None
        )
        result = execute_payment(store, inv, "run-2")
        assert result.status == PaymentStatus.SKIPPED_ALREADY_PAID
        assert "Paid" not in capsys.readouterr().out
