"""Unit tests for the deterministic extractor and its normalization helpers."""

from datetime import date

import pytest

from invoiceflow.loaders import load_invoice_text
from invoiceflow.offline import (
    canonicalize_item,
    extract_invoice,
    fix_ocr_digits,
    parse_date,
    parse_money,
)
from tests.conftest import INVOICES_DIR


def _extract(name: str):
    return extract_invoice(load_invoice_text(INVOICES_DIR / name))


class TestHelpers:
    def test_ocr_digit_fix(self):
        assert fix_ocr_digits("3,500.O0") == "3,500.00"
        assert fix_ocr_digits("2O26") == "2026"
        assert fix_ocr_digits("OCTOBER") == "OCTOBER"  # words untouched

    def test_parse_money(self):
        assert parse_money("$1,000.00") == 1000.0
        assert parse_money("$3,500.O0") == 3500.0
        assert parse_money("yesterday") is None

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("2026-01-15", date(2026, 1, 15)),
            ("Jan 30 2026", date(2026, 1, 30)),
            ("January 27, 2026", date(2026, 1, 27)),
            ("26-Jan-2O26", date(2026, 1, 26)),  # OCR fix inside a date
            ("01/28/2026", date(2026, 1, 28)),
            ("yesterday", None),
        ],
    )
    def test_parse_date(self, raw, expected):
        assert parse_date(raw) == expected

    def test_canonicalize_item(self):
        assert canonicalize_item("Widget A") == ("WidgetA", None)
        assert canonicalize_item("WidgetA (rush order)") == ("WidgetA", "rush order")
        assert canonicalize_item("MegaSprocket") == ("MegaSprocket", None)


class TestFormats:
    def test_clean_txt(self):
        inv = _extract("invoice_1001.txt")
        assert inv.invoice_number == "INV-1001"
        assert inv.vendor == "Widgets Inc."
        assert inv.total == 5000.0
        assert [(li.item, li.quantity) for li in inv.line_items] == [
            ("WidgetA", 10),
            ("WidgetB", 5),
        ]

    def test_typo_txt(self):
        inv = _extract("invoice_1002.txt")
        assert inv.invoice_number == "INV-1002"
        assert inv.vendor == "Gadgets Co."
        assert inv.line_items[0].model_dump(include={"item", "quantity", "unit_price"}) == {
            "item": "GadgetX",
            "quantity": 20,
            "unit_price": 750.0,
        }

    def test_unparseable_due_date_preserved(self):
        inv = _extract("invoice_1003.txt")
        assert inv.due_date is None
        assert inv.due_date_raw == "yesterday"

    def test_json_with_nested_vendor(self):
        inv = _extract("invoice_1004.json")
        assert inv.vendor == "Precision Parts Ltd."
        assert inv.tax_amount == 140.0

    def test_negative_values_kept(self):
        inv = _extract("invoice_1009.json")
        assert inv.vendor == ""
        assert inv.line_items[0].quantity == -5
        assert inv.total == -250.0

    def test_kv_csv(self):
        inv = _extract("invoice_1006.csv")
        assert inv.invoice_number == "INV-1006"
        assert len(inv.line_items) == 2

    def test_table_csv(self):
        inv = _extract("invoice_1007.csv")
        assert inv.subtotal == 14750.0
        assert inv.tax_amount == 885.0
        assert inv.total == 15525.0
        assert len(inv.line_items) == 3

    def test_xml_currency(self):
        inv = _extract("invoice_1014.xml")
        assert inv.currency == "EUR"
        assert inv.invoice_number == "INV-1014"

    def test_email_style_txt(self):
        inv = _extract("invoice_1008.txt")
        assert inv.vendor == "NoProd Industries"
        assert {li.item for li in inv.line_items} == {"SuperGizmo", "MegaSprocket"}

    def test_shipping_goes_to_extra_charges(self):
        inv = _extract("invoice_1010.txt")
        assert inv.extra_charges == 150.0
        assert inv.line_items[3].note == "rush order"

    def test_ocr_invoice(self):
        inv = _extract("invoice_1012.txt")
        assert inv.invoice_number == "INV-1012"
        assert inv.invoice_date == date(2026, 1, 26)
        # "$3,500.O0" fixed
        assert inv.line_items[1].line_total == 3500.0


class TestPdfParity:
    @pytest.mark.parametrize("num", ["1011", "1012", "1013"])
    def test_pdf_matches_text_source(self, num):
        pdf = _extract(f"invoice_{num}.pdf")
        src = _extract(f"invoice_{num}.{'json' if num == '1013' else 'txt'}")
        assert pdf.content_hash() == src.content_hash()
        assert pdf.invoice_number == src.invoice_number
