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

SEED_INVENTORY: list[tuple[str, int]] = [
    # (item, stock) — exactly the case brief's seed data, and deliberately no
    # more. The brief does permit a unit_price column, but a legacy stock
    # system was never handed purchase-order prices, and the only numbers
    # available to seed one from are the sample invoices' own line items. A
    # catalog whose prices are derived from the documents it audits cannot
    # tell a negotiated discount from an overcharge; it would lend invented
    # figures the authority of a reference, which is worse than holding no
    # prices at all. What we cannot source, we do not claim to know.
    ("WidgetA", 15),
    ("WidgetB", 10),
    ("GadgetX", 5),
    ("FakeItem", 0),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS inventory (
    item TEXT PRIMARY KEY,
    stock INTEGER NOT NULL
);
"""


@dataclass(frozen=True)
class InventoryRecord:
    item: str
    stock: int


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
                "INSERT OR IGNORE INTO inventory (item, stock) VALUES (?, ?)",
                SEED_INVENTORY,
            )

    # -- inventory ----------------------------------------------------------

    def get_item(self, item: str) -> InventoryRecord | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT item, stock FROM inventory WHERE item = ? COLLATE NOCASE",
                (item,),
            ).fetchone()
        if row is None:
            return None
        return InventoryRecord(row["item"], row["stock"])

    def all_items(self) -> list[InventoryRecord]:
        with self.connect() as conn:
            rows = conn.execute("SELECT item, stock FROM inventory").fetchall()
        return [InventoryRecord(r["item"], r["stock"]) for r in rows]
