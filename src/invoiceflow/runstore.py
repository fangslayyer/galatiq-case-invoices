"""invoiceflow.db — the pipeline's system of record.

`RunStore` owns everything the pipeline produces: documents, runs, agent
turns, telemetry, the payment registry, and human reviews. The legacy
inventory database (`db.py`) deliberately stays a separate file — it is the
system we validate *against*, not part of what we produce (docs/schema.md D1).

Write discipline (docs/schema.md D2):
  * `begin_run` inserts the run row up front as `failed` — a crash leaves an
    honest audit row instead of nothing.
  * `finish_run` writes every run artifact in ONE transaction at the end.
  * Only `invoice_registry` is mutated mid-run (the `record` node), because
    payment idempotency must be visible to the very next run.
  * `inbox_items` is mutable too, but it is not a run artifact: it tracks a
    file's journey through the queue, and the run it produced is a foreign key.

Safe to share across threads, which the dashboard's inbox worker relies on:
this object holds a path, never a connection, and every method opens and drops
its own inside one call frame. sqlite3's `check_same_thread` therefore never
fires.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

from .models import (
    ApprovalDecision,
    ApprovalStatus,
    CritiqueRound,
    FinalStatus,
    HumanReview,
    Invoice,
    InvoiceRunResult,
    IssueCode,
    LineItem,
    OverrideRecord,
    PaymentResult,
    PaymentStatus,
    PrecedentBundle,
    RecentInvoice,
    RuleReasonKind,
    Severity,
    TraceEvent,
    ValidationIssue,
    ValidationReport,
)
from .recording import RunRecorder
from .rules import RuleConstraints

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

#: Category per issue code, seeded into issue_codes (docs/schema.md D5).
ISSUE_CATEGORIES: dict[IssueCode, str] = {
    IssueCode.UNKNOWN_ITEM: "inventory",
    IssueCode.OUT_OF_STOCK: "inventory",
    IssueCode.STOCK_EXCEEDED: "inventory",
    IssueCode.LINE_TOTAL_MISMATCH: "arithmetic",
    IssueCode.SUBTOTAL_MISMATCH: "arithmetic",
    IssueCode.TOTAL_MISMATCH: "arithmetic",
    IssueCode.NEGATIVE_QUANTITY: "integrity",
    IssueCode.NEGATIVE_AMOUNT: "integrity",
    IssueCode.MISSING_VENDOR: "integrity",
    IssueCode.MISSING_TOTAL: "integrity",
    IssueCode.MISSING_DUE_DATE: "integrity",
    IssueCode.SUSPICIOUS_DUE_DATE: "integrity",
    IssueCode.UNEXPECTED_CURRENCY: "integrity",
    IssueCode.NO_LINE_ITEMS: "integrity",
    IssueCode.DUPLICATE_INVOICE: "duplicate",
    IssueCode.REVISED_INVOICE: "duplicate",
    IssueCode.REVISION_OF_PAID_INVOICE: "duplicate",
    IssueCode.PROMPT_INJECTION_ATTEMPT: "prompt_safety",
    IssueCode.AGENT_OBSERVATION: "agent",
}


class ModelRate(NamedTuple):
    """One model's published rate, USD per million tokens."""

    input_usd_per_mtok: float
    cached_input_usd_per_mtok: float | None
    output_usd_per_mtok: float


MODEL_PRICING: dict[str, ModelRate] = {
    "grok-4.6": ModelRate(2.00, 0.50, 6.00),
}

PRICING_EFFECTIVE_FROM = "1970-01-01"


#: decision_overrides.kind -> runs.decision_source
_OVERRIDE_SOURCE = {
    "hard_rule_review": "hard_rule",
    "hard_rule_reject": "hard_rule",
    "critic_escalation": "critic_escalation",
    "critic_exhausted": "critic_exhausted",
}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


#: Columns that arrived after the tables holding them. SQLite can add a column
#: but cannot alter one, so every entry must be additive and carry a default —
#: which is the only kind of change this schema has ever made, and the only kind
#: that has any business happening without a real migration tool.
_ADDED_COLUMNS: list[tuple[str, str, str]] = [
    ("validation_issues", "subject", "TEXT NOT NULL DEFAULT ''"),
]

#: Tables whose *declaration* may be recreated in place when it stops matching
#: schema.sql. SQLite can add a column but cannot touch a CHECK, so widening one
#: — `rule_reasons.kind` gaining 'precedent' — means rebuilding the table and
#: copying the rows across.
#:
#: An explicit allowlist rather than "rebuild whatever differs", because a
#: rebuild is only safe when the change *widens* what is allowed. Narrowing one
#: would silently drop every row that no longer fits, and deciding which is
#: which is a judgement, not something to infer from a diff.
_REBUILDABLE = {"rule_reasons"}


def _canonical(sql: str) -> str:
    """A CREATE statement reduced to what it actually declares — comments,
    whitespace, the trailing semicolon and any quoting of the declared name
    removed — so a stored one can be compared with a fresh one.

    The unquoting is the load-bearing part. `_rebuild_table` finishes with
    ALTER TABLE ... RENAME TO, and SQLite rewrites the declaration it keeps as
    `CREATE TABLE "rule_reasons"` when it does. Compared literally against
    schema.sql's unquoted `CREATE TABLE rule_reasons` that never matches again,
    so a table rebuilt once is rebuilt on every open forever — pointless work
    over live rows, and the only reason two opens ever collide mid-rebuild.
    """
    body = re.sub(r"\s+", " ", re.sub(r"--[^\n]*", "", sql)).strip().rstrip(";").strip()
    return re.sub(r'^(CREATE (?:TABLE|VIEW|INDEX) )"([^"]+)"', r"\1\2", body, flags=re.IGNORECASE)


def _rebuild_table(conn: sqlite3.Connection, name: str, statement: str) -> None:
    """Recreate `name` from `statement`, carrying every column both versions
    share. SQLite's own recommended dance for a constraint change.

    The scaffolding is dropped before it is created, because a half-finished
    rebuild is exactly what leaves one behind: sqlite3 opens a transaction for
    DML but not for DDL, so an attempt made outside `_grow_schema`'s explicit
    one committed its CREATE and then failed, after which every later open died
    on "table rule_reasons__new already exists". A database wedged that way
    heals on the next open instead of staying wedged. Its rows are safe: this
    runs only when `name` itself still exists, and the scaffold never holds a
    row that table does not.
    """
    scaffold = f"{name}__new"
    conn.execute(f"DROP TABLE IF EXISTS {scaffold}")
    conn.execute(
        re.sub(rf"\bCREATE TABLE {name}\b", f"CREATE TABLE {scaffold}", statement, count=1)
    )
    old = [row[1] for row in conn.execute(f"PRAGMA table_info({name})")]
    new = {row[1] for row in conn.execute(f"PRAGMA table_info({scaffold})")}
    shared = ", ".join(column for column in old if column in new)
    conn.execute(f"INSERT INTO {scaffold} ({shared}) SELECT {shared} FROM {name}")
    conn.execute(f"DROP TABLE {name}")
    conn.execute(f"ALTER TABLE {scaffold} RENAME TO {name}")


def _statements(script: str):
    """Split a SQL script into complete statements.

    `sqlite3.complete_statement` rather than a split on ';': it knows about
    string literals and comments, and schema.sql is full of both.
    """
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            yield buffer
            buffer = ""


def _created_object(statement: str) -> str | None:
    """The name a CREATE statement declares, or None for anything else.

    Comments are stripped first, so a table discussed in a comment above some
    other statement is never mistaken for one being created.
    """
    body = re.sub(r"--[^\n]*", "", statement)
    match = re.search(r"CREATE\s+(?:TABLE|VIEW|INDEX)\s+(\w+)", body, re.IGNORECASE)
    return match.group(1) if match else None


def _grow_schema(conn: sqlite3.Connection) -> None:
    """Bring a database created by an older revision up to schema.sql.

    Additive only, and deliberately not a migration framework: every object the
    schema declares and this database lacks is created, every column in
    `_ADDED_COLUMNS` that is missing is added with its default, and nothing is
    ever dropped, renamed or retyped.

    The reasoning is the issue_codes re-seed's, one level up: a schema change has
    to reach the databases that already exist, or the first run to need it dies
    on a real invoice rather than on the commit that caused it. The alternative
    was making somebody's audit trail the price of an upgrade, which is a strange
    thing for an audit trail to cost.
    """
    # One writer at a time, and the survey taken under the same lock that the
    # changes are: every step below reads what the database has before altering
    # it, which is precisely the shape that breaks when somebody else alters it
    # in between. The dashboard opens a store on each script run and another on
    # its inbox worker, so these do overlap in the wild — and the collision was
    # not theoretical, it was two threads rebuilding rule_reasons at once.
    # IMMEDIATE takes the write lock at BEGIN rather than at the first write,
    # so the loser waits out `busy_timeout` and then finds the work already
    # done, instead of creating a table that by then exists.
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master")}
        # Columns before objects, not the other way round: a new view can select
        # a new column, and creating it against a table that has not grown one
        # yet leaves a view that parses now and fails the first time it is
        # queried.
        for table, column, declaration in _ADDED_COLUMNS:
            if table not in existing:
                continue
            held = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in held:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
        declared = {
            r["name"]: r["sql"] for r in conn.execute("SELECT name, sql FROM sqlite_master")
        }
        for statement in _statements(SCHEMA_PATH.read_text()):
            name = _created_object(statement)
            if name is None:
                continue
            if name not in existing:
                conn.execute(statement)
            elif name in _REBUILDABLE and _canonical(declared[name]) != _canonical(statement):
                _rebuild_table(conn, name, statement)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


@dataclass(frozen=True)
class InboxItem:
    """One uploaded file, as the worker claims it off the queue."""

    id: int
    filename: str
    stored_path: Path
    content_sha256: str


@dataclass(frozen=True)
class SourceDocument:
    """The document a run read, exactly as it was stored.

    `raw_text` is post-loader: for a PDF that means the pdfplumber extraction,
    which is what the Extractor actually saw — the version that matters when a
    figure on screen disagrees with the page.
    """

    file_format: str
    raw_text: str
    char_count: int
    first_seen_path: str


@dataclass(frozen=True)
class ProcessedRecord:
    """Registry entry: the current standing of one invoice number."""

    invoice_number: str
    content_hash: str
    final_status: str
    # What we settled at. Carried so a revision can state its own delta
    # against it — a reviewer who is told only "this was revised" still has
    # to go and look the amount up.
    total: float | None = None


class RunStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._ensure_schema()

    # -- plumbing -----------------------------------------------------------

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        # Per-connection, off by default — every REFERENCES in the schema is
        # inert without this (docs/schema.md §7).
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def _ensure_schema(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            existing = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master")}
            if "runs" not in existing:
                conn.execute("PRAGMA journal_mode = WAL")  # persistent, set once
                conn.executescript(SCHEMA_PATH.read_text())
            else:
                _grow_schema(conn)
            # Re-seeded on every init, not only at creation. issue_codes is a
            # lookup table that validation_issues.code holds a live foreign key
            # to, so a code added to the IssueCode enum has to reach databases
            # that already exist — otherwise the first run to raise it dies on
            # a foreign-key violation, and the failure lands on a real invoice
            # rather than on the change that caused it.
            conn.executemany(
                "INSERT OR IGNORE INTO issue_codes (code, category) VALUES (?, ?)",
                [(code.value, cat) for code, cat in ISSUE_CATEGORIES.items()],
            )
            # Same reasoning, same place: the backend's published rate is a
            # fact about the model, not about this database, so a fresh one
            # knows it and an existing one learns it. OR IGNORE, because a rate
            # somebody set by hand at this key is theirs, not ours — and a
            # genuine price change arrives as a later-dated row, which
            # _price_call prefers anyway.
            conn.executemany(
                "INSERT OR IGNORE INTO model_pricing (model, effective_from, "
                "input_usd_per_mtok, cached_input_usd_per_mtok, output_usd_per_mtok) "
                "VALUES (?, ?, ?, ?, ?)",
                [(model, PRICING_EFFECTIVE_FROM, *rate) for model, rate in MODEL_PRICING.items()],
            )

    def init(self, *, reset: bool = False) -> int:
        """Create the database; with reset, drop it and start fresh (D11).

        Returns how many previously unpriced LLM calls the seeded rates could
        put a number on — zero after a reset, by definition.
        """
        if reset:
            for suffix in ("", "-wal", "-shm"):
                Path(f"{self.path}{suffix}").unlink(missing_ok=True)
        self._ensure_schema()
        with self.connect() as conn:
            return self._price_unpriced_calls(conn)

    @staticmethod
    def _price_unpriced_calls(conn: sqlite3.Connection) -> int:
        """Fill in the cost of calls that were recorded before their model had a
        price, and return how many were priced.

        Not a re-pricing: rows with a cost keep it, because that cost is the
        snapshot of what the call was billed at. NULL is not a snapshot of
        anything — it is the record of us not knowing — so filling it from the
        rate now on file rewrites no history. The arithmetic is _price_call's,
        in SQL, over the newest rate effective at or before each call.

        Run from init() rather than from _ensure_schema: opening the store is
        something the dashboard does constantly and should stay read-shaped,
        while `--init-db` is somebody asking for the database to be put in
        order.
        """
        cur = conn.execute(
            "UPDATE llm_calls SET cost_usd = ("
            "  SELECT (MAX(llm_calls.input_tokens - llm_calls.cached_input_tokens, 0)"
            "            * p.input_usd_per_mtok"
            "          + llm_calls.cached_input_tokens"
            "            * COALESCE(p.cached_input_usd_per_mtok, p.input_usd_per_mtok)"
            "          + llm_calls.output_tokens * p.output_usd_per_mtok) / 1000000.0"
            "  FROM model_pricing p"
            "  WHERE p.model = llm_calls.model AND p.effective_from <= llm_calls.started_at"
            "  ORDER BY p.effective_from DESC LIMIT 1) "
            "WHERE cost_usd IS NULL AND EXISTS ("
            "  SELECT 1 FROM model_pricing p"
            "  WHERE p.model = llm_calls.model AND p.effective_from <= llm_calls.started_at)"
        )
        return cur.rowcount

    # -- intake -------------------------------------------------------------

    def register_document(self, raw_text: str, source_path: str) -> tuple[int, int]:
        """Insert-or-find the document for `raw_text`.

        Returns (document_id, prior_run_count). Identity is the content hash
        alone: the same bytes at a new path are the same document, and running
        them again is a re-run, not new work.
        """
        digest = hashlib.sha256(raw_text.encode()).hexdigest()
        fmt = Path(source_path).suffix.lstrip(".").lower() or "txt"
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO documents (content_sha256, raw_text, file_format, "
                "char_count, first_seen_path, first_seen_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(content_sha256) DO NOTHING",
                (digest, raw_text, fmt, len(raw_text), source_path, _now()),
            )
            doc_id = conn.execute(
                "SELECT id FROM documents WHERE content_sha256 = ?", (digest,)
            ).fetchone()["id"]
            prior = conn.execute(
                "SELECT COUNT(*) AS n FROM runs WHERE document_id = ?", (doc_id,)
            ).fetchone()["n"]
        return doc_id, prior

    def document_history(self, raw_text: str) -> tuple[int | None, int]:
        """(document_id, prior_run_count) for `raw_text`, WITHOUT registering it.

        The read-only half of `register_document`, for the upload dialog's
        pre-flight: it has to say "these exact bytes have been through the
        pipeline N times" *before* the user commits, and six Grok calls are
        spent. Registering here instead would
        file a documents row whose `first_seen_path` names an upload the user
        is about to skip — a lie in the one table whose whole job is identity.
        """
        digest = hashlib.sha256(raw_text.encode()).hexdigest()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id FROM documents WHERE content_sha256 = ?", (digest,)
            ).fetchone()
            if row is None:
                return None, 0
            prior = conn.execute(
                "SELECT COUNT(*) AS n FROM runs WHERE document_id = ?", (row["id"],)
            ).fetchone()["n"]
        return row["id"], prior

    def document_for_run(self, run_id: str) -> SourceDocument | None:
        """What this run read, or None when it failed before reading anything."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT d.file_format, d.raw_text, d.char_count, d.first_seen_path "
                "FROM runs r JOIN documents d ON d.id = r.document_id WHERE r.run_id = ?",
                (run_id,),
            ).fetchone()
        return None if row is None else SourceDocument(**dict(row))

    # -- inbox --------------------------------------------------------------

    def enqueue_upload(
        self,
        *,
        filename: str,
        stored_path: str,
        file_format: str,
        byte_size: int,
        content_sha256: str,
        prior_runs: int,
        source: str = "upload",
    ) -> int:
        """Add one file to the queue. Returns inbox_items.id."""
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO inbox_items (filename, stored_path, file_format, byte_size, "
                "content_sha256, prior_runs, source, state, enqueued_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?)",
                (
                    filename,
                    stored_path,
                    file_format,
                    byte_size,
                    content_sha256,
                    prior_runs,
                    source,
                    _now(),
                ),
            )
            assert cur.lastrowid is not None
            return cur.lastrowid

    def claim_next_upload(self) -> InboxItem | None:
        """Take the oldest queued item, atomically. None when the queue is empty.

        One statement, not SELECT-then-UPDATE: the latter is a deferred
        transaction upgrading read->write, and SQLite does NOT run the busy
        handler for a snapshot-upgrade conflict, so `busy_timeout` would not
        cover a second claimant. Doing it in one statement also means the queue
        stays correct if a second worker is ever started — which Streamlit can
        cause on its own, since its resource cache is clearable from the app's
        ⋮ menu while the thread it cached keeps running.
        """
        with self.connect() as conn:
            row = conn.execute(
                "UPDATE inbox_items SET state = 'processing', started_at = ?, stage = '' "
                "WHERE id = (SELECT id FROM inbox_items WHERE state = 'queued' "
                "ORDER BY id LIMIT 1) RETURNING *",
                (_now(),),
            ).fetchone()
        if row is None:
            return None
        return InboxItem(
            id=row["id"],
            filename=row["filename"],
            stored_path=Path(row["stored_path"]),
            content_sha256=row["content_sha256"],
        )

    def set_upload_stage(self, item_id: int, stage: str) -> None:
        """Which graph node has the floor. Decoration for a live UI, never a
        fact the pipeline reads back."""
        with self.connect() as conn:
            conn.execute(
                "UPDATE inbox_items SET stage = ? WHERE id = ? AND state = 'processing'",
                (stage, item_id),
            )

    def finish_upload(self, item_id: int, *, run_id: str | None = None, error: str = "") -> None:
        """Close an item: `processed` when a run happened, `failed` when none did.

        Note the asymmetry — a run that ended FAILED still leaves the item
        `processed`, because the pipeline did its job and reached an honest
        verdict. `failed` here means we never got a run out of this file at all.
        The run FK is resolved in SQL so callers can stay in run_id space.
        """
        with self.connect() as conn:
            conn.execute(
                "UPDATE inbox_items SET state = ?, stage = '', finished_at = ?, error = ?, "
                "run_id = (SELECT id FROM runs WHERE run_id = ?) WHERE id = ?",
                ("failed" if error else "processed", _now(), error, run_id, item_id),
            )

    def requeue_upload(self, item_id: int) -> None:
        """Put a finished item back in the queue — the Retry button.

        The old run reference is cleared rather than kept: the retry produces
        its own run, and the abandoned one stays in `runs` as the honest
        `failed` row `begin_run` wrote.
        """
        with self.connect() as conn:
            conn.execute(
                "UPDATE inbox_items SET state = 'queued', stage = '', started_at = NULL, "
                "finished_at = NULL, run_id = NULL, error = '' WHERE id = ?",
                (item_id,),
            )

    def dismiss_upload(self, item_id: int) -> None:
        """Clear it out of the list. The row stays — what we were handed and
        when is a fact; `v_inbox` is what does the hiding."""
        with self.connect() as conn:
            conn.execute("UPDATE inbox_items SET dismissed_at = ? WHERE id = ?", (_now(), item_id))

    def reclaim_stale_uploads(self) -> int:
        """Close items a dead process left mid-flight, returning how many.

        Two statements, and the order matters. An item can be stale because the
        server died *between* `finish_run` committing and `finish_upload`
        running, in which case a complete run exists and the item is
        `processed`, not failed. `runs.source_path` is exactly
        `inbox_items.stored_path`, and uploads have unique paths by
        construction, so adopting that run is safe.
        """
        with self.connect() as conn:
            adopted = conn.execute(
                "UPDATE inbox_items SET state = 'processed', stage = '', finished_at = ?, "
                "run_id = (SELECT r.id FROM runs r WHERE r.source_path = inbox_items.stored_path "
                "          AND r.finished_at IS NOT NULL ORDER BY r.id DESC LIMIT 1) "
                "WHERE state = 'processing' AND EXISTS ("
                "    SELECT 1 FROM runs r WHERE r.source_path = inbox_items.stored_path "
                "    AND r.finished_at IS NOT NULL)",
                (_now(),),
            ).rowcount
            abandoned = conn.execute(
                "UPDATE inbox_items SET state = 'failed', stage = '', finished_at = ?, error = ? "
                "WHERE state = 'processing'",
                (
                    _now(),
                    "The dashboard stopped while this file was being processed. Nothing was "
                    "recorded for it — retry to run it again.",
                ),
            ).rowcount
        return adopted + abandoned

    def inbox_rows(self, limit: int = 200) -> list[dict]:
        """Everything not dismissed, newest first."""
        with self.connect() as conn:
            return [
                dict(r)
                for r in conn.execute("SELECT * FROM v_inbox ORDER BY id DESC LIMIT ?", (limit,))
            ]

    def inbox_counts(self) -> dict[str, int]:
        """state -> count. What the tab label and the polling interval read."""
        with self.connect() as conn:
            return {
                r["state"]: r["n"]
                for r in conn.execute("SELECT state, COUNT(*) AS n FROM v_inbox GROUP BY state")
            }

    # -- run lifecycle ------------------------------------------------------

    def begin_run(
        self, run_id: str, source_path: str, started_at: str, llm_backend: str, revision: str
    ) -> int:
        """Open the run's audit row. Deliberately pessimistic: it says
        `failed` until `finish_run` proves otherwise, so a crash mid-run
        leaves the truth behind rather than nothing."""
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO runs (run_id, source_path, started_at, llm_backend, "
                "pipeline_revision, final_status, error) VALUES (?, ?, ?, ?, ?, 'failed', "
                "'run did not complete')",
                (run_id, source_path, started_at, llm_backend, revision),
            )
            assert cur.lastrowid is not None
            return cur.lastrowid

    def finish_run(
        self,
        run_pk: int,
        *,
        document_id: int | None,
        finished_at: str,
        duration_ms: int,
        final_status: FinalStatus,
        decision: ApprovalDecision | None,
        quarantine_reason: str,
        error: str,
        recorder: RunRecorder,
        invoice: Invoice | None,
        extraction_attempts: list[str],
        report: ValidationReport | None,
        constraints: RuleConstraints | None,
        scrutiny_threshold: float,
        critique_rounds: list[CritiqueRound],
        overrides: list[OverrideRecord],
        payment: PaymentResult | None,
        trace: list[TraceEvent],
        precedents: PrecedentBundle | None = None,
    ) -> None:
        """Write every artifact of one finished run in a single transaction."""
        decision_source = None
        if decision is not None:
            decision_source = _OVERRIDE_SOURCE[overrides[-1].kind] if overrides else "agent"

        with self.connect() as conn:
            conn.execute("BEGIN")
            conn.execute(
                "UPDATE runs SET document_id=?, finished_at=?, duration_ms=?, final_status=?, "
                "decision_status=?, decision_source=?, quarantine_reason=?, error=? WHERE id=?",
                (
                    document_id,
                    finished_at,
                    duration_ms,
                    final_status.value,
                    decision.status.value if decision else None,
                    decision_source,
                    quarantine_reason,
                    error,
                    run_pk,
                ),
            )

            # The spine: one row per agent turn, telemetry hanging off each.
            turn_ids: dict[int, int] = {}  # recorder seq -> agent_invocations.id
            for turn in recorder.turns:
                cur = conn.execute(
                    "INSERT INTO agent_invocations (run_id, seq, node, agent, round_no, "
                    "triggered_by, outcome, error, started_at, duration_ms) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_pk,
                        turn.seq,
                        turn.node,
                        turn.agent,
                        turn.round_no,
                        turn_ids.get(turn.triggered_by_seq or -1),
                        turn.outcome,
                        turn.error,
                        turn.started_at,
                        turn.duration_ms,
                    ),
                )
                assert cur.lastrowid is not None
                turn_ids[turn.seq] = cur.lastrowid
                for call in turn.calls:
                    conn.execute(
                        "INSERT INTO llm_calls (invocation_id, seq, model, input_tokens, "
                        "cached_input_tokens, output_tokens, reasoning_tokens, total_tokens, "
                        "tool_calls, latency_ms, cost_usd, langsmith_run_id, started_at, error) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            turn_ids[turn.seq],
                            call.seq,
                            call.model,
                            call.input_tokens,
                            call.cached_input_tokens,
                            call.output_tokens,
                            call.reasoning_tokens,
                            call.total_tokens,
                            call.tool_calls,
                            call.latency_ms,
                            self._price_call(conn, call),
                            call.langsmith_run_id,
                            call.started_at,
                            call.error,
                        ),
                    )

            def turn_id(agent: str, round_no: int = 1) -> int | None:
                for turn in recorder.turns:
                    if turn.agent == agent and turn.round_no == round_no:
                        return turn_ids[turn.seq]
                return None

            extractor_id = turn_id("extractor")
            if extractor_id is not None:
                conn.executemany(
                    "INSERT INTO extraction_attempts (invocation_id, attempt_no, problems) "
                    "VALUES (?, ?, ?)",
                    [(extractor_id, i, p) for i, p in enumerate(extraction_attempts, 1)],
                )

            if invoice is not None and extractor_id is not None:
                cur = conn.execute(
                    "INSERT INTO invoices (run_id, invocation_id, invoice_number, vendor, "
                    "invoice_date, due_date, due_date_raw, subtotal, tax_amount, extra_charges, "
                    "total, currency, payment_terms, notes, content_hash) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_pk,
                        extractor_id,
                        invoice.invoice_number,
                        invoice.vendor,
                        str(invoice.invoice_date) if invoice.invoice_date else None,
                        str(invoice.due_date) if invoice.due_date else None,
                        invoice.due_date_raw,
                        invoice.subtotal,
                        invoice.tax_amount,
                        invoice.extra_charges,
                        invoice.total,
                        invoice.currency,
                        invoice.payment_terms,
                        invoice.notes,
                        invoice.content_hash(),
                    ),
                )
                invoice_pk = cur.lastrowid
                conn.executemany(
                    "INSERT INTO invoice_line_items (invoice_id, line_no, item, quantity, "
                    "unit_price, line_total, note) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [
                        (invoice_pk, n, li.item, li.quantity, li.unit_price, li.line_total, li.note)
                        for n, li in enumerate(invoice.line_items, 1)
                    ],
                )

            validator_id = turn_id("validator")
            if report is not None and validator_id is not None:
                cur = conn.execute(
                    "INSERT INTO validation_reports (run_id, invocation_id, summary) "
                    "VALUES (?, ?, ?)",
                    (run_pk, validator_id, report.summary),
                )
                report_pk = cur.lastrowid
                safety_net = set(report.safety_net_tools)
                conn.executemany(
                    "INSERT INTO validation_tool_runs (report_id, seq, tool_name, invoked_by) "
                    "VALUES (?, ?, ?, ?)",
                    [
                        (report_pk, n, name, "safety_net" if name in safety_net else "agent")
                        for n, name in enumerate(report.tools_used, 1)
                    ],
                )
                conn.executemany(
                    "INSERT INTO validation_issues (report_id, seq, code, severity, detail, "
                    "subject, origin, tool_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            report_pk,
                            n,
                            issue.code.value,
                            issue.severity.value,
                            issue.detail,
                            issue.subject,
                            "agent" if issue.code == IssueCode.AGENT_OBSERVATION else "tool",
                            None,
                        )
                        for n, issue in enumerate(report.issues, 1)
                    ],
                )

            if constraints is not None:
                cur = conn.execute(
                    "INSERT INTO rule_evaluations (run_id, must_reject, must_review, "
                    "requires_scrutiny, scrutiny_threshold) VALUES (?, ?, ?, ?, ?)",
                    (
                        run_pk,
                        int(constraints.must_reject),
                        int(constraints.must_review),
                        int(constraints.requires_scrutiny),
                        scrutiny_threshold,
                    ),
                )
                eval_pk = cur.lastrowid
                reasons = [
                    (eval_pk, kind.value, reason)
                    for kind, bucket in (
                        (RuleReasonKind.REJECT, constraints.reject_reasons),
                        (RuleReasonKind.REVIEW, constraints.review_reasons),
                        (RuleReasonKind.SCRUTINY, constraints.scrutiny_reasons),
                        (RuleReasonKind.ADVISORY, constraints.advisory_warnings),
                        (RuleReasonKind.PRECEDENT, constraints.precedent_discharged),
                    )
                    for reason in bucket
                ]
                conn.executemany(
                    "INSERT INTO rule_reasons (rule_evaluation_id, kind, reason) VALUES (?, ?, ?)",
                    reasons,
                )

            # Written whether or not anything was released: a refusal to release
            # is as much a fact about the policy as a release is, and "how often
            # does history fall short, and by how much?" is the question that
            # says whether this feature is worth its complexity.
            if precedents is not None:
                conn.executemany(
                    "INSERT INTO precedent_citations (run_id, code, subject, vendor, cases, "
                    "rejections, burden, support, released, blocked_by, cited_run_ids, terms) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            run_pk,
                            p.code.value,
                            p.subject,
                            p.vendor,
                            len(p.cases),
                            p.rejections,
                            p.burden,
                            p.support,
                            int(p.released),
                            p.blocked_by,
                            json.dumps(p.cited_run_ids),
                            json.dumps(p.terms),
                        )
                        for p in precedents.findings
                    ],
                )

            for round_no, round_ in enumerate(critique_rounds, 1):
                approver_id = turn_id("approver", round_no)
                if approver_id is not None:
                    conn.execute(
                        "INSERT INTO approver_decisions (invocation_id, status, reasoning, "
                        "risk_factors) VALUES (?, ?, ?, ?)",
                        (
                            approver_id,
                            round_.decision.status.value,
                            round_.decision.reasoning,
                            json.dumps(round_.decision.risk_factors),
                        ),
                    )
                critic_id = turn_id("critic", round_no)
                if critic_id is not None:
                    conn.execute(
                        "INSERT INTO critic_reviews (invocation_id, verdict, feedback) "
                        "VALUES (?, ?, ?)",
                        (critic_id, round_.critique.verdict.value, round_.critique.feedback),
                    )

            for override in overrides:
                overridden = turn_id("approver", override.round_no)
                if overridden is None:
                    continue  # unreachable: an override presupposes an approver turn
                conn.execute(
                    "INSERT INTO decision_overrides (run_id, invocation_id, kind, from_status, "
                    "to_status, reasoning, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_pk,
                        overridden,
                        override.kind,
                        override.from_status.value,
                        override.to_status.value,
                        override.reasoning,
                        override.created_at or finished_at,
                    ),
                )

            if payment is not None:
                conn.execute(
                    "INSERT INTO payments (run_id, status, vendor, amount, currency, reference, "
                    "paid_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_pk,
                        payment.status.value,
                        payment.vendor,
                        payment.amount,
                        invoice.currency if invoice else "USD",
                        payment.reference,
                        payment.paid_at or finished_at,
                    ),
                )

            conn.executemany(
                "INSERT INTO trace_events (run_id, seq, stage, event, detail, at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (run_pk, n, ev.stage, ev.event, ev.detail, ev.at or finished_at)
                    for n, ev in enumerate(trace, 1)
                ],
            )

    # -- pricing ------------------------------------------------------------

    def set_model_pricing(
        self,
        model: str,
        *,
        input_usd_per_mtok: float,
        output_usd_per_mtok: float,
        cached_input_usd_per_mtok: float | None = None,
        effective_from: str | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO model_pricing VALUES (?, ?, ?, ?, ?)",
                (
                    model,
                    effective_from or _now(),
                    input_usd_per_mtok,
                    cached_input_usd_per_mtok,
                    output_usd_per_mtok,
                ),
            )

    @staticmethod
    def _price_call(conn: sqlite3.Connection, call) -> float | None:
        """Cost of one call under the newest pricing at or before it — or None
        when no pricing is known. Never invented: an unknown model costs NULL,
        not zero."""
        row = conn.execute(
            "SELECT * FROM model_pricing WHERE model = ? AND effective_from <= ? "
            "ORDER BY effective_from DESC LIMIT 1",
            (call.model, call.started_at or _now()),
        ).fetchone()
        if row is None:
            return None
        cached_rate = row["cached_input_usd_per_mtok"]
        if cached_rate is None:
            cached_rate = row["input_usd_per_mtok"]
        fresh = max(call.input_tokens - call.cached_input_tokens, 0)
        return (
            fresh * row["input_usd_per_mtok"]
            + call.cached_input_tokens * cached_rate
            + call.output_tokens * row["output_usd_per_mtok"]
        ) / 1_000_000

    # -- registry (mutable current state; mid-run on purpose) ---------------

    def get_processed(self, invoice_number: str) -> ProcessedRecord | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT invoice_number, content_hash, final_status, total "
                "FROM invoice_registry WHERE invoice_number = ?",
                (invoice_number,),
            ).fetchone()
        if row is None:
            return None
        return ProcessedRecord(
            row["invoice_number"], row["content_hash"], row["final_status"], row["total"]
        )

    def record_processed(
        self,
        invoice_number: str,
        content_hash: str,
        vendor: str,
        total: float | None,
        final_status: str,
        run_pk: int | None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO invoice_registry "
                "(invoice_number, content_hash, vendor, total, final_status, last_run_id, "
                "processed_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(invoice_number) DO UPDATE SET "
                "content_hash=excluded.content_hash, vendor=excluded.vendor, "
                "total=excluded.total, final_status=excluded.final_status, "
                "last_run_id=excluded.last_run_id, processed_at=excluded.processed_at",
                (invoice_number, content_hash, vendor, total, final_status, run_pk, _now()),
            )

    def record_settlement(
        self,
        invoice_number: str,
        content_hash: str,
        vendor: str,
        total: float | None,
        final_status: str,
        run_pk: int | None,
    ) -> bool:
        """Update the registry for a finished run, unless that would forget a
        payment. Returns whether it wrote.

        A run ending in anything but `paid` never overwrites a `paid` record:
        the registry is what `outstanding_balance` reads to decide what is
        still owed, so downgrading it is exactly how an invoice becomes payable
        twice.

        Every caller that finishes a run goes through here rather than calling
        `record_processed` directly. The graph and the dashboard both settle
        invoices, and this rule living in only one of them is what let a
        dashboard approval clear the paid flag it was meant to protect.
        """
        prior = self.get_processed(invoice_number)
        if (
            prior is not None
            and prior.final_status == FinalStatus.PAID
            and final_status != FinalStatus.PAID
        ):
            return False
        self.record_processed(invoice_number, content_hash, vendor, total, final_status, run_pk)
        return True

    # -- human review -------------------------------------------------------

    def add_human_review(
        self,
        run_id: str,
        *,
        action: str,
        from_status: FinalStatus,
        to_status: FinalStatus,
        note: str = "",
        reviewer: str = "dashboard",
    ) -> None:
        with self.connect() as conn:
            pk = conn.execute("SELECT id FROM runs WHERE run_id = ?", (run_id,)).fetchone()["id"]
            conn.execute(
                "INSERT INTO human_reviews (run_id, reviewed_at, reviewer, action, from_status, "
                "to_status, note) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (pk, _now(), reviewer, action, from_status.value, to_status.value, note),
            )

    def add_payment(self, run_id: str, payment: PaymentResult, currency: str = "USD") -> None:
        """A human-authorized payment for a run the pipeline did not pay.

        Upserts because `payments.run_id` is UNIQUE and a run can be acted on
        twice: a refused attempt recorded as `skipped_already_paid` is replaced
        by the real payment if a later approval succeeds.
        """
        with self.connect() as conn:
            pk = conn.execute("SELECT id FROM runs WHERE run_id = ?", (run_id,)).fetchone()["id"]
            conn.execute(
                "INSERT INTO payments (run_id, status, vendor, amount, currency, reference, "
                "paid_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET status=excluded.status, "
                "vendor=excluded.vendor, amount=excluded.amount, currency=excluded.currency, "
                "reference=excluded.reference, paid_at=excluded.paid_at",
                (
                    pk,
                    payment.status.value,
                    payment.vendor,
                    payment.amount,
                    currency,
                    payment.reference,
                    payment.paid_at or _now(),
                ),
            )

    # -- precedent ----------------------------------------------------------

    def precedent_rows(self, code: str, subject: str) -> list[dict]:
        """Every prior decision a *person* made on one open question.

        Deliberately not filtered by vendor here. The vendor comparison needs
        normalising (case, punctuation, spacing) and doing it in SQL would mean
        two implementations of one rule that must never disagree — so the rows
        come back by `(code, subject)`, which the index covers, and
        `precedent.vendor_key` does the matching in the one place it is defined.

        `v_review_precedent` is what makes this only ever human decisions: an
        automatically approved run has no `human_reviews` row to join to, and so
        cannot become evidence for the next automatic approval.
        """
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM v_review_precedent WHERE code = ? AND subject = ? "
                "ORDER BY reviewed_at",
                (code, subject),
            ).fetchall()
        return [dict(r) for r in rows]

    def paid_vendors(self) -> list[str]:
        """Every vendor the company has actually settled with, verbatim.

        Read from the registry rather than from `runs.final_status`, because the
        registry is where a human override lands too — "have we ever paid these
        people?" must count the invoices a person released, not only the ones
        the pipeline released by itself. Returned unnormalised: `vendor_key` is
        the one place that decides when two spellings are one company.
        """
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT vendor FROM invoice_registry "
                "WHERE final_status = ? AND vendor <> ''",
                (FinalStatus.PAID.value,),
            ).fetchall()
        return [r["vendor"] for r in rows]

    def invoices_dated_near(self, on: date, days: int) -> list[RecentInvoice]:
        """Every registered invoice dated within `days` of `on` — any vendor.

        Read from `invoice_registry` rather than from `runs`, for the reason
        `paid_vendors` gives: the registry is one row per invoice number at its
        current standing, so a re-run counts once and a human override counts as
        what the person decided rather than as what the pipeline first said.

        Two statuses only. `rejected` is left out because no money will move on
        it, and a rejection must not push the next honest invoice over a
        threshold. `needs_review` is counted even though nothing has been paid
        yet: it is money queued to leave, and the question this feeds is how much
        is heading out of the door in this window. `duplicate` and `failed` runs
        never reach the registry at all.

        The vendor comes back verbatim and unmatched — `vendor_key`
        (precedent.py) is the single place that decides when two spellings are
        one company, and a normalisation that gates automatic payment must not
        have a second implementation in SQL. The join to `invoices` is what
        supplies the date the vendor stamped on the document, which the registry
        does not carry; a registry row whose run predates that table, or whose
        run_pk was never set, drops out of the window rather than being counted
        at an unknown date.
        """
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT reg.invoice_number, reg.vendor, reg.total, reg.final_status, "
                "i.invoice_date FROM invoice_registry reg "
                "JOIN invoices i ON i.run_id = reg.last_run_id "
                "WHERE reg.final_status IN (?, ?) AND reg.total IS NOT NULL "
                "AND i.invoice_date BETWEEN ? AND ?",
                (
                    FinalStatus.PAID.value,
                    FinalStatus.NEEDS_REVIEW.value,
                    str(on - timedelta(days=days)),
                    str(on + timedelta(days=days)),
                ),
            ).fetchall()
        return [
            RecentInvoice(
                invoice_number=r["invoice_number"],
                vendor=r["vendor"],
                invoice_date=r["invoice_date"],
                total=r["total"],
                final_status=FinalStatus(r["final_status"]),
            )
            for r in rows
        ]

    def invoice_for_run(self, run_id: str) -> Invoice | None:
        """The invoice one run extracted.

        Precedent needs the whole invoice, not the summary row: what a prior
        finding put at risk is recomputed from it with the very same function
        that priced the current one, so a case can never be weighed on a
        different measure than the invoice it is being weighed against.
        """
        with self.connect() as conn:
            row = conn.execute("SELECT id FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                return None
            return self._load_invoice(conn, row["id"])

    def learned_precedent(self) -> list[dict]:
        """What the pipeline has learned, per open question — the Learning tab."""
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM v_precedent_learning")]

    def citations_for(self, run_id: str) -> list[dict]:
        """The precedent this run was decided against, released or not."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT pc.* FROM precedent_citations pc "
                "JOIN runs r ON r.id = pc.run_id WHERE r.run_id = ? ORDER BY pc.id",
                (run_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def review_queue(self) -> list[InvoiceRunResult]:
        """Runs waiting on a human decision.

        Membership comes from `v_review_queue` rather than being re-derived by
        whatever renders it, so the dashboard and the analytics cannot disagree
        about what "waiting on a human" means. Note what the view adds over a
        plain status filter: `is_latest`, so a document that has been re-run
        asks for one decision rather than one per attempt.
        """
        with self.connect() as conn:
            run_ids = [row["run_id"] for row in conn.execute("SELECT run_id FROM v_review_queue")]
        loaded = (self.load_result(run_id) for run_id in run_ids)
        return [result for result in loaded if result is not None]

    def rejected_runs(self) -> list[InvoiceRunResult]:
        """Runs whose *effective* status is rejected.

        Filtered here rather than by a view on purpose: the views read
        `runs.final_status`, which is what the pipeline decided, and a
        rejection a person has overturned is no longer a rejection.
        `load_result` is what reconciles the two, so this has to go through it.
        """
        return [r for r in self.load_results() if r.final_status == FinalStatus.REJECTED]

    def money_sent(self) -> dict[str, float]:
        """What actually left the bank, by currency — successful payments only.

        `payments.amount` on a declined row is the sum that was claimed and
        refused, so summing the column blind reports money that never moved.
        """
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT currency, SUM(amount) AS sent FROM payments "
                "WHERE status = ? GROUP BY currency",
                (PaymentStatus.SUCCESS.value,),
            ).fetchall()
        return {row["currency"]: row["sent"] for row in rows}

    # -- reads --------------------------------------------------------------

    def run_usage(self, run_id: str) -> tuple[int, int, float | None]:
        """(llm_calls, total_tokens, cost or None-when-unpriced) for one run."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT llm_calls, total_tokens, cost_usd FROM v_run_summary WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return 0, 0, None
        priced = row["cost_usd"] if row["cost_usd"] else None
        return row["llm_calls"], row["total_tokens"], priced

    def load_results(self) -> list[InvoiceRunResult]:
        """Every run, newest first, reconstructed for the dashboard/exporter."""
        with self.connect() as conn:
            run_ids = [
                r["run_id"]
                for r in conn.execute("SELECT run_id FROM runs ORDER BY id DESC").fetchall()
            ]
        results = [self.load_result(rid) for rid in run_ids]
        return [r for r in results if r is not None]

    def load_result(self, run_id: str) -> InvoiceRunResult | None:
        """Rebuild one run as the InvoiceRunResult the JSON export renders.

        The agents' words come back verbatim; the *effective* final status
        reflects the newest human review, because that is what a reader of
        the export needs to act on — the system's own verdict stays visible
        in `overrides` and `human_reviews`.
        """
        with self.connect() as conn:
            run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if run is None:
                return None
            pk = run["id"]

            invoice = self._load_invoice(conn, pk)
            report = self._load_report(conn, pk, run)
            rounds = self._load_rounds(conn, pk)
            overrides = self._load_overrides(conn, pk)
            reviews = [
                HumanReview(
                    reviewed_at=r["reviewed_at"],
                    reviewer=r["reviewer"],
                    action=r["action"],
                    from_status=FinalStatus(r["from_status"]),
                    to_status=FinalStatus(r["to_status"]),
                    note=r["note"],
                )
                for r in conn.execute(
                    "SELECT * FROM human_reviews WHERE run_id = ? ORDER BY id", (pk,)
                ).fetchall()
            ]
            payment_row = conn.execute("SELECT * FROM payments WHERE run_id = ?", (pk,)).fetchone()
            trace = [
                TraceEvent(stage=t["stage"], event=t["event"], detail=t["detail"], at=t["at"])
                for t in conn.execute(
                    "SELECT * FROM trace_events WHERE run_id = ? ORDER BY seq", (pk,)
                ).fetchall()
            ]
            summary = conn.execute(
                "SELECT document_run_no FROM v_run_summary WHERE run_id = ?", (run_id,)
            ).fetchone()

        decision = None
        if run["decision_status"] is not None and rounds:
            last = rounds[-1].decision
            decision = ApprovalDecision(
                status=ApprovalStatus(run["decision_status"]),
                reasoning=overrides[-1].reasoning if overrides else last.reasoning,
                risk_factors=last.risk_factors,
            )
        final_status = FinalStatus(run["final_status"])
        if reviews:
            final_status = reviews[-1].to_status

        payment = None
        if payment_row is not None:
            payment = PaymentResult(
                status=payment_row["status"],
                vendor=payment_row["vendor"],
                amount=payment_row["amount"],
                reference=payment_row["reference"],
                paid_at=payment_row["paid_at"],
            )

        return InvoiceRunResult(
            run_id=run["run_id"],
            source_file_path=run["source_path"],
            started_at=run["started_at"],
            finished_at=run["finished_at"] or "",
            llm_backend=run["llm_backend"],
            final_status=final_status,
            invoice=invoice,
            validation=report,
            decision=decision,
            critique_rounds=rounds,
            payment=payment,
            error=run["error"],
            trace=trace,
            document_run_no=summary["document_run_no"] if summary else 1,
            overrides=overrides,
            human_reviews=reviews,
            human_reviewed_at=reviews[-1].reviewed_at if reviews else "",
        )

    @staticmethod
    def _load_invoice(conn: sqlite3.Connection, run_pk: int) -> Invoice | None:
        row = conn.execute("SELECT * FROM invoices WHERE run_id = ?", (run_pk,)).fetchone()
        if row is None:
            return None
        items = [
            LineItem(
                item=li["item"],
                quantity=li["quantity"],
                unit_price=li["unit_price"],
                line_total=li["line_total"],
                note=li["note"],
            )
            for li in conn.execute(
                "SELECT * FROM invoice_line_items WHERE invoice_id = ? ORDER BY line_no",
                (row["id"],),
            ).fetchall()
        ]
        return Invoice(
            invoice_number=row["invoice_number"],
            vendor=row["vendor"],
            invoice_date=row["invoice_date"],
            due_date=row["due_date"],
            due_date_raw=row["due_date_raw"],
            line_items=items,
            subtotal=row["subtotal"],
            tax_amount=row["tax_amount"],
            extra_charges=row["extra_charges"],
            total=row["total"],
            currency=row["currency"],
            payment_terms=row["payment_terms"],
            notes=row["notes"],
        )

    @staticmethod
    def _load_report(conn: sqlite3.Connection, run_pk: int, run) -> ValidationReport | None:
        row = conn.execute(
            "SELECT * FROM validation_reports WHERE run_id = ?", (run_pk,)
        ).fetchone()
        if row is None:
            if run["quarantine_reason"]:
                # The gate's verdict is derived, not agent work: rebuild it the
                # same way the graph did, from the reason it recorded.
                return ValidationReport(
                    issues=[
                        ValidationIssue(
                            code=IssueCode.PROMPT_INJECTION_ATTEMPT,
                            severity=Severity.WARNING,
                            detail=run["quarantine_reason"],
                        )
                    ],
                    summary="Quarantined at ingestion: this document forged the pipeline's own "
                    "prompt fences, so it was never shown to a language model. Needs a human "
                    "reader.",
                    tools_used=["prompt_safety_gate"],
                )
            return None
        issues = [
            ValidationIssue(
                code=i["code"], severity=i["severity"], detail=i["detail"], subject=i["subject"]
            )
            for i in conn.execute(
                "SELECT * FROM validation_issues WHERE report_id = ? ORDER BY seq", (row["id"],)
            ).fetchall()
        ]
        tools = conn.execute(
            "SELECT tool_name, invoked_by FROM validation_tool_runs WHERE report_id = ? "
            "ORDER BY seq",
            (row["id"],),
        ).fetchall()
        return ValidationReport(
            issues=issues,
            summary=row["summary"],
            tools_used=[t["tool_name"] for t in tools],
            safety_net_tools=[t["tool_name"] for t in tools if t["invoked_by"] == "safety_net"],
        )

    @staticmethod
    def _load_rounds(conn: sqlite3.Connection, run_pk: int) -> list[CritiqueRound]:
        from .models import Critique, CritiqueVerdict

        rows = conn.execute(
            "SELECT * FROM v_approval_rounds WHERE run_id = ? ORDER BY round_no", (run_pk,)
        ).fetchall()
        rounds = []
        for r in rows:
            if r["critic_verdict"] is None:
                continue  # an approver draft the graph never critiqued (crash)
            rounds.append(
                CritiqueRound(
                    decision=ApprovalDecision(
                        status=ApprovalStatus(r["decision_status"]),
                        reasoning=r["decision_reasoning"],
                        risk_factors=json.loads(r["risk_factors_json"]),
                    ),
                    critique=Critique(
                        verdict=CritiqueVerdict(r["critic_verdict"]),
                        feedback=r["critic_feedback"],
                    ),
                )
            )
        return rounds

    @staticmethod
    def _load_overrides(conn: sqlite3.Connection, run_pk: int) -> list[OverrideRecord]:
        return [
            OverrideRecord(
                round_no=o["round_no"],
                kind=o["kind"],
                from_status=ApprovalStatus(o["from_status"]),
                to_status=ApprovalStatus(o["to_status"]),
                reasoning=o["reasoning"],
                created_at=o["created_at"],
            )
            for o in conn.execute(
                "SELECT do.*, ai.round_no FROM decision_overrides do "
                "JOIN agent_invocations ai ON ai.id = do.invocation_id "
                "WHERE do.run_id = ? ORDER BY do.id",
                (run_pk,),
            ).fetchall()
        ]
