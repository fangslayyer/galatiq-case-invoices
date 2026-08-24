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
        """The warning lands before the six Grok calls, not after them."""
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

    def logs() -> int:
        return sum(1 for e in at.expander if e.label == "Activity log")

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


class TestTheOriginalDocument:
    """Every invoice can show what actually arrived, whatever the format."""

    def open_first_row(self, at):
        next(b for b in at.button if b.key and b.key.startswith("toggle-")).click().run()
        return at

    def test_a_text_invoice_shows_what_the_extractor_read(self, dashboard):
        at = self.open_first_row(AppTest.from_file(APP, default_timeout=60).run())
        assert not at.exception, [e.value for e in at.exception]
        assert any(e.label == "Original document" for e in at.expander)
        # The stored text is the file itself for a non-PDF format.
        source = (INVOICES_DIR / "invoice_1003.txt").read_text().strip()
        assert any(c.value.strip() == source for c in at.code)
        assert any(b.label.startswith("Download ") for b in at.download_button)

    def test_a_pdf_offers_the_page_and_the_extracted_text(
        self, settings, db, fake_brain, monkeypatch
    ):
        _export(monkeypatch, settings)
        pipe = Pipeline(settings, llm=fake_brain)
        result = pipe.run(INVOICES_DIR / "invoice_1011.pdf")
        assert result.final_status  # it ran

        at = self.open_first_row(AppTest.from_file(APP, default_timeout=60).run())
        assert not at.exception, [e.value for e in at.exception]
        # The page itself — st.pdf ships as a component, so it lands in the
        # tree as a bidi_component rather than a typed element.
        assert len(at.get("bidi_component")) == 1
        # ...and separately what pdfplumber pulled out of it, which is the
        # version the Extractor actually saw.
        stored = pipe.store.document_for_run(result.run_id)
        assert stored is not None and stored.file_format == "pdf"
        assert any(c.value.strip() == stored.raw_text.strip() for c in at.code)

    def test_a_deleted_upload_says_so_instead_of_breaking(
        self, settings, db, fake_brain, monkeypatch
    ):
        _export(monkeypatch, settings)
        from invoiceflow import inbox

        path = inbox.save_upload(
            settings.uploads_dir,
            "invoice_1001.txt",
            (INVOICES_DIR / "invoice_1001.txt").read_bytes(),
        )
        Pipeline(settings, llm=fake_brain).run(path)
        path.unlink()  # the spool was cleaned up after processing

        at = self.open_first_row(AppTest.from_file(APP, default_timeout=60).run())
        assert not at.exception, [e.value for e in at.exception]
        assert any("no longer on disk" in c.value for c in at.caption)
        assert not any(b.label.startswith("Download ") for b in at.download_button)
        # The text survives regardless: it lives in the database, not the file.
        assert any("Widgets Inc" in c.value for c in at.code)


class TestABatchStillArriving:
    """The Inbox polls inside a fragment, so a run landing mid-batch redraws
    that one table. Everything else — both queues, All runs, the counts in the
    tab labels — is drawn by a *full* run, and until one happens a finished
    invoice is invisible outside the Inbox. The polling timer is the browser's
    and out of AppTest's reach; what is testable is the mark the fragment polls
    against, and that the page does publish a run mid-batch when it reruns.
    """

    #: Two more files behind the one that lands, so the batch is still moving.
    BATCH = ("invoice_1016.json", "invoice_1002.txt", "invoice_1005.json")

    def queue_batch(self, store, settings) -> None:
        from invoiceflow import inbox

        for name in self.BATCH:
            path = inbox.save_upload(settings.uploads_dir, name, (INVOICES_DIR / name).read_bytes())
            inbox.enqueue(store, inbox.probe_upload(store, path))

    def land_one(self, store, settings, fake_brain) -> None:
        from invoiceflow import inbox

        assert inbox.process_one(store, lambda: Pipeline(settings, llm=fake_brain)) is not None

    def test_a_landed_run_reaches_the_rest_of_the_page(self, dashboard, settings, fake_brain):
        self.queue_batch(dashboard, settings)
        self.land_one(dashboard, settings, fake_brain)  # one of three

        at = AppTest.from_file(APP, default_timeout=60).run()
        assert not at.exception, [e.value for e in at.exception]
        labels = [t.label for t in at.tabs]
        assert "📥 Inbox (2 in flight)" in labels  # the batch is still moving
        assert "🟡 Needs review (1)" in labels  # ...and the one that landed counts
        # The mark the fragment compares against, so it can tell that a run has
        # landed since this page was drawn.
        assert at.session_state["inbox.landed"] == 1

        waiting = dashboard.review_queue()
        assert len(waiting) == 1 and waiting[0].invoice is not None
        number = waiting[0].invoice.invoice_number
        assert any(number in h.value for h in at.subheader)  # Needs review
        assert any(number in m.value for m in at.markdown)  # All runs

    def test_a_refresh_mid_batch_keeps_the_tab_the_reader_is_on(
        self, dashboard, settings, fake_brain
    ):
        """Streamlit identifies a tab by its label, and every count in the bar
        moves when a run lands. Without a default pinned to the tab in front of
        the reader, each refresh would drop them back on the Inbox."""
        self.queue_batch(dashboard, settings)
        at = AppTest.from_file(APP, default_timeout=60).run()
        at.session_state["tabs.active"] = "🟡 Needs review"

        self.land_one(dashboard, settings, fake_brain)  # relabels every tab
        at.run()
        assert not at.exception, [e.value for e in at.exception]
        labels = [t.label for t in at.tabs]
        assert "🟡 Needs review (1)" in labels  # the label it was selected by is gone
        container = at.get("tab_container")[0]
        assert labels[container.proto.tab_container.default_tab_index] == "🟡 Needs review (1)"


class TestAQuarantinedDocument:
    """A forged-fence document is stopped before the Extractor, so the run has
    no invoice at all. Every pane that names a run by its invoice has to cope."""

    def quarantined_upload(self, settings, fake_brain):
        """The poisoned demo file, uploaded the way a person would send it —
        so its path is the deep spool path, not something a title can borrow."""
        from invoiceflow import inbox

        source = PROJECT_ROOT / "data" / "demo" / "injection" / "invoice_5001.txt"
        path = inbox.save_upload(settings.uploads_dir, source.name, source.read_bytes())
        result = Pipeline(settings, llm=fake_brain).run(path)
        assert result.final_status == FinalStatus.NEEDS_REVIEW
        assert result.invoice is None  # the gate fired before extraction
        return result

    def test_it_is_titled_by_its_filename_not_its_path(self, settings, db, fake_brain, monkeypatch):
        _export(monkeypatch, settings)
        result = self.quarantined_upload(settings, fake_brain)

        at = AppTest.from_file(APP, default_timeout=60).run()
        assert not at.exception, [e.value for e in at.exception]
        headings = [h.value for h in at.subheader]
        assert "invoice_5001.txt · quarantined, never extracted" in headings
        # The old title was the absolute path of the spooled upload.
        assert not any(str(settings.uploads_dir) in h for h in headings)
        assert result.source_file_path not in " ".join(headings)

    def test_the_empty_invoice_pane_explains_itself(self, settings, db, fake_brain, monkeypatch):
        _export(monkeypatch, settings)
        self.quarantined_upload(settings, fake_brain)

        at = AppTest.from_file(APP, default_timeout=60).run()
        assert not at.exception, [e.value for e in at.exception]
        assert any("Quarantined at ingestion" in w.value for w in at.warning)
        # ...and the document it refers to is open, not folded away: it is the
        # only thing a reviewer can actually read.
        pane = next(e for e in at.expander if e.label == "Original document")
        assert pane.proto.expanded
        assert any("Meridian Office Supply" in c.value for c in at.code)


def test_the_inbox_renders_finished_and_queued_rows_together(
    dashboard, settings, fake_brain, caplog
):
    """Every inbox cell has to be one type down the column.

    A queued row has no duration and a finished one does, so a numeric cell
    with a "—" fallback makes that column part float and part string — which
    Arrow refuses, taking the whole table down.
    """
    from invoiceflow import inbox

    done = inbox.save_upload(
        settings.uploads_dir, "invoice_1001.txt", (INVOICES_DIR / "invoice_1001.txt").read_bytes()
    )
    inbox.enqueue(dashboard, inbox.probe_upload(dashboard, done))
    inbox.drain(dashboard, lambda: Pipeline(settings, llm=fake_brain))

    waiting = inbox.save_upload(
        settings.uploads_dir, "invoice_1004.json", (INVOICES_DIR / "invoice_1004.json").read_bytes()
    )
    inbox.enqueue(dashboard, inbox.probe_upload(dashboard, waiting))

    # A fake-brain run finishes in milliseconds, which rounds to 0.0 and takes
    # the same "—" branch as a queued row — so the bug only shows with a
    # realistic duration behind it.
    with dashboard.connect() as conn:
        conn.execute("UPDATE runs SET duration_ms = 12345")

    states = {r["state"] for r in dashboard.inbox_rows()}
    assert states == {"processed", "queued"}  # the mix that breaks it

    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception, [e.value for e in at.exception]

    # Neither at.exception nor the rendered frame can see this: Streamlit
    # catches the Arrow failure and silently coerces the column, so the only
    # evidence is the traceback it logs on the way past.
    assert not [r for r in caplog.records if "Arrow" in r.getMessage()], (
        "a mixed-type column forced Streamlit to repair the table"
    )


# ---------------------------------------------------------------------------
# The Learning tab
# ---------------------------------------------------------------------------


@pytest.fixture
def learning_dashboard(settings, db, fake_brain, monkeypatch) -> RunStore:
    """A dashboard where one demo invoice has been settled by a person, so the
    walkthrough has a completed step and the learned table has a row."""
    from invoiceflow.review import apply_human_review
    from tests.conftest import DEMO_DIR

    _export(monkeypatch, settings)
    pipe = Pipeline(settings, llm=fake_brain)
    first = pipe.run(DEMO_DIR / "invoice_4001.txt")
    assert first.final_status == FinalStatus.NEEDS_REVIEW
    apply_human_review(pipe.store, first, approve=True, note="known rounding", reviewer="demo")
    return pipe.store


def test_learning_tab_renders_on_an_empty_database(empty_dashboard):
    """A fresh install has learned nothing, which must read as a starting point
    rather than as a broken panel."""
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("Nothing learned yet" in i.value for i in at.info)


def test_learning_tab_shows_what_a_person_settled(learning_dashboard):
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception, [e.value for e in at.exception]
    learned = learning_dashboard.learned_precedent()
    assert len(learned) == 1
    assert learned[0]["code"] == "total_mismatch"
    assert learned[0]["approvals"] == 1
    # The walkthrough offers the next invoice in that track, and only that one.
    keys = {b.key for b in at.button}
    assert "demo-INV-4002" in keys
    assert "demo-INV-4003" not in keys  # locked behind the one before it


def test_walkthrough_will_not_run_ahead_of_the_person(settings, db, fake_brain, monkeypatch):
    """The human step is the demo, so an undecided invoice blocks the track —
    otherwise the whole thing collapses into eight items in the review queue."""
    from tests.conftest import DEMO_DIR

    _export(monkeypatch, settings)
    pipe = Pipeline(settings, llm=fake_brain)
    pipe.run(DEMO_DIR / "invoice_4001.txt")  # left needing review, deliberately

    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception, [e.value for e in at.exception]
    assert not any(b.key and b.key.startswith("demo-INV-40") for b in at.button)
    assert any("INV-4001 is waiting on you" in i.value for i in at.info)


def test_a_released_run_says_which_humans_released_it(settings, db, fake_brain, monkeypatch):
    from invoiceflow.review import apply_human_review
    from tests.conftest import DEMO_DIR

    _export(monkeypatch, settings)
    pipe = Pipeline(settings, llm=fake_brain)
    first = pipe.run(DEMO_DIR / "invoice_4001.txt")
    apply_human_review(pipe.store, first, approve=True, reviewer="demo")
    settled = pipe.run(DEMO_DIR / "invoice_4002.csv")
    assert settled.final_status == FinalStatus.PAID

    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception, [e.value for e in at.exception]
    at.button(key=f"toggle-{settled.run_id}").click().run()
    rendered = " ".join(m.value for m in at.markdown)
    assert "1 prior approval(s) settled" in rendered
    assert "review discharged" in rendered
