"""The upload inbox: files a person dropped into the dashboard, and the one
background worker that turns them into runs.

Deliberately Streamlit-free — nothing here imports `st`. The worker runs on a
plain thread with no ScriptRunContext, where every Streamlit call is a silently
logged no-op; keeping the import out of this module is how that stays true
rather than merely remembered. The dashboard owns the widgets, this module owns
the queue, the disk, and the pipeline.

Serial by construction, and not for the reason you would guess. Each run builds
its own `RunRecorder`, so concurrency would not confuse the telemetry. What it
would break is payment idempotency: `execute_payment` reads the registry
(payment.py) and the registry is written later, from the `record` node
(graph.py) — two transactions, not one. Two runs of the same invoice number in
flight together could both conclude that nothing has been paid yet. One at a
time is the cheapest way to keep that guarantee, and SQLite's single writer
means concurrency would have bought very little anyway.

Sharing the store across threads is free, and worth naming: `RunStore` holds a
path, never a connection, and every method opens and drops its own. sqlite3's
`check_same_thread` therefore never fires.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

from .config import Settings
from .loaders import load_invoice_text
from .pipeline import Pipeline
from .runstore import InboxItem, RunStore

log = logging.getLogger(__name__)

POLL_SECONDS = 2.0

#: Anything outside this is replaced. Conservative on purpose — see safe_filename.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def safe_filename(name: str) -> str:
    """Reduce a browser-supplied filename to something safe to put on disk.

    `UploadedFile.name` is whatever the client sent and Streamlit does not
    sanitise it. It becomes a path component AND, through `Pipeline.run`, part
    of `run_id` — which `export_result_json` turns straight back into a
    filename (`results/<run_id>.json`). A name containing `../` would traverse
    out of both. Basename first, then an allowlist.
    """
    stem = Path(name.replace("\\", "/")).name
    cleaned = _UNSAFE.sub("_", stem).lstrip(".")
    return cleaned or "upload"


def save_upload(uploads_dir: Path, name: str, data: bytes) -> Path:
    """Write one upload to `<uploads_dir>/<uuid4 hex[:8]>/<safe name>`.

    A directory per upload rather than a mangled filename: `run_id` is built
    from the path stem, so `invoice_1001-3f2a0b1c` reads far better than
    `invoice_1001_2_a94f-3f2a0b1c`, and the vendor's own name survives two
    people uploading the same one.
    """
    target = uploads_dir / uuid.uuid4().hex[:8]
    target.mkdir(parents=True, exist_ok=True)
    path = target / safe_filename(name)
    path.write_bytes(data)
    return path


@dataclass(frozen=True)
class UploadProbe:
    """What we can tell about a saved file before spending anything on it."""

    filename: str
    stored_path: Path
    file_format: str
    byte_size: int
    content_sha256: str  # "" when the file could not be read
    prior_runs: int
    error: str  # the loader's own words, verbatim

    @property
    def readable(self) -> bool:
        return not self.error

    @property
    def is_rerun(self) -> bool:
        return self.prior_runs > 0


def probe_upload(store: RunStore, path: Path) -> UploadProbe:
    """Read a saved file the way the pipeline will, and say what we know.

    Doubles as the door check: `load_invoice_text` is the same call the ingest
    node makes, so a file that cannot be read is refused here rather than a
    minute later as a `failed` run nobody asked for. The hash it yields is also
    what tells the user these exact bytes have been through the pipeline
    before, applied where it costs nothing.
    """
    size = path.stat().st_size
    fmt = path.suffix.lstrip(".").lower()
    try:
        raw_text = load_invoice_text(path)
    except Exception as exc:
        # Broad deliberately, and broader than the ingest node's own
        # (OSError, ValueError): this is the door check for arbitrary bytes a
        # browser handed us, and a corrupt PDF surfaces as pdfplumber's
        # PdfminerException, which inherits straight from Exception. "Is this
        # readable?" must always be answerable, never raisable.
        return UploadProbe(path.name, path, fmt, size, "", 0, f"{type(exc).__name__}: {exc}")
    _, prior_runs = store.document_history(raw_text)
    return UploadProbe(
        filename=path.name,
        stored_path=path,
        file_format=fmt,
        byte_size=size,
        content_sha256=hashlib.sha256(raw_text.encode()).hexdigest(),
        prior_runs=prior_runs,
        error="",
    )


def discard_upload(path: Path) -> None:
    """Remove a saved upload and the directory we made for it."""
    path.unlink(missing_ok=True)
    parent = path.parent
    if parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()


def enqueue(store: RunStore, probe: UploadProbe, *, source: str = "upload") -> int:
    return store.enqueue_upload(
        filename=probe.filename,
        stored_path=str(probe.stored_path),
        file_format=probe.file_format,
        byte_size=probe.byte_size,
        content_sha256=probe.content_sha256,
        prior_runs=probe.prior_runs,
        source=source,
    )


class StageReporter(BaseCallbackHandler):
    """Writes the graph's current node onto the inbox row, for the live UI.

    A callback rather than a hop through `PipelineState`: node names come from
    LangGraph's own metadata, so this cannot drift from the topology the way a
    hand-written string inside each node would, and neither the typed state
    contract nor graph.py is touched for something that is purely
    observability. That is the seam `recording.py` already established.
    """

    def __init__(self, store: RunStore, item_id: int) -> None:
        self._store = store
        self._item_id = item_id

    def on_chain_start(
        self,
        serialized: dict[str, Any] | None,
        inputs: dict[str, Any],
        *,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        node = (metadata or {}).get("langgraph_node")
        if node:
            # Never allowed to break a run: this is decoration on a live view.
            try:
                self._store.set_upload_stage(self._item_id, str(node))
            except Exception:  # a locked DB must not fail an invoice
                log.debug("could not record stage %s for item %s", node, self._item_id)


def process_one(store: RunStore, get_pipeline: Callable[[], Pipeline]) -> InboxItem | None:
    """Claim one item and run it. None when the queue is empty.

    `get_pipeline` is a factory and it is called only *after* an item has been
    claimed. That ordering is the whole point: an app with no XAI_API_KEY still
    renders, still accepts uploads, and only reports the missing key against
    the file that actually needed it.
    """
    item = store.claim_next_upload()
    if item is None:
        return None
    try:
        result = get_pipeline().run(item.stored_path, callbacks=[StageReporter(store, item.id)])
    except Exception as exc:  # broad on purpose, see below
        # Broad on purpose. Everything from MissingApiKeyError to an
        # uninitialised inventory.db has to end as a visible, explained inbox
        # row rather than as a dead worker thread. The run row itself is
        # already honest: begin_run wrote `failed` up front.
        log.exception("inbox item %s (%s) failed", item.id, item.filename)
        store.finish_upload(item.id, error=f"{type(exc).__name__}: {exc}")
        return item
    store.finish_upload(item.id, run_id=result.run_id)
    return item


def drain(store: RunStore, get_pipeline: Callable[[], Pipeline]) -> int:
    """Process everything queued right now, returning how many.

    The synchronous half of the worker — what the tests drive, with a FakeBrain
    pipeline and no thread in sight.
    """
    done = 0
    while process_one(store, get_pipeline) is not None:
        done += 1
    return done


class InboxWorker:
    """One thread, draining the queue serially for as long as the app lives."""

    def __init__(
        self,
        settings: Settings,
        *,
        pipeline_factory: Callable[[], Pipeline] | None = None,
    ) -> None:
        self._settings = settings
        self._store = RunStore(settings.runs_db_path)
        self._factory = pipeline_factory
        self._pipeline: Pipeline | None = None
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _get_pipeline(self) -> Pipeline:
        """Built on first use, never at import: a dashboard with no API key has
        to render, accept uploads and explain itself."""
        if self._pipeline is None:
            self._pipeline = self._factory() if self._factory else Pipeline(self._settings)
        return self._pipeline

    def start(self) -> None:
        """Idempotent. Reclaims anything a previous process left mid-flight."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            reclaimed = self._store.reclaim_stale_uploads()
            if reclaimed:
                log.info("reclaimed %d inbox item(s) stranded by a previous process", reclaimed)
            self._thread = threading.Thread(
                target=self._loop, name="invoiceflow-inbox", daemon=True
            )
            self._thread.start()

    def wake(self) -> None:
        """Skip the poll interval — something was just queued."""
        self._wake.set()

    def stop(self, timeout: float = 5.0) -> None:
        self._stopping.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                drain(self._store, self._get_pipeline)
            except Exception:  # the thread must outlive any one failure
                log.exception("inbox worker loop error")
            self._wake.wait(POLL_SECONDS)
            self._wake.clear()


_WORKERS: dict[Path, InboxWorker] = {}
_WORKERS_LOCK = threading.Lock()


def worker_for(settings: Settings) -> InboxWorker:
    """The one worker for this run store in this process.

    Not left to `@st.cache_resource` alone: that cache is clearable from the
    app's own ⋮ menu, and clearing it drops the *reference* to a thread that
    keeps running — the next rerun would start a second worker against the same
    queue. Keyed on the database so a test store gets its own.
    """
    key = Path(settings.runs_db_path).resolve()
    with _WORKERS_LOCK:
        worker = _WORKERS.get(key)
        if worker is None:
            worker = _WORKERS[key] = InboxWorker(settings)
            worker.start()
        return worker
