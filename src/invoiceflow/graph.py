"""The LangGraph StateGraph that orchestrates the agents.

    ingest -> validate -> approve <-> critique -> pay -> record
                 |                        |
                 +--(exact duplicate)-----+--(reject / review)--> record

Agents never call each other directly: conditional edges route on each
agent's structured output, which keeps the flow inspectable and testable.
"""

from __future__ import annotations

import logging
from pathlib import Path

from langchain_core.language_models import BaseChatModel
from langgraph.graph import END, StateGraph

from .agents import ExtractionError, run_approver, run_critic, run_extractor, run_validator
from .config import PROJECT_ROOT, Settings
from .db import Database
from .loaders import load_invoice_text
from .models import (
    ApprovalStatus,
    CritiqueRound,
    CritiqueVerdict,
    FinalStatus,
    IssueCode,
    Severity,
    TraceEvent,
)
from .payment import execute_payment
from .rules import evaluate_rules
from .state import PipelineState
from .validation import ValidationContext

log = logging.getLogger(__name__)

DOCS_DIR = PROJECT_ROOT / "docs"


def _ev(stage: str, event: str, detail: str = "") -> TraceEvent:
    log.info("[%s] %s%s", stage, event, f" — {detail}" if detail else "")
    return TraceEvent(stage=stage, event=event, detail=detail)


def build_graph(settings: Settings, db: Database, llm: BaseChatModel):
    def ingest(state: PipelineState) -> dict:
        trace = [_ev("ingestion", "loading", state["source_file"])]
        try:
            raw_text = load_invoice_text(state["source_file"])
        except (OSError, ValueError) as exc:
            trace.append(_ev("ingestion", "load_failed", str(exc)))
            return {"error": str(exc), "final_status": FinalStatus.FAILED, "trace": trace}
        catalog = [rec.item for rec in db.all_items()]
        try:
            invoice, retries = run_extractor(
                llm, raw_text, catalog, max_retries=settings.max_extraction_retries
            )
        except ExtractionError as exc:
            trace.append(_ev("ingestion", "extraction_failed", str(exc)))
            return {
                "raw_text": raw_text,
                "error": str(exc),
                "final_status": FinalStatus.FAILED,
                "trace": trace,
            }
        trace.append(
            _ev(
                "ingestion",
                "extracted",
                f"{invoice.invoice_number} from {invoice.vendor or '<no vendor>'}, "
                f"{len(invoice.line_items)} line item(s), total {invoice.total}"
                + (
                    f" ({retries} self-correction retr{'y' if retries == 1 else 'ies'})"
                    if retries
                    else ""
                ),
            )
        )
        return {
            "raw_text": raw_text,
            "invoice": invoice,
            "extraction_retries": retries,
            "trace": trace,
        }

    def validate(state: PipelineState) -> dict:
        ctx = ValidationContext(
            invoice=state["invoice"], db=db, expected_currency=settings.expected_currency
        )
        report = run_validator(llm, ctx)
        trace = [
            _ev(
                "validation",
                "report",
                f"{len(report.issues)} issue(s) via {', '.join(report.tools_used)}",
            )
        ]
        for issue in report.issues:
            trace.append(
                _ev("validation", f"issue:{issue.severity}", f"{issue.code}: {issue.detail}")
            )
        return {"report": report, "trace": trace}

    def approve(state: PipelineState) -> dict:
        constraints = state.get("constraints") or evaluate_rules(
            state["invoice"], state["report"], settings.scrutiny_threshold
        )
        rounds = state.get("critique_rounds") or []
        feedback = rounds[-1].critique.feedback if rounds else None
        decision = run_approver(llm, state["invoice"], state["report"], constraints, feedback)
        trace = [
            _ev(
                "approval",
                f"proposed:{decision.status}",
                decision.reasoning + (" [revision after critique]" if feedback else ""),
            )
        ]
        return {"constraints": constraints, "decision": decision, "trace": trace}

    def critique(state: PipelineState) -> dict:
        decision = state["decision"]
        constraints = state["constraints"]
        crit = run_critic(llm, state["invoice"], state["report"], constraints, decision)
        trace = [_ev("approval", f"critique:{crit.verdict}", crit.feedback)]
        rounds_so_far = len(state.get("critique_rounds") or [])
        exhausted = (
            crit.verdict == CritiqueVerdict.REVISE
            and rounds_so_far + 1 > settings.max_critique_rounds
        )

        updates: dict = {"critique_rounds": [CritiqueRound(decision=decision, critique=crit)]}
        if crit.verdict == CritiqueVerdict.ESCALATE or exhausted:
            reason = (
                "Approver and Critic could not converge; escalating to a human."
                if exhausted
                else crit.feedback
            )
            updates["decision"] = decision.model_copy(
                update={
                    "status": ApprovalStatus.NEEDS_REVIEW,
                    "reasoning": f"{decision.reasoning}\n\nEscalated by Critic: {reason}",
                }
            )
            trace.append(_ev("approval", "escalated", reason))
        elif crit.verdict == CritiqueVerdict.ACCEPT and (
            decision.status == ApprovalStatus.APPROVED and constraints.must_reject
        ):
            # Defense in depth: hard rules outrank both agents.
            updates["decision"] = decision.model_copy(
                update={
                    "status": ApprovalStatus.REJECTED,
                    "reasoning": "Hard business rule override: critical validation failures "
                    "forbid approval. " + "; ".join(constraints.reject_reasons),
                }
            )
            trace.append(_ev("approval", "hard_rule_override", "approved -> rejected"))
        updates["trace"] = trace
        return updates

    def pay(state: PipelineState) -> dict:
        result = execute_payment(db, state["invoice"], state["run_id"])
        return {
            "payment": result,
            "trace": [
                _ev(
                    "payment",
                    result.status,
                    f"${result.amount:,.2f} to {result.vendor}",
                )
            ],
        }

    def record(state: PipelineState) -> dict:
        final = _final_status(state)
        trace = [_ev("record", f"final:{final}")]
        invoice = state.get("invoice")
        if invoice is not None and final not in (FinalStatus.FAILED, FinalStatus.DUPLICATE):
            prior = db.get_processed(invoice.invoice_number)
            if prior is not None and prior.final_status == "paid" and final != FinalStatus.PAID:
                trace.append(
                    _ev("record", "registry_kept", f"{invoice.invoice_number} stays 'paid'")
                )
            else:
                db.record_processed(
                    invoice.invoice_number,
                    invoice.content_hash(),
                    invoice.vendor,
                    invoice.total,
                    final.value,
                    state["run_id"],
                )
                trace.append(
                    _ev("record", "registry_updated", f"{invoice.invoice_number} -> {final}")
                )
        return {"final_status": final, "trace": trace}

    # -- routing ------------------------------------------------------------

    def after_ingest(state: PipelineState) -> str:
        return "record" if state.get("error") else "validate"

    def after_validate(state: PipelineState) -> str:
        report = state["report"]
        is_exact_duplicate = any(
            i.code == IssueCode.DUPLICATE_INVOICE and i.severity == Severity.CRITICAL
            for i in report.issues
        )
        return "record" if is_exact_duplicate else "approve"

    def after_critique(state: PipelineState) -> str:
        rounds = state["critique_rounds"]
        verdict = rounds[-1].critique.verdict
        if verdict == CritiqueVerdict.REVISE and len(rounds) <= settings.max_critique_rounds:
            return "approve"
        if state["decision"].status == ApprovalStatus.APPROVED:
            return "pay"
        return "record"

    graph = StateGraph(PipelineState)
    graph.add_node("ingest", ingest)
    graph.add_node("validate", validate)
    graph.add_node("approve", approve)
    graph.add_node("critique", critique)
    graph.add_node("pay", pay)
    graph.add_node("record", record)
    graph.set_entry_point("ingest")
    graph.add_conditional_edges("ingest", after_ingest, ["validate", "record"])
    graph.add_conditional_edges("validate", after_validate, ["approve", "record"])
    graph.add_edge("approve", "critique")
    graph.add_conditional_edges("critique", after_critique, ["approve", "pay", "record"])
    graph.add_edge("pay", "record")
    graph.add_edge("record", END)
    compiled = graph.compile()
    export_graph_image(compiled)
    return compiled


def export_graph_image(compiled, out_dir: Path = DOCS_DIR) -> None:
    """Render the compiled graph to docs/graph.png (mermaid source alongside it).

    The PNG is rendered by mermaid.ink, so it is only re-fetched when the graph
    topology actually changed, and a failure (offline, service down) is logged
    rather than raised: a missing diagram must never break a pipeline run.
    """
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        drawable = compiled.get_graph()
        mermaid = drawable.draw_mermaid()
        mmd_path, png_path = out_dir / "graph.mmd", out_dir / "graph.png"
        if png_path.exists() and mmd_path.exists() and mmd_path.read_text() == mermaid:
            return
        mmd_path.write_text(mermaid)
        png_path.write_bytes(drawable.draw_mermaid_png())
        log.info("graph diagram written to %s", png_path)
    except Exception as exc:  # diagram export is best-effort, never fatal
        log.warning("graph diagram export skipped: %s", exc)


def _final_status(state: PipelineState) -> FinalStatus:
    if state.get("error"):
        return FinalStatus.FAILED
    report = state.get("report")
    if report is not None and any(
        i.code == IssueCode.DUPLICATE_INVOICE and i.severity == Severity.CRITICAL
        for i in report.issues
    ):
        return FinalStatus.DUPLICATE
    payment = state.get("payment")
    if payment is not None:
        return FinalStatus.PAID if payment.status == "success" else FinalStatus.DUPLICATE
    decision = state.get("decision")
    if decision is None:
        return FinalStatus.FAILED
    if decision.status == ApprovalStatus.REJECTED:
        return FinalStatus.REJECTED
    return FinalStatus.NEEDS_REVIEW
