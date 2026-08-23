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

    def test_refuses_double_payment(self, store, capsys):
        inv = make_invoice()
        store.record_processed(
            inv.invoice_number, inv.content_hash(), inv.vendor, inv.total, "paid", None
        )
        result = execute_payment(store, inv, "run-2")
        assert result.status == PaymentStatus.SKIPPED_ALREADY_PAID
        assert "Paid" not in capsys.readouterr().out
