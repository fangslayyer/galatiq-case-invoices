"""SQLite access: mock inventory + processed-invoice registry.

The inventory table is the legacy system the Validator checks against.
The processed_invoices registry gives the pipeline idempotency (an invoice is
never paid twice) and catches resubmissions/revisions of known invoices.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

SEED_INVENTORY: list[tuple[str, int, float]] = [
    # (item, stock, unit_price) — from the case brief, plus catalog prices
    ("WidgetA", 15, 250.00),
    ("WidgetB", 10, 500.00),
    ("GadgetX", 5, 750.00),
    ("FakeItem", 0, 1000.00),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS inventory (
    item TEXT PRIMARY KEY,
    stock INTEGER NOT NULL,
    unit_price REAL
);
CREATE TABLE IF NOT EXISTS processed_invoices (
    invoice_number TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    vendor TEXT,
    total REAL,
    final_status TEXT NOT NULL,
    run_id TEXT,
    processed_at TEXT DEFAULT (datetime('now'))
);
"""


@dataclass(frozen=True)
class InventoryRecord:
    item: str
    stock: int
    unit_price: float | None


@dataclass(frozen=True)
class ProcessedRecord:
    invoice_number: str
    content_hash: str
    final_status: str


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def init(self, *, reset: bool = False) -> None:
        with self.connect() as conn:
            if reset:
                conn.execute("DROP TABLE IF EXISTS inventory")
                conn.execute("DROP TABLE IF EXISTS processed_invoices")
            conn.executescript(SCHEMA)
            conn.executemany(
                "INSERT OR IGNORE INTO inventory (item, stock, unit_price) VALUES (?, ?, ?)",
                SEED_INVENTORY,
            )

    # -- inventory ----------------------------------------------------------

    def get_item(self, item: str) -> InventoryRecord | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT item, stock, unit_price FROM inventory WHERE item = ? COLLATE NOCASE",
                (item,),
            ).fetchone()
        if row is None:
            return None
        return InventoryRecord(row["item"], row["stock"], row["unit_price"])

    def all_items(self) -> list[InventoryRecord]:
        with self.connect() as conn:
            rows = conn.execute("SELECT item, stock, unit_price FROM inventory").fetchall()
        return [InventoryRecord(r["item"], r["stock"], r["unit_price"]) for r in rows]

    # -- processed registry -------------------------------------------------

    def get_processed(self, invoice_number: str) -> ProcessedRecord | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT invoice_number, content_hash, final_status "
                "FROM processed_invoices WHERE invoice_number = ?",
                (invoice_number,),
            ).fetchone()
        if row is None:
            return None
        return ProcessedRecord(row["invoice_number"], row["content_hash"], row["final_status"])

    def record_processed(
        self,
        invoice_number: str,
        content_hash: str,
        vendor: str,
        total: float | None,
        final_status: str,
        run_id: str,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO processed_invoices "
                "(invoice_number, content_hash, vendor, total, final_status, run_id) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(invoice_number) DO UPDATE SET "
                "content_hash=excluded.content_hash, vendor=excluded.vendor, "
                "total=excluded.total, final_status=excluded.final_status, "
                "run_id=excluded.run_id, processed_at=datetime('now')",
                (invoice_number, content_hash, vendor, total, final_status, run_id),
            )
