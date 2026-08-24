"""The run store: schema integrity, run round-trips, telemetry, human review.

The pipeline-level behavior (statuses, registry ordering, routing) lives in
test_pipeline_e2e.py; this file covers what the *database* guarantees — that
every artifact of a run lands, joins correctly, and comes back verbatim.
"""

import re
import sqlite3
import threading

import pytest

from invoiceflow import runstore
from invoiceflow.models import FinalStatus, IssueCode
from invoiceflow.pipeline import export_result_json
from invoiceflow.runstore import (
    ISSUE_CATEGORIES,
    MODEL_PRICING,
    PRICING_EFFECTIVE_FROM,
    SCHEMA_PATH,
    RunStore,
    _created_object,
    _statements,
)
from tests.conftest import INVOICES_DIR

#: Everything the precedent feature added to the schema, for the growth test.
PRECEDENT_OBJECTS = {
    "precedent_citations",
    "v_review_precedent",
    "v_precedent_learning",
    "idx_issues_subject",
}


def _pre_precedent_schema() -> str:
    """schema.sql as it stood before any of this existed.

    Reconstructed from the current file rather than pasted in: a copy of the old
    DDL would rot the moment anything unrelated changed, and then this test
    would be exercising a database shape nobody ever had.
    """
    script = "".join(
        statement
        for statement in _statements(SCHEMA_PATH.read_text())
        if _created_object(statement) not in PRECEDENT_OBJECTS
    )
    before = script
    script = script.replace("    subject   TEXT    NOT NULL DEFAULT '',\n", "")
    script = script.replace(
        "CHECK (kind IN\n"
        "                           ('reject','review','scrutiny','advisory','precedent'))",
        "CHECK (kind IN ('reject','review','scrutiny','advisory'))",
    )
    assert script != before, "the schema no longer contains what this test rolls back"
    return script


def _object_shapes_of(store) -> dict[str, str]:
    with store.connect() as conn:
        return {
            name: re.sub(r"\s+", " ", sql).strip()
            for name, sql in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL"
            )
        }


def _object_shapes(script: str) -> dict[str, str]:
    """name -> normalized SQL for every table/view/index in `script`."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(script)
    return {
        name: re.sub(r"\s+", " ", sql).strip()
        for name, sql in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL"
        ).fetchall()
    }


class TestSchema:
    def test_schema_doc_and_schema_sql_agree(self):
        """docs/schema.md is the design record; schema.sql is what runs. This
        keeps them the same database, modulo the amendments the .sql header
        declares (nullable document_id and its view guards)."""
        doc = (SCHEMA_PATH.parent.parent.parent / "docs" / "schema.md").read_text()
        doc_sql = "\n".join(re.findall(r"```sql\n(.*?)```", doc, re.DOTALL))
        doc_shapes = _object_shapes(doc_sql)
        sql_shapes = _object_shapes(SCHEMA_PATH.read_text())
        assert set(doc_shapes) == set(sql_shapes)
        declared_amendments = {"runs", "v_run_summary"}
        drifted = {
            name
            for name in doc_shapes
            if doc_shapes[name] != sql_shapes[name] and name not in declared_amendments
        }
        assert not drifted, f"schema.sql drifted from docs/schema.md: {sorted(drifted)}"

    def test_a_schema_change_reaches_a_database_that_already_exists(self, tmp_path):
        """Same argument as the issue_codes re-seed below, one level up. A
        column, a table, a view and a widened CHECK all arrived with precedent;
        a database created before them has to grow them on open, or the first
        run to need one dies on a real invoice — and takes an audit trail with
        it if the answer is "recreate the database"."""
        path = tmp_path / "older.db"
        conn = sqlite3.connect(path)
        conn.executescript(_pre_precedent_schema())
        conn.execute(
            "INSERT INTO runs (run_id, source_path, started_at, final_status) "
            "VALUES ('older-0001', 'x.txt', '2026-01-01T00:00:00+00:00', 'paid')"
        )
        conn.execute(
            "INSERT INTO rule_evaluations (run_id, must_reject, must_review, "
            "requires_scrutiny, scrutiny_threshold) VALUES (1, 0, 0, 0, 10000)"
        )
        conn.execute(
            "INSERT INTO rule_reasons (rule_evaluation_id, kind, reason) "
            "VALUES (1, 'advisory', 'something older')"
        )
        conn.commit()
        conn.close()

        store = RunStore(path)  # opening it is what grows the schema

        with store.connect() as conn:
            names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master")}
            assert names >= PRECEDENT_OBJECTS
            columns = {r[1] for r in conn.execute("PRAGMA table_info(validation_issues)")}
            assert "subject" in columns
            # The rebuilt table kept every row it had...
            kept = conn.execute("SELECT reason FROM rule_reasons").fetchall()
            assert [r["reason"] for r in kept] == ["something older"]
            # ...and accepts the kind the old CHECK forbade.
            conn.execute(
                "INSERT INTO rule_reasons (rule_evaluation_id, kind, reason) "
                "VALUES (1, 'precedent', 'settled by precedent')"
            )

    def test_growing_the_schema_twice_changes_nothing(self, tmp_path):
        """Every step is conditional, so a second open must not rebuild a table
        that is already current — that is how a rebuild loses rows."""
        path = tmp_path / "older.db"
        conn = sqlite3.connect(path)
        conn.executescript(_pre_precedent_schema())
        conn.close()

        first = _object_shapes_of(RunStore(path))
        again = _object_shapes_of(RunStore(path))
        assert first == again
        assert not any(name.endswith("__new") for name in first)

    def test_a_rebuilt_table_is_rebuilt_once_and_never_again(self, tmp_path, monkeypatch):
        """The shape check above cannot see this: a rebuild leaves the table
        looking exactly as it did, so rebuilding on every open looks identical
        to not rebuilding at all. It is not identical — SQLite stores the
        renamed table as CREATE TABLE "rule_reasons", and comparing that with
        schema.sql's unquoted name kept the rebuild firing forever, copying
        live rows on every open of the dashboard."""
        path = tmp_path / "older.db"
        conn = sqlite3.connect(path)
        conn.executescript(_pre_precedent_schema())
        conn.close()

        rebuilt: list[str] = []
        real = runstore._rebuild_table

        def spy(conn, name, statement):
            rebuilt.append(name)
            return real(conn, name, statement)

        monkeypatch.setattr(runstore, "_rebuild_table", spy)
        RunStore(path)
        RunStore(path)
        RunStore(path)
        assert rebuilt == ["rule_reasons"]

    def test_two_threads_can_open_the_same_database_at_once(self, tmp_path):
        """What the dashboard actually does: a store per Streamlit script run
        and another on the inbox worker, several of them alive at the same
        moment. Both used to survey the schema and then change it with nothing
        holding the two together, so an upload during the Inbox tab's two-second
        poll died on `table rule_reasons__new already exists`."""
        path = tmp_path / "older.db"
        conn = sqlite3.connect(path)
        conn.executescript(_pre_precedent_schema())
        conn.execute(
            "INSERT INTO runs (run_id, source_path, started_at, final_status) "
            "VALUES ('older-0001', 'x.txt', '2026-01-01T00:00:00+00:00', 'paid')"
        )
        conn.execute(
            "INSERT INTO rule_evaluations (run_id, must_reject, must_review, "
            "requires_scrutiny, scrutiny_threshold) VALUES (1, 0, 0, 0, 10000)"
        )
        conn.execute(
            "INSERT INTO rule_reasons (rule_evaluation_id, kind, reason) "
            "VALUES (1, 'advisory', 'something older')"
        )
        conn.commit()
        conn.close()

        failures: list[str] = []
        start = threading.Barrier(6)

        def open_store() -> None:
            start.wait()  # all six inside _ensure_schema together, not in turn
            try:
                RunStore(path)
            except Exception as exc:  # a thread's traceback goes nowhere; collect it
                failures.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=open_store) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert not failures
        with RunStore(path).connect() as conn:
            kept = [r["reason"] for r in conn.execute("SELECT reason FROM rule_reasons")]
            assert kept == ["something older"]  # six rebuilds, one row, still one row
            names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master")}
            assert not any(name.endswith("__new") for name in names)

    def test_an_abandoned_rebuild_does_not_wedge_the_database(self, tmp_path):
        """Scaffolding left by an interrupted rebuild used to be permanent: the
        next open tried to create it again and raised, so every open after that
        one raised too. It is cleared instead, and the real table's rows — the
        scaffold never holds one they do not — are untouched."""
        path = tmp_path / "older.db"
        conn = sqlite3.connect(path)
        conn.executescript(_pre_precedent_schema())
        conn.execute(
            "INSERT INTO runs (run_id, source_path, started_at, final_status) "
            "VALUES ('older-0001', 'x.txt', '2026-01-01T00:00:00+00:00', 'paid')"
        )
        conn.execute(
            "INSERT INTO rule_evaluations (run_id, must_reject, must_review, "
            "requires_scrutiny, scrutiny_threshold) VALUES (1, 0, 0, 0, 10000)"
        )
        conn.execute(
            "INSERT INTO rule_reasons (rule_evaluation_id, kind, reason) "
            "VALUES (1, 'advisory', 'something older')"
        )
        conn.execute("CREATE TABLE rule_reasons__new (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()

        with RunStore(path).connect() as conn:
            kept = [r["reason"] for r in conn.execute("SELECT reason FROM rule_reasons")]
            assert kept == ["something older"]
            names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master")}
            assert not any(name.endswith("__new") for name in names)

    def test_a_new_issue_code_reaches_an_existing_database(self, store):
        """Adding an IssueCode must not require a hand-written migration.
        validation_issues.code has a live FK to issue_codes, so a database
        created before the code existed would reject the first issue raising
        it — on a real invoice, at run time."""
        store.init()
        with store.connect() as conn:
            conn.execute("DELETE FROM issue_codes WHERE code = ?", (IssueCode.UNKNOWN_ITEM.value,))
            assert not conn.execute(
                "SELECT 1 FROM issue_codes WHERE code = ?", (IssueCode.UNKNOWN_ITEM.value,)
            ).fetchone()
        store.init()  # a plain re-init, exactly what `--init-db` does
        with store.connect() as conn:
            assert conn.execute(
                "SELECT 1 FROM issue_codes WHERE code = ?", (IssueCode.UNKNOWN_ITEM.value,)
            ).fetchone()

    def test_registry_never_downgrades_a_paid_invoice(self, store):
        """`outstanding_balance` reads the registry to decide what is still
        owed, so letting a later non-paid run clear the paid flag is exactly
        how an invoice becomes payable twice. A dashboard approval used to do
        precisely that."""
        store.init()
        store.record_processed("INV-1004", "hash-v1", "Precision Parts", 1_890.0, "paid", None)

        wrote = store.record_settlement(
            "INV-1004", "hash-r1", "Precision Parts", 5_940.0, "duplicate", None
        )
        assert wrote is False
        prior = store.get_processed("INV-1004")
        assert prior.final_status == "paid" and prior.total == 1_890.0

        # ...but a genuine settlement of the balance does update it.
        wrote = store.record_settlement(
            "INV-1004", "hash-r1", "Precision Parts", 5_940.0, "paid", None
        )
        assert wrote is True
        prior = store.get_processed("INV-1004")
        assert prior.final_status == "paid" and prior.total == 5_940.0

    def test_review_queue_holds_exactly_what_awaits_a_decision(self, settings, db, fake_brain):
        """Membership comes from `v_review_queue`, so the dashboard and the
        analytics cannot disagree about what is waiting on a person."""
        from invoiceflow.pipeline import Pipeline
        from invoiceflow.review import apply_human_review

        pipe = Pipeline(settings, llm=fake_brain)
        escalated = pipe.run(INVOICES_DIR / "invoice_1008.txt")  # unknown items
        pipe.run(INVOICES_DIR / "invoice_1001.txt")  # clean -> paid
        pipe.run(INVOICES_DIR / "invoice_1003.txt")  # zero stock -> rejected
        assert [r.run_id for r in pipe.store.review_queue()] == [escalated.run_id]

        # Deciding it takes it out of the queue; the view's own
        # `human_reviewed_at IS NULL` is what does that, not the status.
        apply_human_review(pipe.store, escalated, approve=False, note="not ours")
        assert pipe.store.review_queue() == []

    def test_review_queue_and_rejected_track_the_effective_status(self, settings, db, fake_brain):
        """A human's call moves a run between the two tabs; the stored status
        the views read never changes."""
        from invoiceflow.pipeline import Pipeline
        from invoiceflow.review import apply_human_review

        pipe = Pipeline(settings, llm=fake_brain)
        rejected = pipe.run(INVOICES_DIR / "invoice_1003.txt")
        assert [r.run_id for r in pipe.store.rejected_runs()] == [rejected.run_id]

        apply_human_review(pipe.store, rejected, approve=True, note="overturned")
        # Overturned, so it is no longer a rejection — even though runs.final_status
        # still says 'rejected' and every view still reads it that way.
        assert pipe.store.rejected_runs() == []
        with pipe.store.connect() as conn:
            stored = conn.execute(
                "SELECT final_status FROM runs WHERE run_id = ?", (rejected.run_id,)
            ).fetchone()["final_status"]
        assert stored == "rejected"

    def test_every_issue_code_is_seeded(self, store):
        assert set(ISSUE_CATEGORIES) == set(IssueCode)
        with store.connect() as conn:
            seeded = {r["code"] for r in conn.execute("SELECT code FROM issue_codes")}
        assert seeded == {c.value for c in IssueCode}

    def test_status_typo_is_rejected_at_the_boundary(self, store):
        # D4: the CHECK constraints are the DB-side FinalStatus.
        with store.connect() as conn, pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO runs (run_id, source_path, started_at, final_status) "
                "VALUES ('x', 'x', 'now', 'payed')"
            )

    def test_same_content_at_two_paths_is_one_document(self, store):
        doc1, prior1 = store.register_document("INVOICE TEXT", "data/invoices/a.txt")
        doc2, prior2 = store.register_document("INVOICE TEXT", "inbox/copy.txt")
        assert doc1 == doc2
        assert (prior1, prior2) == (0, 0)  # no runs yet, just one identity


class TestRunRoundTrip:
    @pytest.fixture
    def paid_run(self, pipeline):
        return pipeline.run(INVOICES_DIR / "invoice_1001.txt")

    def test_full_spine_is_recorded(self, pipeline, paid_run):
        with pipeline.store.connect() as conn:
            turns = conn.execute(
                "SELECT agent, node, outcome FROM agent_invocations ai "
                "JOIN runs r ON r.id = ai.run_id WHERE r.run_id = ? ORDER BY seq",
                (paid_run.run_id,),
            ).fetchall()
        assert [(t["agent"], t["node"]) for t in turns] == [
            ("extractor", "ingest"),
            ("validator", "validate"),
            ("approver", "decide"),
            ("critic", "critique"),
        ]
        assert all(t["outcome"] == "ok" for t in turns)

    def test_every_turn_carries_llm_calls_with_usage(self, pipeline, paid_run):
        """The telemetry path end to end: FakeBrain emits usage metadata the
        same way Grok does, so every turn must have priced-able call rows."""
        with pipeline.store.connect() as conn:
            rows = conn.execute(
                "SELECT ai.agent, COUNT(lc.id) AS calls, SUM(lc.total_tokens) AS tokens "
                "FROM agent_invocations ai "
                "JOIN runs r ON r.id = ai.run_id "
                "LEFT JOIN llm_calls lc ON lc.invocation_id = ai.id "
                "WHERE r.run_id = ? GROUP BY ai.agent",
                (paid_run.run_id,),
            ).fetchall()
        by_agent = {r["agent"]: (r["calls"], r["tokens"]) for r in rows}
        # Validator: tool round + empty round + summary = 3; the rest 1 each.
        assert by_agent["validator"][0] == 3
        assert by_agent["extractor"][0] == by_agent["approver"][0] == 1
        assert by_agent["critic"][0] == 1
        assert all(tokens > 0 for _, tokens in by_agent.values())

    def test_result_reloads_verbatim(self, pipeline, paid_run):
        reloaded = pipeline.store.load_result(paid_run.run_id)
        assert reloaded is not None
        assert reloaded.final_status == FinalStatus.PAID
        assert reloaded.invoice == paid_run.invoice
        assert reloaded.decision.reasoning == paid_run.decision.reasoning
        assert [r.critique.verdict for r in reloaded.critique_rounds] == [
            r.critique.verdict for r in paid_run.critique_rounds
        ]
        assert reloaded.payment is not None and reloaded.payment.amount == 5000.0
        assert [e.event for e in reloaded.trace] == [e.event for e in paid_run.trace]

    def test_export_json_matches_the_returned_result(self, pipeline, paid_run, settings):
        out = export_result_json(pipeline.store, paid_run.run_id, settings.results_dir)
        assert out.read_text() == pipeline.store.load_result(paid_run.run_id).model_dump_json(
            indent=2
        )

    def test_rule_constraints_are_persisted(self, pipeline):
        """The data the old JSON dropped entirely: why an outcome was forced."""
        rejected = pipeline.run(INVOICES_DIR / "invoice_1002.txt")
        with pipeline.store.connect() as conn:
            ev = conn.execute(
                "SELECT re.* FROM rule_evaluations re JOIN runs r ON r.id = re.run_id "
                "WHERE r.run_id = ?",
                (rejected.run_id,),
            ).fetchone()
            reasons = conn.execute(
                "SELECT kind, reason FROM rule_reasons WHERE rule_evaluation_id = ?",
                (ev["id"],),
            ).fetchall()
        assert ev["must_reject"] == 1
        assert ev["scrutiny_threshold"] == pipeline.settings.scrutiny_threshold
        assert any(r["kind"] == "reject" and "stock" in r["reason"] for r in reasons)

    def test_crash_leaves_an_honest_failed_row(self, pipeline, monkeypatch):
        """begin_run is pessimistic on purpose: if the process dies mid-run,
        the audit row already says failed instead of saying nothing."""

        def boom(*args, **kwargs):
            raise RuntimeError("simulated crash inside the graph")

        monkeypatch.setattr(pipeline.graph, "invoke", boom)
        with pytest.raises(RuntimeError):
            pipeline.run(INVOICES_DIR / "invoice_1001.txt")
        with pipeline.store.connect() as conn:
            row = conn.execute("SELECT final_status, error FROM runs").fetchone()
        assert row["final_status"] == "failed"
        assert row["error"] == "run did not complete"


class TestSelfCorrectionEvidence:
    def test_extraction_attempts_are_kept(self, settings, db, store, ground_truth):
        from invoiceflow.pipeline import Pipeline
        from tests.test_pipeline_e2e import AmnesiacExtractor

        pipe = Pipeline(settings, llm=AmnesiacExtractor(extractions=ground_truth))
        result = pipe.run(INVOICES_DIR / "invoice_1001.txt")
        assert result.final_status == FinalStatus.PAID
        with pipe.store.connect() as conn:
            attempts = conn.execute(
                "SELECT attempt_no, problems FROM extraction_attempts"
            ).fetchall()
            correction = conn.execute(
                "SELECT * FROM v_self_correction WHERE run_id = ?", (result.run_id,)
            ).fetchone()
        assert len(attempts) == 1
        assert "invoice_number is empty" in attempts[0]["problems"]
        assert correction["extraction_retries"] == 1


class TestTelemetryPricing:
    def test_cost_appears_only_when_priced(self, pipeline):
        run = pipeline.run(INVOICES_DIR / "invoice_1001.txt")
        calls, tokens, cost = pipeline.store.run_usage(run.run_id)
        assert calls == 6 and tokens > 0
        assert cost is None  # no pricing row: never a made-up $0

        pipeline.store.set_model_pricing(
            "FakeBrain",
            input_usd_per_mtok=1.0,
            output_usd_per_mtok=5.0,
            effective_from="2000-01-01",
        )
        priced = pipeline.run(INVOICES_DIR / "invoice_1004.json")
        _, _, cost2 = pipeline.store.run_usage(priced.run_id)
        with pipeline.store.connect() as conn:
            expected = conn.execute(
                "SELECT SUM((lc.input_tokens * 1.0 + lc.output_tokens * 5.0) / 1e6) AS c "
                "FROM llm_calls lc JOIN agent_invocations ai ON ai.id = lc.invocation_id "
                "JOIN runs r ON r.id = ai.run_id WHERE r.run_id = ?",
                (priced.run_id,),
            ).fetchone()["c"]
        assert cost2 == pytest.approx(expected, abs=1e-4)

    def test_the_backend_is_priced_out_of_the_box(self, store):
        """A fresh database already knows what Grok costs — otherwise the first
        thing anybody sees on the dashboard is a dash where the money goes."""
        with store.connect() as conn:
            rows = {r["model"]: r for r in conn.execute("SELECT * FROM model_pricing")}
        assert set(rows) == set(MODEL_PRICING)
        seeded, rate = rows["grok-4.6"], MODEL_PRICING["grok-4.6"]
        assert seeded["input_usd_per_mtok"] == rate.input_usd_per_mtok
        assert seeded["cached_input_usd_per_mtok"] == rate.cached_input_usd_per_mtok
        assert seeded["output_usd_per_mtok"] == rate.output_usd_per_mtok
        # Below any timestamp a call can carry, so history prices too.
        assert seeded["effective_from"] < "1971"

    def test_a_hand_set_rate_survives_a_later_init(self, store):
        """The seed states a published price; it does not overrule one somebody
        put there on purpose."""
        store.set_model_pricing(
            "grok-4.6",
            input_usd_per_mtok=99.0,
            output_usd_per_mtok=99.0,
            effective_from=PRICING_EFFECTIVE_FROM,
        )
        store.init()
        with store.connect() as conn:
            row = conn.execute("SELECT * FROM model_pricing WHERE model = 'grok-4.6'").fetchone()
        assert row["input_usd_per_mtok"] == 99.0

    def test_init_prices_calls_recorded_before_the_rate_existed(self, pipeline):
        """Costs that were NULL because no price was on file get one; costs
        already snapshotted are left exactly as they were billed."""
        run = pipeline.run(INVOICES_DIR / "invoice_1001.txt")
        _, _, cost = pipeline.store.run_usage(run.run_id)
        assert cost is None  # FakeBrain is not a priced model

        pipeline.store.set_model_pricing(
            "FakeBrain",
            input_usd_per_mtok=1.0,
            output_usd_per_mtok=5.0,
            effective_from="2000-01-01",
        )
        assert pipeline.store.init() == 6  # the six calls of that first run
        _, _, backfilled = pipeline.store.run_usage(run.run_id)
        # v_run_summary rounds to the cent's cent, like the first test here.
        assert backfilled == pytest.approx(
            _expected_cost(pipeline.store, run.run_id, 1.0, 5.0), abs=1e-4
        )

        # Idempotent, and never a re-pricing: a second init at a new rate
        # leaves the snapshot alone.
        pipeline.store.set_model_pricing(
            "FakeBrain",
            input_usd_per_mtok=1000.0,
            output_usd_per_mtok=1000.0,
            effective_from="2001-01-01",
        )
        assert pipeline.store.init() == 0
        _, _, unchanged = pipeline.store.run_usage(run.run_id)
        assert unchanged == pytest.approx(backfilled, abs=1e-9)


def _expected_cost(store, run_id: str, in_rate: float, out_rate: float) -> float:
    with store.connect() as conn:
        return conn.execute(
            "SELECT SUM((lc.input_tokens * ? + lc.output_tokens * ?) / 1e6) AS c "
            "FROM llm_calls lc JOIN agent_invocations ai ON ai.id = lc.invocation_id "
            "JOIN runs r ON r.id = ai.run_id WHERE r.run_id = ?",
            (in_rate, out_rate, run_id),
        ).fetchone()["c"]


class TestReprocessDetection:
    def test_rerun_of_same_content_is_flagged_not_blocked(self, pipeline):
        first = pipeline.run(INVOICES_DIR / "invoice_1001.txt")
        second = pipeline.run(INVOICES_DIR / "invoice_1001.txt")
        assert first.document_run_no == 1
        assert second.document_run_no == 2
        assert any(e.event == "reprocessed_document" for e in second.trace)
        # It still ran (D10: a real run, never suppressed) — and the money is
        # still safe, via the registry, not via refusal to run.
        assert second.final_status == FinalStatus.DUPLICATE
        with pipeline.store.connect() as conn:
            row = conn.execute("SELECT * FROM v_reprocessed_documents").fetchone()
        assert row["run_count"] == 2

    def test_cross_format_pair_is_not_a_reprocess(self, pipeline):
        """Same invoice number in .txt and .pdf is different content — the
        duplicate check catches the money, not the document identity."""
        pipeline.run(INVOICES_DIR / "invoice_1011.txt")
        pdf = pipeline.run(INVOICES_DIR / "invoice_1011.pdf")
        assert pdf.document_run_no == 1  # different document, first run
        assert pdf.final_status == FinalStatus.DUPLICATE  # but never paid twice


class TestHumanReview:
    def test_review_never_edits_the_agents_words(self, pipeline, store):
        run = pipeline.run(INVOICES_DIR / "invoice_1002.txt")  # auto-rejected
        before = store.load_result(run.run_id).decision.reasoning
        store.add_human_review(
            run.run_id,
            action="confirm",
            from_status=FinalStatus.REJECTED,
            to_status=FinalStatus.REJECTED,
            note="checked against the PO",
        )
        after = store.load_result(run.run_id)
        assert after.decision.reasoning == before
        assert after.human_reviews[-1].note == "checked against the PO"
        assert after.human_reviewed_at
