"""Deterministic, rule-based invoice extraction — the offline twin of the
Extractor agent.

Used by the stub LLM backend so the whole pipeline (and the test suite) runs
without an API key or network. Handles the same normalization the real agent
is prompted for: item-name canonicalization ("Widget A" -> "WidgetA"), OCR
digit fixes ("$3,500.O0", "2O26"), and multi-format date parsing.
"""

from __future__ import annotations

import csv
import io
import json
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime

from .models import Invoice, LineItem

_DATE_FORMATS = (
    "%Y-%m-%d",
    "%d-%b-%Y",
    "%b %d %Y",
    "%b %d, %Y",
    "%B %d %Y",
    "%B %d, %Y",
    "%m/%d/%Y",
)


def fix_ocr_digits(token: str) -> str:
    """Replace letter O with zero inside otherwise-numeric tokens."""
    if re.search(r"\d", token):
        return re.sub(r"(?<=[\d.,])O|O(?=[\d.,])", "0", token)
    return token


def parse_money(raw: str | None) -> float | None:
    if raw is None:
        return None
    cleaned = fix_ocr_digits(raw.strip()).replace("$", "").replace(",", "").rstrip(".")
    if not cleaned or cleaned in {"-", "."}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    cleaned = fix_ocr_digits(raw.strip().rstrip(".")).replace("  ", " ")
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def canonicalize_item(name: str) -> tuple[str, str | None]:
    """Return (canonical_name, note). Strips parenthetical notes and collapses
    internal spaces: 'WidgetA (rush order)' -> ('WidgetA', 'rush order'),
    'Widget A' -> ('WidgetA', None)."""
    note = None
    m = re.match(r"^(.*?)\s*\(([^)]*)\)\s*$", name.strip())
    if m:
        name, note = m.group(1), m.group(2)
    return re.sub(r"\s+", "", name.strip()), note


def extract_invoice(raw_text: str) -> Invoice:
    """Parse raw invoice text of any supported shape into a structured Invoice."""
    stripped = raw_text.lstrip()
    if stripped.startswith("{"):
        return _from_json(raw_text)
    if stripped.startswith("<?xml") or stripped.startswith("<invoice"):
        return _from_xml(raw_text)
    if _looks_like_csv(stripped):
        return _from_csv(raw_text)
    return _from_text(raw_text)


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


def _from_json(raw: str) -> Invoice:
    data = json.loads(raw)
    vendor = data.get("vendor") or ""
    if isinstance(vendor, dict):
        vendor = vendor.get("name") or ""
    items = [
        LineItem(
            item=canonicalize_item(str(li.get("item", "")))[0],
            quantity=int(li.get("quantity", 0)),
            unit_price=li.get("unit_price"),
            line_total=li.get("amount"),
            note=li.get("note"),
        )
        for li in data.get("line_items", [])
    ]
    notes = data.get("notes", "")
    if data.get("revision"):
        notes = f"Revision {data['revision']}. {notes}".strip()
    return Invoice(
        invoice_number=_normalize_number(str(data.get("invoice_number", ""))),
        vendor=vendor,
        invoice_date=parse_date(data.get("date")),
        due_date=parse_date(data.get("due_date")),
        due_date_raw=None if parse_date(data.get("due_date")) else data.get("due_date"),
        line_items=items,
        subtotal=data.get("subtotal"),
        tax_amount=data.get("tax_amount"),
        total=data.get("total"),
        currency=data.get("currency", "USD"),
        payment_terms=data.get("payment_terms") or "",
        notes=notes,
    )


# ---------------------------------------------------------------------------
# XML
# ---------------------------------------------------------------------------


def _xml_text(node: ET.Element | None) -> str | None:
    return node.text.strip() if node is not None and node.text else None


def _from_xml(raw: str) -> Invoice:
    root = ET.fromstring(raw)
    header, totals = root.find("header"), root.find("totals")
    items = [
        LineItem(
            item=canonicalize_item(_xml_text(li.find("name")) or "")[0],
            quantity=int(_xml_text(li.find("quantity")) or 0),
            unit_price=parse_money(_xml_text(li.find("unit_price"))),
        )
        for li in root.iter("item")
    ]
    get = lambda tag: _xml_text(header.find(tag)) if header is not None else None  # noqa: E731
    return Invoice(
        invoice_number=_normalize_number(get("invoice_number") or ""),
        vendor=get("vendor") or "",
        invoice_date=parse_date(get("date")),
        due_date=parse_date(get("due_date")),
        line_items=items,
        subtotal=parse_money(_xml_text(totals.find("subtotal"))) if totals is not None else None,
        tax_amount=parse_money(_xml_text(totals.find("tax_amount")))
        if totals is not None
        else None,
        total=parse_money(_xml_text(totals.find("total"))) if totals is not None else None,
        currency=get("currency") or "USD",
        payment_terms=_xml_text(root.find("payment_terms")) or "",
    )


# ---------------------------------------------------------------------------
# CSV (two shapes: key/value rows, and one-row-per-line-item tables)
# ---------------------------------------------------------------------------


def _looks_like_csv(stripped: str) -> bool:
    first = stripped.splitlines()[0] if stripped.splitlines() else ""
    return first.count(",") >= 1 and not first.lower().startswith(("invoice\n", "from:"))


def _from_csv(raw: str) -> Invoice:
    rows = list(csv.reader(io.StringIO(raw)))
    header = [c.strip().lower() for c in rows[0]] if rows else []
    if header[:2] == ["field", "value"]:
        return _from_kv_csv(rows[1:])
    return _from_table_csv(header, rows[1:])


def _from_kv_csv(rows: list[list[str]]) -> Invoice:
    fields: dict[str, str] = {}
    items: list[LineItem] = []
    for row in rows:
        if len(row) < 2:
            continue
        key, value = row[0].strip().lower(), row[1].strip()
        if key == "item":
            items.append(LineItem(item=canonicalize_item(value)[0], quantity=0))
        elif key == "quantity" and items:
            items[-1].quantity = int(value)
        elif key == "unit_price" and items:
            items[-1].unit_price = parse_money(value)
        else:
            fields[key] = value
    return Invoice(
        invoice_number=_normalize_number(fields.get("invoice_number", "")),
        vendor=fields.get("vendor", ""),
        invoice_date=parse_date(fields.get("date")),
        due_date=parse_date(fields.get("due_date")),
        line_items=items,
        subtotal=parse_money(fields.get("subtotal")),
        tax_amount=parse_money(fields.get("tax")),
        total=parse_money(fields.get("total")),
        payment_terms=fields.get("payment_terms", ""),
    )


def _from_table_csv(header: list[str], rows: list[list[str]]) -> Invoice:
    col = {name: i for i, name in enumerate(header)}
    items: list[LineItem] = []
    fields: dict[str, str] = {}
    subtotal = tax = total = None
    for row in rows:
        if not any(c.strip() for c in row):
            continue
        if row[0].strip():  # data row
            fields.setdefault("invoice_number", row[col.get("invoice number", 0)])
            fields.setdefault("vendor", row[col.get("vendor", 1)])
            fields.setdefault("date", row[col.get("date", 2)])
            fields.setdefault("due_date", row[col.get("due date", 3)])
            items.append(
                LineItem(
                    item=canonicalize_item(row[col.get("item", 4)])[0],
                    quantity=int(row[col.get("qty", 5)]),
                    unit_price=parse_money(row[col.get("unit price", 6)]),
                    line_total=parse_money(row[col.get("line total", 7)]),
                )
            )
        else:  # trailer row, e.g. ,,,,,,Subtotal:,14750.00
            cells = [c.strip() for c in row if c.strip()]
            if len(cells) >= 2:
                label, value = cells[-2].lower(), parse_money(cells[-1])
                if "subtotal" in label:
                    subtotal = value
                elif "tax" in label:
                    tax = value
                elif "total" in label:
                    total = value
    return Invoice(
        invoice_number=_normalize_number(fields.get("invoice_number", "")),
        vendor=fields.get("vendor", ""),
        invoice_date=parse_date(fields.get("date")),
        due_date=parse_date(fields.get("due_date")),
        line_items=items,
        subtotal=subtotal,
        tax_amount=tax,
        total=total,
    )


# ---------------------------------------------------------------------------
# Free text (clean, typo-ridden, tabular, and email-style layouts)
# ---------------------------------------------------------------------------

_FIELD_PATTERNS: dict[str, list[str]] = {
    "invoice_number": [
        r"invoice number\s*:\s*(\S+)",
        r"invoice\s*:\s*(INV[- ]?\d+)",
        r"inv\s*(?:no|#)?\s*[:.]?\s*(INV[- ]?\d+|\d+)",
        r"invoice\s*#\s*(INV-\d+)",
    ],
    "vendor": [
        # stop before a second "Due:" field crammed onto the same line (PDF layouts)
        r"(?:vendor|vndr)\s*:\s*(.+?)(?:\s+due\s*:.*)?$",
        r"from\s*:\s*([^\n@]+?)\s*$",
    ],
    "date": [r"(?:^|\s)(?:date|dt)\s*:\s*(.+)"],
    "due_date": [r"due\s*(?:date|dt)?\s*:\s*(.+)"],
}

_ITEM_PATTERNS = [
    # "WidgetA   qty: 10   unit price: $250.00"  /  "GadgetX  qty 20  @ $750 ea"
    r"^\s*(?P<name>[A-Za-z][\w ]*?)\s+qty:?\s*(?P<qty>-?\d+)\s+"
    r"(?:unit price:|@)\s*\$?(?P<price>[\d.,O]+)",
    # "- SuperGizmo   x12   $400.00 each"
    r"^\s*-\s*(?P<name>[A-Za-z]\w*)\s+x(?P<qty>-?\d+)\s+\$?(?P<price>[\d.,O]+)",
    # Tabular rows, both column-aligned txt and single-spaced PDF text, with an
    # optional trailing note: "WidgetA (rush order)  4  $300.00  $1,200.00",
    # "GadgetX 3 $750.00 $2,250.00 Expedited"
    r"^\s*(?P<name>[A-Za-z][\w ]*?(?:\s*\([^)]*\))?)\s+(?P<qty>-?\d+)\s+"
    r"\$(?P<price>[\d.,O]+)(?:\s+\$(?P<line_total>[\d.,O]+))?(?:\s+(?P<note>[A-Za-z].*))?\s*$",
]

_TOTAL_LABELS = [
    (r"(?<!sub)total(?:\s*amount)?\s*:\s*\$?([\d.,O-]+)", "total"),
    (r"^\s*amt\s*:\s*\$?([\d.,O-]+)", "total"),
    (r"subtotal\s*:\s*\$?([\d.,O-]+)", "subtotal"),
    (r"(?:sales\s+)?tax(?:\s*\([^)]*\))?\s*:\s*\$?([\d.,O-]+)", "tax_amount"),
    (r"shipping\s*:\s*\$?([\d.,O-]+)", "extra_charges"),
]


def _first_match(patterns: list[str], line: str) -> str | None:
    for pat in patterns:
        m = re.search(pat, line, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None


def _from_text(raw: str) -> Invoice:
    # Collapse the stylized "I N V O I C E" banner so it can't confuse matching
    lines = [re.sub(r"^\s*I N V O I C E\s*$", "INVOICE", ln) for ln in raw.splitlines()]
    fields: dict[str, str] = {}
    amounts: dict[str, float] = {}
    items: list[LineItem] = []
    notes: list[str] = []

    for line in lines:
        if not line.strip() or set(line.strip()) <= {"-", "="}:
            continue
        for key, pats in _FIELD_PATTERNS.items():
            if key not in fields:
                val = _first_match(pats, line)
                # "due_date" regex would also match a bare "Date:" line; guard order
                if val and not (key == "date" and re.search(r"due", line, re.IGNORECASE)):
                    fields[key] = val
        for pat, key in _TOTAL_LABELS:
            if key not in amounts:
                m = re.search(pat, line, re.IGNORECASE)
                if m:
                    parsed = parse_money(m.group(1))
                    if parsed is not None:
                        amounts[key] = parsed
        for pat in _ITEM_PATTERNS:
            m = re.match(pat, line)
            if m:
                g = m.groupdict()
                name, paren_note = canonicalize_item(g["name"])
                if name.lower() in {"item", "description", "qty", "total", "subtotal"}:
                    break
                items.append(
                    LineItem(
                        item=name,
                        quantity=int(g["qty"]),
                        unit_price=parse_money(g["price"]),
                        line_total=parse_money(g.get("line_total")),
                        note=paren_note or g.get("note"),
                    )
                )
                break
        m = re.search(r"notes?\s*:\s*(.+)", line, re.IGNORECASE)
        if m:
            notes.append(m.group(1).strip())

    due_raw = fields.get("due_date")
    due = parse_date(due_raw)
    terms = _first_match([r"(?:payment\s+)?(?:terms|pymnt terms)\s*:\s*(.+)"], raw) or ""
    return Invoice(
        invoice_number=_normalize_number(fields.get("invoice_number", "")),
        vendor=fields.get("vendor", ""),
        invoice_date=parse_date(fields.get("date")),
        due_date=due,
        due_date_raw=None if due else due_raw,
        line_items=items,
        subtotal=amounts.get("subtotal"),
        tax_amount=amounts.get("tax_amount"),
        extra_charges=amounts.get("extra_charges", 0.0),
        total=amounts.get("total"),
        payment_terms=terms,
        notes=" ".join(notes),
    )


def _normalize_number(raw: str) -> str:
    """'INV 1012' -> 'INV-1012'; bare '1002' -> 'INV-1002'."""
    cleaned = raw.strip().rstrip(".,")
    if re.fullmatch(r"\d+", cleaned):
        return f"INV-{cleaned}"
    return re.sub(r"^INV[ _]", "INV-", cleaned, flags=re.IGNORECASE).upper()
