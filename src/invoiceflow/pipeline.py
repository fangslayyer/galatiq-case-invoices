"""Top-level pipeline runner: wraps the graph with run IDs, timing, and the
run store — the observability layer the CLI and dashboard read.

It also owns the LLM factory, since this is the only place one is built: xAI
Grok is the single reasoning engine. "Offline" in the case brief means no
external non-Grok APIs (payment and inventory are mocked locally), not a
hand-rolled backup brain — tests inject a fake chat model through `llm`.

Persistence contract (docs/schema.md):
  * `begin_run` opens the audit row before anything happens — a crash leaves
    an honest `failed` row instead of nothing.
  * The graph mutates only the payment registry mid-run (idempotency must be
    visible to the very next run).
  * `finish_run` writes everything else in one transaction at the end.
"""

from __future__ import annotations

import logging
import subprocess
import time
import uuid
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from pydantic import SecretStr

from .config import PROJECT_ROOT, Settings, langsmith_project
from .db import Database
from .graph import build_graph
from .models import FinalStatus, InvoiceRunResult
from .recording import RunRecorder, TelemetryHandler
from .runstore import RunStore
from .state import PipelineState

log = logging.getLogger(__name__)


class MissingApiKeyError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def pipeline_revision() -> str:
    """`git describe --dirty` for runs.pipeline_revision, or '' outside git."""
    try:
        return subprocess.run(
            ["git", "describe", "--always", "--dirty"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
    except Exception:  # not a repo, no git, timeout — never a run blocker
        return ""


class Pipeline:
    def __init__(self, settings: Settings | None = None, llm: BaseChatModel | None = None):
        """`llm` lets tests inject a fake brain; production always uses Grok."""
        self.settings = settings or Settings()
        self.db = Database(self.settings.db_path)
        self.store = RunStore(self.settings.runs_db_path)

        if llm is not None:
            self.llm = llm
        else:
            # Only the real Grok path needs a key: an injected brain (tests,
            # notebooks) must keep working with no credentials at all.
            api_key = self.settings.resolve_api_key()
            if not api_key:
                raise MissingApiKeyError(
                    "XAI_API_KEY is not set. Export it or put it in .env — the pipeline's "
                    "reasoning engine is xAI Grok and there is no non-LLM fallback."
                )
            from langchain_xai import ChatXAI  # deferred: importing langchain is slow

            self.llm = ChatXAI(
                model=self.settings.grok_model, api_key=SecretStr(api_key), temperature=0
            )

        self.graph = build_graph(self.settings, self.db, self.store, self.llm)
        project = langsmith_project()
        if project:
            log.info("LangSmith tracing enabled — runs stream to project %r", project)

    @property
    def backend(self) -> str:
        return getattr(self.llm, "model_name", None) or type(self.llm).__name__

    def run(
        self,
        invoice_path: Path | str,
        *,
        callbacks: list[BaseCallbackHandler] | None = None,
    ) -> InvoiceRunResult:
        """Process one invoice. `callbacks` rides alongside the telemetry
        handler — how the dashboard watches a run advance node by node."""
        invoice_path = Path(invoice_path)
        run_id = f"{invoice_path.stem}-{uuid.uuid4().hex[:8]}"
        started = datetime.now(UTC).isoformat(timespec="seconds")
        t0 = time.monotonic()
        log.info("run %s: processing %s (backend=%s)", run_id, invoice_path, self.backend)

        run_pk = self.store.begin_run(
            run_id, str(invoice_path), started, self.backend, pipeline_revision()
        )
        recorder = RunRecorder()

        # Annotated, not a bare literal: this is the other half of the contract in
        # state.py, so a missing or misspelled Required key is a type error here
        # rather than a reducer that silently never fires.
        initial: PipelineState = {
            "source_file_path": str(invoice_path),
            "run_id": run_id,
            "started_at": started,
            "trace": [],
            "critique_rounds": [],
            "recorder": recorder,
            "run_pk": run_pk,
        }
        state = self.graph.invoke(
            initial,
            # The telemetry handler propagates into every agent's model call
            # via LangChain's contextvars, alongside (never instead of) the
            # LangSmith tracer. Names and labels the trace tree when tracing
            # is on; inert otherwise.
            config={
                "callbacks": [TelemetryHandler(recorder, self.backend), *(callbacks or [])],
                "run_name": f"invoice {invoice_path.name}",
                "tags": ["invoiceflow", self.backend],
                "metadata": {
                    "run_id": run_id,
                    "source_file_path": str(invoice_path),
                    "llm_backend": self.backend,
                },
            },
        )
        finished = datetime.now(UTC).isoformat(timespec="seconds")
        final_status = state.get("final_status", FinalStatus.FAILED)

        self.store.finish_run(
            run_pk,
            document_id=state.get("document_id"),
            finished_at=finished,
            duration_ms=int((time.monotonic() - t0) * 1000),
            final_status=final_status,
            decision=state.get("decision"),
            quarantine_reason=state.get("quarantine_reason", ""),
            error=state.get("error", ""),
            recorder=recorder,
            invoice=state.get("invoice"),
            extraction_attempts=state.get("extraction_attempts", []),
            report=state.get("report"),
            constraints=state.get("constraints"),
            scrutiny_threshold=self.settings.scrutiny_threshold,
            critique_rounds=state.get("critique_rounds", []),
            overrides=state.get("overrides", []),
            payment=state.get("payment"),
            trace=state.get("trace", []),
        )
        log.info("run %s: recorded in %s", run_id, self.store.path)

        return InvoiceRunResult(
            run_id=run_id,
            source_file_path=str(invoice_path),
            started_at=started,
            finished_at=finished,
            llm_backend=self.backend,
            final_status=final_status,
            invoice=state.get("invoice"),
            validation=state.get("report"),
            decision=state.get("decision"),
            critique_rounds=state.get("critique_rounds", []),
            payment=state.get("payment"),
            error=state.get("error", ""),
            trace=state.get("trace", []),
            document_run_no=state.get("document_run_no", 1),
            overrides=state.get("overrides", []),
        )

    def run_many(self, paths: list[Path]) -> list[InvoiceRunResult]:
        return [self.run(p) for p in paths]


def export_result_json(store: RunStore, run_id: str, out_dir: Path) -> Path:
    """Render one run from the store to `<out_dir>/<run_id>.json`.

    The file is derived, never a second writer: it cannot drift from the
    database it was rendered from (docs/schema.md, rollout phase 6).
    """
    result = store.load_result(run_id)
    if result is None:
        raise KeyError(f"no run named {run_id!r} in {store.path}")
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{run_id}.json"
    out.write_text(result.model_dump_json(indent=2))
    return out
