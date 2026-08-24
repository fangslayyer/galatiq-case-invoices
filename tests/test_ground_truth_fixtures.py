"""Sanity checks on the recorded ground-truth extractions.

These fixtures document what the real LLM is expected to produce for each
sample document; the offline suite depends on them, so keep them honest:
every sample file must have one, and the properties the pipeline relies on
(cross-format hashing, preserved evidence) must hold.
"""

from invoiceflow.loaders import SUPPORTED_EXTENSIONS
from tests.conftest import DOCUMENT_DIRS, EXTRACTIONS_DIR


def load(name: str):
    from invoiceflow.models import Invoice

    return Invoice.model_validate_json((EXTRACTIONS_DIR / f"{name}.json").read_text())


def test_every_sample_file_has_a_recorded_extraction():
    samples = {
        p.name
        for d in DOCUMENT_DIRS
        for p in d.iterdir()
        if p.suffix.lower() in SUPPORTED_EXTENSIONS
    }
    recorded = {p.name.removesuffix(".json") for p in EXTRACTIONS_DIR.glob("*.json")}
    assert samples == recorded


def test_same_invoice_across_formats_shares_a_content_hash():
    # duplicate detection across formats relies on this
    for num, alt in [("1011", "txt"), ("1012", "txt"), ("1013", "json")]:
        pdf = load(f"invoice_{num}.pdf")
        src = load(f"invoice_{num}.{alt}")
        assert pdf.content_hash() == src.content_hash()


def test_evidence_is_preserved_not_fixed():
    # the extractor's contract: represent faithfully, don't repair business data
    corrupt = load("invoice_1009.json")
    assert corrupt.line_items[0].quantity == -5
    assert corrupt.total == -250.0
    fraud = load("invoice_1003.txt")
    assert fraud.due_date is None
    assert fraud.due_date_raw == "yesterday"


def test_ocr_and_naming_are_normalized():
    # ...while representation problems are cleaned up
    ocr = load("invoice_1012.txt")
    assert ocr.invoice_number == "INV-1012"
    assert ocr.line_items[1].line_total == 3500.0  # was "$3,500.O0"
    assert {li.item for li in ocr.line_items} == {"WidgetA", "WidgetB", "GadgetX"}
