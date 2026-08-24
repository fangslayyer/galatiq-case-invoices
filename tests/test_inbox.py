"""The upload inbox: saving a file, judging it before it costs anything, and
draining the queue through the real pipeline.

No threads here and no Streamlit. `inbox.drain` is the synchronous half of the
worker — the loop around it is three lines — so every behaviour worth asserting
is reachable without either.
"""

import sqlite3
import subprocess
import sys

import pytest

from invoiceflow import inbox
from invoiceflow.pipeline import MissingApiKeyError, Pipeline
from tests.conftest import INVOICES_DIR


def upload(settings, name: str, data: bytes | None = None):
    """Save a real sample file (or bytes) as though it had been uploaded."""
    if data is None:
        data = (INVOICES_DIR / name).read_bytes()
    return inbox.save_upload(settings.uploads_dir, name, data)


class TestSavingAnUpload:
    def test_the_original_filename_survives(self, settings):
        path = upload(settings, "invoice_1001.txt")
        assert path.name == "invoice_1001.txt"
        assert path.parent.parent == settings.uploads_dir
        # The stem is what run_id is built from, so it has to stay readable.
        assert len(path.parent.name) == 8

    def test_two_uploads_of_one_name_do_not_collide(self, settings):
        a = upload(settings, "invoice_1001.txt")
        b = upload(settings, "invoice_1001.txt")
        assert a != b and a.name == b.name and a.read_bytes() == b.read_bytes()

    @pytest.mark.parametrize(
        "hostile",
        ["../../.ssh/authorized_keys", "..", "", "a/b/c.txt", "in voice;rm -rf.txt"],
    )
    def test_a_hostile_filename_cannot_escape_the_uploads_directory(self, settings, hostile):
        """UploadedFile.name is client-controlled and Streamlit does not
        sanitise it. It becomes a path component *and* part of run_id, which
        export_result_json turns back into results/<run_id>.json."""
        path = upload(settings, hostile, b"x")
        assert settings.uploads_dir.resolve() in path.resolve().parents

    def test_discarding_removes_the_file_and_its_directory(self, settings):
        path = upload(settings, "invoice_1001.txt")
        inbox.discard_upload(path)
        assert not path.exists() and not path.parent.exists()


class TestProbing:
    def test_a_readable_file_reports_its_format_and_hash(self, settings, store):
        probe = inbox.probe_upload(store, upload(settings, "invoice_1004.json"))
        assert probe.readable and probe.file_format == "json"
        assert len(probe.content_sha256) == 64
        assert probe.prior_runs == 0 and not probe.is_rerun

    def test_an_unsupported_extension_is_refused_at_the_door(self, settings, store):
        probe = inbox.probe_upload(store, upload(settings, "contract.docx", b"whatever"))
        assert not probe.readable and "Unsupported invoice format" in probe.error

    def test_a_pdf_with_no_extractable_text_is_refused(self, settings, store):
        probe = inbox.probe_upload(store, upload(settings, "scan.pdf", b"%PDF-1.4 not really"))
        assert not probe.readable  # never becomes a burnt run

    def test_a_document_already_processed_is_flagged_before_it_costs_anything(
        self, settings, store, pipeline
    ):
        pipeline.run(INVOICES_DIR / "invoice_1001.txt")
        probe = inbox.probe_upload(store, upload(settings, "invoice_1001.txt"))
        assert probe.is_rerun and probe.prior_runs == 1

    def test_probing_never_registers_a_document(self, settings, store):
        """A probe is read-only. Registering here would file a documents row
        whose first_seen_path names an upload the user is about to skip."""
        inbox.probe_upload(store, upload(settings, "invoice_1001.txt"))
        with store.connect() as conn:
            assert conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"] == 0


class TestDraining:
    def test_a_drained_queue_records_real_runs(self, settings, store, pipeline):
        for name in ("invoice_1001.txt", "invoice_1003.txt"):
            inbox.enqueue(store, inbox.probe_upload(store, upload(settings, name)))
        assert inbox.drain(store, lambda: pipeline) == 2

        rows = {r["filename"]: r for r in store.inbox_rows()}
        assert {r["state"] for r in rows.values()} == {"processed"}
        assert rows["invoice_1001.txt"]["final_status"] == "paid"
        assert rows["invoice_1003.txt"]["final_status"] == "rejected"
        # The join to v_run_summary carries the telemetry the tab shows.
        assert rows["invoice_1001.txt"]["vendor"]
        assert rows["invoice_1001.txt"]["duration_ms"] is not None

    def test_an_empty_queue_never_builds_a_pipeline(self, store):
        """The 'a dashboard with no API key still works' guarantee: the factory
        is only called once an item has actually been claimed."""

        def explode() -> Pipeline:
            raise AssertionError("the pipeline must not be built for an empty queue")

        assert inbox.drain(store, explode) == 0

    def test_a_pipeline_that_cannot_start_fails_only_its_own_item(self, settings, store):
        for name in ("invoice_1001.txt", "invoice_1004.json"):
            inbox.enqueue(store, inbox.probe_upload(store, upload(settings, name)))

        def no_key() -> Pipeline:
            raise MissingApiKeyError("XAI_API_KEY is not set.")

        assert inbox.process_one(store, no_key) is not None
        rows = sorted(store.inbox_rows(), key=lambda r: r["id"])
        assert rows[0]["state"] == "failed" and "XAI_API_KEY" in rows[0]["error"]
        assert rows[1]["state"] == "queued"  # the next item is untouched

    def test_a_crash_mid_run_leaves_the_item_failed_and_the_run_honest(
        self, settings, store, pipeline
    ):
        inbox.enqueue(store, inbox.probe_upload(store, upload(settings, "invoice_1001.txt")))

        class Exploding(Pipeline):
            def run(self, invoice_path, *, callbacks=None):
                raise RuntimeError("grok went down")

        exploding = Exploding.__new__(Exploding)
        inbox.process_one(store, lambda: exploding)
        row = store.inbox_rows()[0]
        assert row["state"] == "failed" and "grok went down" in row["error"]

    def test_a_run_that_fails_is_still_a_processed_item(self, settings, store, pipeline):
        """`processed` means a run happened and reached a verdict — not that the
        verdict was good. Never getting a run at all is the other thing."""
        path = upload(settings, "invoice_1001.txt")
        probe = inbox.probe_upload(store, path)
        path.unlink()  # the loader will now fail inside the run
        inbox.enqueue(store, probe)
        inbox.drain(store, lambda: pipeline)
        row = store.inbox_rows()[0]
        assert row["state"] == "processed" and row["final_status"] == "failed"


class TestQueueMechanics:
    def test_claiming_is_atomic(self, settings, store):
        inbox.enqueue(store, inbox.probe_upload(store, upload(settings, "invoice_1001.txt")))
        assert store.claim_next_upload() is not None
        assert store.claim_next_upload() is None  # no second claimant gets it

    def test_a_restart_reclaims_an_item_whose_run_never_finished(self, settings, store):
        inbox.enqueue(store, inbox.probe_upload(store, upload(settings, "invoice_1001.txt")))
        store.claim_next_upload()  # the process now "dies"
        assert store.reclaim_stale_uploads() == 1
        row = store.inbox_rows()[0]
        assert row["state"] == "failed" and "dashboard stopped" in row["error"]

    def test_a_restart_adopts_a_run_that_did_finish(self, settings, store, pipeline):
        """The server can die between finish_run committing and finish_upload
        running. That item is processed, not failed."""
        path = upload(settings, "invoice_1001.txt")
        inbox.enqueue(store, inbox.probe_upload(store, path))
        item = store.claim_next_upload()
        assert item is not None
        pipeline.run(item.stored_path)  # the run completes; the item is not closed
        assert store.reclaim_stale_uploads() == 1
        row = store.inbox_rows()[0]
        assert row["state"] == "processed" and row["final_status"] == "paid"

    def test_retry_puts_a_failed_item_back_in_the_queue(self, settings, store):
        item_id = inbox.enqueue(
            store, inbox.probe_upload(store, upload(settings, "invoice_1001.txt"))
        )
        store.claim_next_upload()
        store.finish_upload(item_id, error="boom")
        store.requeue_upload(item_id)
        row = store.inbox_rows()[0]
        assert row["state"] == "queued" and row["error"] == ""

    def test_dismissing_hides_the_row_without_deleting_it(self, settings, store):
        item_id = inbox.enqueue(
            store, inbox.probe_upload(store, upload(settings, "invoice_1001.txt"))
        )
        store.dismiss_upload(item_id)
        assert store.inbox_rows() == [] and store.inbox_counts() == {}
        with store.connect() as conn:
            assert conn.execute("SELECT COUNT(*) AS n FROM inbox_items").fetchone()["n"] == 1

    def test_an_unknown_state_is_refused_by_the_database(self, settings, store):
        item_id = inbox.enqueue(
            store, inbox.probe_upload(store, upload(settings, "invoice_1001.txt"))
        )
        with pytest.raises(sqlite3.IntegrityError), store.connect() as conn:
            conn.execute("UPDATE inbox_items SET state = 'weird' WHERE id = ?", (item_id,))


class TestTheModuleItself:
    def test_the_inbox_never_imports_streamlit(self):
        """The worker runs on a thread with no ScriptRunContext, where every
        Streamlit call is a silently logged no-op. Keeping the import out of
        this module is how that stays true rather than merely remembered.

        Checked in a subprocess because pytest has already imported Streamlit
        for the dashboard tests, so this process cannot answer the question.
        """
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import invoiceflow.inbox, sys; print('streamlit' in sys.modules)",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout.strip() == "False"

    def test_one_worker_per_store(self, settings):
        assert inbox.worker_for(settings) is inbox.worker_for(settings)
        inbox.worker_for(settings).stop()
