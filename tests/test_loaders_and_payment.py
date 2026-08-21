import pytest

from invoiceflow.loaders import UnsupportedFormatError, load_invoice_text
from invoiceflow.payment import execute_payment
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
    def test_pays_unseen_invoice(self, db, capsys):
        result = execute_payment(db, make_invoice(), "run-1")
        assert result.status == "success"
        assert "Paid 500.0 to Test Vendor" in capsys.readouterr().out

    def test_refuses_double_payment(self, db, capsys):
        inv = make_invoice()
        db.record_processed(
            inv.invoice_number, inv.content_hash(), inv.vendor, inv.total, "paid", "r1"
        )
        result = execute_payment(db, inv, "run-2")
        assert result.status == "skipped_already_paid"
        assert "Paid" not in capsys.readouterr().out
