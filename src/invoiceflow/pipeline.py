"""Top-level pipeline runner: wraps the graph with run IDs, timing, and
persisted results — the observability layer the CLI and dashboard read."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

from langchain_core.language_models import BaseChatModel

from .config import Settings
from .db import Database
from .graph import build_graph
from .llm import build_llm
from .models import FinalStatus, InvoiceRunResult

log = logging.getLogger(__name__)


class Pipeline:
    def __init__(self, settings: Settings | None = None, llm: BaseChatModel | None = None):
        """`llm` lets tests inject a fake brain; production always uses Grok."""
        self.settings = settings or Settings()
        self.db = Database(self.settings.db_path)
        self.llm = llm if llm is not None else build_llm(self.settings)
        self.graph = build_graph(self.settings, self.db, self.llm)

    @property
    def backend(self) -> str:
        return getattr(self.llm, "model_name", None) or type(self.llm).__name__

    def run(self, invoice_path: Path | str, *, persist: bool = True) -> InvoiceRunResult:
        invoice_path = Path(invoice_path)
        run_id = f"{invoice_path.stem}-{uuid.uuid4().hex[:8]}"
        started = datetime.now(UTC).isoformat(timespec="seconds")
        log.info("run %s: processing %s (backend=%s)", run_id, invoice_path, self.backend)

        state = self.graph.invoke(
            {
                "source_file": str(invoice_path),
                "run_id": run_id,
                "started_at": started,
                "trace": [],
                "critique_rounds": [],
            }
        )
        result = InvoiceRunResult(
            run_id=run_id,
            source_file=str(invoice_path),
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
