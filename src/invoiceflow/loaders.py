"""File loaders: turn any supported invoice file into raw text.

Deliberately dumb — no interpretation happens here. Structured formats are
passed through verbatim so the Extractor agent (or the offline parser) sees
exactly what the vendor sent, messy or not.
"""

from __future__ import annotations

from pathlib import Path

SUPPORTED_EXTENSIONS = {".txt", ".json", ".csv", ".xml", ".pdf"}


class UnsupportedFormatError(ValueError):
    pass


def load_invoice_text(path: Path | str) -> str:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Invoice file not found: {path}")
    ext = path.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise UnsupportedFormatError(
            f"Unsupported invoice format '{ext}' (supported: {sorted(SUPPORTED_EXTENSIONS)})"
        )
    if ext == ".pdf":
        return _load_pdf(path)
    return path.read_text(encoding="utf-8", errors="replace")


def _load_pdf(path: Path) -> str:
    import pdfplumber  # local import: heavy dependency, only needed for PDFs

    with pdfplumber.open(path) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    if not text.strip():
        raise UnsupportedFormatError(f"No extractable text in PDF: {path}")
    return text
