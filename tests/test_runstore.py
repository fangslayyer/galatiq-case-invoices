"""The run store: schema integrity, run round-trips, telemetry, human review.

The pipeline-level behavior (statuses, registry ordering, routing) lives in
test_pipeline_e2e.py; this file covers what the *database* guarantees — that
every artifact of a run lands, joins correctly, and comes back verbatim.
"""

import re
import sqlite3

import pytest

from invoiceflow.models import FinalStatus, IssueCode
from invoiceflow.pipeline import export_result_json
from invoiceflow.runstore import ISSUE_CATEGORIES, SCHEMA_PATH
from tests.conftest import INVOICES_DIR


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
