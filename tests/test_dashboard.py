"""Dashboard tests: the human-override paths, driven through Streamlit's own
AppTest runner so the real script executes.

These cover the half of the system no pipeline test reaches — what a person can
do to a run after the agents are done with it. A review never edits the agents'
output: it lands as a human_reviews row, and the effective status is derived.
"""

import pytest
from streamlit.testing.v1 import AppTest

from invoiceflow.config import PROJECT_ROOT
from invoiceflow.models import FinalStatus, InvoiceRunResult
from invoiceflow.pipeline import Pipeline
from invoiceflow.runstore import RunStore
from tests.conftest import INVOICES_DIR

APP = str(PROJECT_ROOT / "ui" / "app.py")


def _export(monkeypatch, settings) -> None:
    """The app builds its own Settings from the environment, so the tmp paths
    have to be exported rather than injected. INBOX_WORKER especially: a thread
    started here would outlive the test, keep polling a database pytest has
    deleted, and build a real ChatXAI from the XAI_API_KEY that .env exports."""
    monkeypatch.setenv("INVOICEFLOW_DB_PATH", str(settings.db_path))
    monkeypatch.setenv("INVOICEFLOW_RUNS_DB_PATH", str(settings.runs_db_path))
    monkeypatch.setenv("INVOICEFLOW_RESULTS_DIR", str(settings.results_dir))
    monkeypatch.setenv("INVOICEFLOW_UPLOADS_DIR", str(settings.uploads_dir))
    monkeypatch.setenv("INVOICEFLOW_INBOX_WORKER", "0")


@pytest.fixture
def empty_dashboard(settings, db, monkeypatch) -> RunStore:
    """A dashboard over a database with no runs at all — a fresh install."""
    _export(monkeypatch, settings)
    return RunStore(settings.runs_db_path)


@pytest.fixture
def dashboard(settings, db, fake_brain, monkeypatch) -> RunStore:
    """A dashboard over two real runs: one paid, one auto-rejected."""
    _export(monkeypatch, settings)
    pipe = Pipeline(settings, llm=fake_brain)
    pipe.run(INVOICES_DIR / "invoice_1001.txt")  # clean -> paid
    rejected = pipe.run(INVOICES_DIR / "invoice_1003.txt")  # zero-stock -> rejected
    assert rejected.final_status == FinalStatus.REJECTED
    return pipe.store


def saved(store: RunStore, run_id: str) -> InvoiceRunResult:
    result = store.load_result(run_id)
    assert result is not None
    return result


def test_rejected_runs_are_actionable_and_counted(dashboard):
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception, [e.value for e in at.exception]
    # Auto-rejections get their own tab, not the escalation queue: the queue
    # means "this needs a decision from you", these are decided already.
    assert "⛔ Rejected (1 unchecked)" in [t.label for t in at.tabs]
    labels = {b.label for b in at.button}
    assert {"✅ Overturn & pay", "⛔ Confirm rejection"} <= labels


def test_confirming_a_rejection_stamps_the_reviewer(dashboard):
    at = AppTest.from_file(APP, default_timeout=60).run()
    confirm = next(b for b in at.button if b.label == "⛔ Confirm rejection")
    assert confirm.key is not None
    run_id = confirm.key.removeprefix("reject-")
    confirm.click().run()
    assert not at.exception, [e.value for e in at.exception]

    result = saved(dashboard, run_id)
    assert result.final_status == FinalStatus.REJECTED  # unchanged
    assert result.human_reviewed_at  # but no longer unchecked
    # The review is its own record; the agent's reasoning is never edited.
    assert result.human_reviews[-1].action == "confirm"
    assert "Human confirmation" not in (result.decision.reasoning if result.decision else "")
    # And it stops being counted as outstanding: the tab loses its "unchecked".
    after = [t.label for t in AppTest.from_file(APP, default_timeout=60).run().tabs]
    assert "⛔ Rejected" in after
    assert not any("unchecked" in label for label in after)


def test_overturning_a_rejection_pays_it(dashboard):
    at = AppTest.from_file(APP, default_timeout=60).run()
    overturn = next(b for b in at.button if b.label == "✅ Overturn & pay")
    assert overturn.key is not None
    run_id = overturn.key.removeprefix("approve-")
    overturn.click().run()
    assert not at.exception, [e.value for e in at.exception]

    result = saved(dashboard, run_id)
    assert result.final_status == FinalStatus.PAID  # effective, via the review
    assert result.payment is not None and result.payment.status == "success"
    assert result.human_reviews[-1].action == "override_approve"
    # The registry follows the human's call, so a resubmission is a duplicate.
    assert result.invoice is not None
    reg = dashboard.get_processed(result.invoice.invoice_number)
    assert reg is not None and reg.final_status == "paid"


class TestUploadAndInbox:
    def test_the_dashboard_opens_on_an_empty_database(self, empty_dashboard):
        """The old st.stop() hid the entire app in exactly this state — the one
        where the upload button that fixes it is the only thing worth showing."""
        at = AppTest.from_file(APP, default_timeout=60).run()
        assert not at.exception, [e.value for e in at.exception]
        labels = [t.label for t in at.tabs]
        assert "📥 Inbox" in labels and "📚 All runs" in labels
        assert "📤 Upload Invoices" in {b.label for b in at.button}

    def test_uploading_queues_a_file_without_processing_it(self, empty_dashboard, settings):
        at = AppTest.from_file(APP, default_timeout=60).run()
        next(b for b in at.button if b.key == "open-upload").click().run()

        data = (INVOICES_DIR / "invoice_1002.txt").read_bytes()
        at.file_uploader[0].set_value(("invoice_1002.txt", data, "text/plain")).run()
        next(b for b in at.button if b.key == "inbox-check").click().run()
        next(b for b in at.button if b.key == "inbox-queue-new").click().run()
        assert not at.exception, [e.value for e in at.exception]

        rows = empty_dashboard.inbox_rows()
        assert len(rows) == 1
        assert rows[0]["filename"] == "invoice_1002.txt" and rows[0]["state"] == "queued"
        # Queuing is not processing: the worker is off, so nothing ran.
        with empty_dashboard.connect() as conn:
            assert conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"] == 0

    def test_re_uploading_a_processed_document_says_so_before_queueing(self, dashboard):
        """beyond-the-brief §17, where it costs nothing: the warning lands
        before the six Grok calls, not after them."""
        at = AppTest.from_file(APP, default_timeout=60).run()
        next(b for b in at.button if b.key == "open-upload").click().run()
        data = (INVOICES_DIR / "invoice_1001.txt").read_bytes()
        at.file_uploader[0].set_value(("invoice_1001.txt", data, "text/plain")).run()
        next(b for b in at.button if b.key == "inbox-check").click().run()

        assert any("already processed" in w.value for w in at.warning)
        # Nothing fresh to queue, so only the explicit "queue all" escape hatch.
        keys = {b.key for b in at.button}
        assert "inbox-queue-all" in keys and "inbox-queue-new" not in keys

    def test_cancelling_leaves_nothing_behind(self, empty_dashboard, settings):
        at = AppTest.from_file(APP, default_timeout=60).run()
        next(b for b in at.button if b.key == "open-upload").click().run()
        data = (INVOICES_DIR / "invoice_1001.txt").read_bytes()
        at.file_uploader[0].set_value(("invoice_1001.txt", data, "text/plain")).run()
        next(b for b in at.button if b.key == "inbox-check").click().run()
        next(b for b in at.button if b.key == "inbox-cancel").click().run()

        assert empty_dashboard.inbox_rows() == []
        assert not list(settings.uploads_dir.iterdir())  # the saved file is gone

    def test_an_unreadable_upload_is_refused_at_the_door(self, empty_dashboard):
        at = AppTest.from_file(APP, default_timeout=60).run()
        next(b for b in at.button if b.key == "open-upload").click().run()
        at.file_uploader[0].set_value(("scan.pdf", b"%PDF-1.4 not really", "application/pdf")).run()
        next(b for b in at.button if b.key == "inbox-check").click().run()

        assert at.error, "an unreadable file must be explained, not queued"
        assert "inbox-queue-new" not in {b.key for b in at.button}
        assert empty_dashboard.inbox_rows() == []

    def test_the_inbox_tab_shows_what_the_worker_did(self, dashboard, settings, fake_brain):
        from invoiceflow import inbox

        path = inbox.save_upload(
            settings.uploads_dir,
            "invoice_1016.json",
            (INVOICES_DIR / "invoice_1016.json").read_bytes(),
        )
        inbox.enqueue(dashboard, inbox.probe_upload(dashboard, path))
        inbox.drain(dashboard, lambda: Pipeline(settings, llm=fake_brain))

        at = AppTest.from_file(APP, default_timeout=60).run()
        assert not at.exception, [e.value for e in at.exception]
        assert "📥 Inbox" in [t.label for t in at.tabs]  # nothing left in flight
        row = dashboard.inbox_rows()[0]
        # An unknown item forces review, so it lands in the escalation queue.
        assert row["state"] == "processed" and row["final_status"] == "needs_review"


class TestARunStillInFlight:
    """`begin_run` writes the row as `failed` / "run did not complete" so a
    crash leaves an honest audit row. Until `finish_run` lands, that makes a
    run in progress look exactly like a crashed one — everywhere the status is
    read without also reading `finished_at`."""

    def test_an_unfinished_run_is_not_reported_as_failed(self, dashboard):
        dashboard.begin_run(
            "in-flight-1",
            "/tmp/invoice_9999.txt",
            "2026-08-24T00:00:00+00:00",
            "grok-4.6",
            "abc123",
        )
        result = saved(dashboard, "in-flight-1")
        # The stored row stays pessimistic, on purpose...
        assert result.final_status == FinalStatus.FAILED
        assert result.error == "run did not complete"
        # ...but it is distinguishable, which is what the dashboard reads.
        assert result.finished_at == ""

        at = AppTest.from_file(APP, default_timeout=60).run()
        assert not at.exception, [e.value for e in at.exception]
        cells = [m.value for m in at.markdown]
        assert "invoice_9999.txt" in cells  # the row is listed...
        assert "⚙️ processing" in cells  # ...as processing
        assert "💥 failed" not in cells
        assert any(b.key == "toggle-in-flight-1" for b in at.button)

    def test_a_genuinely_failed_run_still_reads_as_failed(self, dashboard, settings, fake_brain):
        """The other half: once a run lands, a failure must look like one."""
        pipe = Pipeline(settings, llm=fake_brain)
        result = pipe.run(INVOICES_DIR / "does_not_exist.txt")
        assert result.final_status == FinalStatus.FAILED and result.finished_at

        at = AppTest.from_file(APP, default_timeout=60).run()
        cells = [m.value for m in at.markdown]
        assert "does_not_exist.txt" in cells and "💥 failed" in cells
        assert any(b.key == f"toggle-{result.run_id}" for b in at.button)

    def test_the_in_flight_run_explains_itself_instead_of_showing_an_error(self, dashboard):
        dashboard.begin_run(
            "in-flight-2",
            "/tmp/invoice_8888.txt",
            "2026-08-24T00:00:00+00:00",
            "grok-4.6",
            "abc123",
        )
        at = AppTest.from_file(APP, default_timeout=60).run()
        # Rows render collapsed, so the detail is not built until it is opened.
        assert not any("Still processing" in i.value for i in at.info)
        next(b for b in at.button if b.key == "toggle-in-flight-2").click().run()
        assert not at.exception, [e.value for e in at.exception]
        assert any("Still processing" in i.value for i in at.info)
        # begin_run's placeholder must not be rendered as a red failure.
        assert not any("run did not complete" in e.value for e in at.error)


def test_the_needs_review_tab_counts_what_is_waiting(dashboard, settings, fake_brain):
    """Empty until something escalates, then it carries a count like the others."""
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert "🟡 Needs review" in [t.label for t in at.tabs]  # fixture has none yet

    # An unknown item forces review, however the Approver felt about it.
    pipe = Pipeline(settings, llm=fake_brain)
    escalated = pipe.run(INVOICES_DIR / "invoice_1016.json")
    assert escalated.final_status == FinalStatus.NEEDS_REVIEW

    after = [t.label for t in AppTest.from_file(APP, default_timeout=60).run().tabs]
    assert "🟡 Needs review (1)" in after


def test_a_row_opens_and_closes_on_a_single_click(dashboard):
    """The toggle is an on_click callback, so the button label and the pane
    below it agree on the same pass. Reading the button's return value instead
    would leave it saying "View" over an already-open detail."""
    at = AppTest.from_file(APP, default_timeout=60).run()
    logs = lambda: sum(1 for e in at.expander if e.label == "Activity log")
    before = logs()

    row = next(b for b in at.button if b.key and b.key.startswith("toggle-"))
    assert row.label == "View"
    row.click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert next(b for b in at.button if b.key == row.key).label == "Hide"
    assert logs() == before + 1  # exactly one detail opened

    next(b for b in at.button if b.key == row.key).click().run()
    assert next(b for b in at.button if b.key == row.key).label == "View"
    assert logs() == before
