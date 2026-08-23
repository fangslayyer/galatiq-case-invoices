"""SQLite access to the mock inventory — the legacy system we validate against.

Deliberately *only* inventory: everything the pipeline itself produces
(runs, registry, telemetry) lives in invoiceflow.db via `runstore.RunStore`,
keeping the case's legacy-system boundary visible in the file layout
(docs/schema.md D1). The processed-invoice registry that used to live here is
now `invoice_registry` in the run store.
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
"""


@dataclass(frozen=True)
class InventoryRecord:
    item: str
    stock: int
    unit_price: float | None


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
                # pre-runstore files kept the registry here; clear it on reset
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
