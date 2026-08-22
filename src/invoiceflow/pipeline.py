"""Top-level pipeline runner: wraps the graph with run IDs, timing, and
persisted results — the observability layer the CLI and dashboard read.

It also owns the LLM factory, since this is the only place one is built: xAI
Grok is the single reasoning engine. "Offline" in the case brief means no
external non-Grok APIs (payment and inventory are mocked locally), not a
hand-rolled backup brain — tests inject a fake chat model through `llm`.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

from langchain_core.language_models import BaseChatModel
from pydantic import SecretStr

from .config import Settings, langsmith_project
from .db import Database
from .graph import build_graph
from .models import FinalStatus, InvoiceRunResult
from .state import PipelineState

log = logging.getLogger(__name__)


class MissingApiKeyError(RuntimeError):
    pass


class Pipeline:
    def __init__(self, settings: Settings | None = None, llm: BaseChatModel | None = None):
        """`llm` lets tests inject a fake brain; production always uses Grok."""
        self.settings = settings or Settings()
        self.db = Database(self.settings.db_path)

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

        self.graph = build_graph(self.settings, self.db, self.llm)
        project = langsmith_project()
        if project:
            log.info("LangSmith tracing enabled — runs stream to project %r", project)

    @property
    def backend(self) -> str:
        return getattr(self.llm, "model_name", None) or type(self.llm).__name__

    def run(self, invoice_path: Path | str, *, persist: bool = True) -> InvoiceRunResult:
        invoice_path = Path(invoice_path)
        run_id = f"{invoice_path.stem}-{uuid.uuid4().hex[:8]}"
        started = datetime.now(UTC).isoformat(timespec="seconds")
        log.info("run %s: processing %s (backend=%s)", run_id, invoice_path, self.backend)

        # Annotated, not a bare literal: this is the other half of the contract in
        # state.py, so a missing or misspelled Required key is a type error here
        # rather than a reducer that silently never fires.
        initial: PipelineState = {
            "source_file_path": str(invoice_path),
            "run_id": run_id,
            "started_at": started,
            "trace": [],
            "critique_rounds": [],
        }
        state = self.graph.invoke(
            initial,
            # Names and labels the trace tree when LangSmith is on; inert otherwise.
            config={
                "run_name": f"invoice {invoice_path.name}",
                "tags": ["invoiceflow", self.backend],
                "metadata": {
                    "run_id": run_id,
                    "source_file_path": str(invoice_path),
                    "llm_backend": self.backend,
                },
            },
        )
        result = InvoiceRunResult(
            run_id=run_id,
            source_file_path=str(invoice_path),
            started_at=started,
            finished_at=datetime.now(UTC).isoformat(timespec="seconds"),
            llm_backend=self.backend,
            final_status=state.get("final_status", FinalStatus.FAILED),
            invoice=state.get("invoice"),
            validation=state.get("report"),
            decision=state.get("decision"),
            critique_rounds=state.get("critique_rounds", []),
            payment=state.get("payment"),
            error=state.get("error", ""),
            trace=state.get("trace", []),
        )
        if persist:
            self._persist(result)
        return result

    def run_many(self, paths: list[Path], *, persist: bool = True) -> list[InvoiceRunResult]:
        return [self.run(p, persist=persist) for p in paths]

    def _persist(self, result: InvoiceRunResult) -> Path:
        self.settings.results_dir.mkdir(parents=True, exist_ok=True)
        out = self.settings.results_dir / f"{result.run_id}.json"
        out.write_text(result.model_dump_json(indent=2))
        log.info("run %s: result written to %s", result.run_id, out)
        return out


def load_results(results_dir: Path) -> list[InvoiceRunResult]:
    """Read every persisted run, newest first (used by the dashboard)."""
    if not results_dir.exists():
        return []
    results = []
    for path in sorted(results_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            results.append(InvoiceRunResult.model_validate_json(path.read_text()))
        except ValueError:
            log.warning("Skipping unreadable result file %s", path)
    return results
