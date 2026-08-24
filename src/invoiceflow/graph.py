"""The LangGraph StateGraph that orchestrates the agents.

    ingest -> validate -> decide <-> critique -> pay -> record
                 |                       |
                 +--(exact duplicate)----+--(reject / review)--> record

Agents never call each other directly: conditional edges route on each
agent's structured output, which keeps the flow inspectable and testable.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
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
    OverrideRecord,
    PaymentStatus,
    PrecedentBundle,
    TraceEvent,
    ValidationReport,
)
from .payment import execute_payment
from .precedent import build_precedent_tool, lookup_precedents, precedent_block
from .recording import RunRecorder
from .rules import evaluate_rules
from .runstore import RunStore
from .state import PipelineState
from .structuring import lookup_vendor_window
from .validation import ValidationContext, forged_fence_issue

log = logging.getLogger(__name__)

DOCS_DIR = PROJECT_ROOT / "docs"


def _ev(stage: str, event: str, detail: str = "") -> TraceEvent:
    log.info("[%s] %s%s", stage, event, f" — {detail}" if detail else "")
    return TraceEvent(
        stage=stage,
        event=event,
        detail=detail,
        at=datetime.now(UTC).isoformat(timespec="seconds"),
    )


def _precedent_events(bundle: PrecedentBundle) -> list[TraceEvent]:
    """One trace line per open question history was asked about.

    A refusal to release is traced as loudly as a release: "history fell short by
    0.6" is the line that explains an escalation nobody could otherwise account
    for, and over many runs it is the measure of whether this is worth its weight.
    """
    events = []
    for found in bundle.findings:
        event = "precedent:released" if found.released else "precedent:insufficient"
        events.append(_ev("approval", event, found.summary_line()))
    return events


def _recorder(state: PipelineState) -> RunRecorder:
    """The run's recorder, or a throwaway one when the graph is driven bare
    (direct `graph.invoke` in tests): the nodes never need to care."""
    return state.get("recorder") or RunRecorder()


def _prompt_safety_gate(raw_text: str) -> ValidationReport | None:
    """Quarantine verdict for a freshly loaded document, or None if it is clean.

    Runs before the Extractor, because the Extractor is itself an LLM: a
    document that forges prompt fences must not reach *any* model, only a
    human. The returned report is what the run carries in place of a real
    validation pass, so the finding surfaces in the persisted result.
    """
    issue = forged_fence_issue(raw_text)
    if issue is None:
        return None
    return ValidationReport(
        issues=[issue],
        summary="Quarantined at ingestion: this document forged the pipeline's own prompt "
        "fences, so it was never shown to a language model. Needs a human reader.",
        tools_used=["prompt_safety_gate"],
    )


def build_graph(settings: Settings, db: Database, store: RunStore, llm: BaseChatModel):
    def ingest(state: PipelineState) -> dict:
        trace = [_ev("ingestion", "loading", state["source_file_path"])]
        try:
            raw_text = load_invoice_text(state["source_file_path"])
        except (OSError, ValueError) as exc:
            trace.append(_ev("ingestion", "load_failed", str(exc)))
            return {"error": str(exc), "final_status": FinalStatus.FAILED, "trace": trace}
        # Register the document before anything else touches it: identity is
        # the content hash, so a re-run (from any path) is visible immediately,
        # and a quarantined document keeps its evidence in the store.
        document_id, prior_runs = store.register_document(raw_text, state["source_file_path"])
        doc_updates = {"document_id": document_id, "document_run_no": prior_runs + 1}
        if prior_runs:
            trace.append(
                _ev(
                    "ingestion",
                    "reprocessed_document",
                    f"this document has been processed {prior_runs} time(s) before "
                    f"(run #{prior_runs + 1} for identical content)",
                )
            )
        quarantine = _prompt_safety_gate(raw_text)
        if quarantine is not None:
            reason = quarantine.issues[0].detail
            trace.append(_ev("ingestion", "quarantined", reason))
            return {
                "raw_text": raw_text,
                "quarantine_reason": reason,
                "report": quarantine,
                "trace": trace,
                **doc_updates,
            }
        catalog = [rec.item for rec in db.all_items()]
        rec = _recorder(state)
        try:
            with rec.turn("ingest", "extractor") as turn:
                invoice, attempts = run_extractor(
                    llm, raw_text, catalog, max_retries=settings.max_extraction_retries
                )
                if attempts:
                    turn.outcome = "retried"
        except ExtractionError as exc:
            trace.append(_ev("ingestion", "extraction_failed", str(exc)))
            return {
                "raw_text": raw_text,
                "extraction_attempts": exc.attempts,
                "error": str(exc),
                "final_status": FinalStatus.FAILED,
                "trace": trace,
                **doc_updates,
            }
        retries = len(attempts)
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
            "extraction_attempts": attempts,
            "trace": trace,
            **doc_updates,
        }

    def validate(state: PipelineState) -> dict:
        ctx = ValidationContext(
            invoice=state["invoice"],
            db=db,
            store=store,
            expected_currency=settings.expected_currency,
            raw_text=state.get("raw_text", ""),
        )
        with _recorder(state).turn("validate", "validator"):
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

    def decide(state: PipelineState) -> dict:
        trace: list[TraceEvent] = []
        # Resolved once and cached in state: a redraft round must be judged
        # against the same history as the first draft, and re-querying between
        # rounds would let a concurrent review change the answer mid-decision.
        precedents = state.get("precedents")
        if precedents is None:
            precedents = lookup_precedents(store, state["invoice"], state["report"], settings)
            trace.extend(_precedent_events(precedents))
        constraints = state.get("constraints")
        if constraints is None:
            # Same caching argument as precedent above, and the same hazard: a
            # redraft has to be judged against the window the first draft was.
            window = lookup_vendor_window(store, state["invoice"], settings)
            if window.invoices:
                trace.append(
                    _ev(
                        "approval",
                        "vendor_window",
                        f"{len(window.invoices)} other invoice(s) from "
                        f"{state['invoice'].vendor} dated within {window.days} days, "
                        f"totalling ${window.total:,.2f}",
                    )
                )
            constraints = evaluate_rules(
                state["invoice"],
                state["report"],
                settings.scrutiny_threshold,
                precedents,
                window,
            )
        # Offered only where history has something to say. On everything else the
        # Approver runs exactly as it did before precedent existed — no bound
        # schema, no extra round-trip (see run_approver).
        tools = [build_precedent_tool(precedents)] if precedents.has_cases else None
        rounds = state.get("critique_rounds") or []
        feedback = rounds[-1].critique.feedback if rounds else None
        rec = _recorder(state)
        with rec.turn(
            "decide",
            "approver",
            round_no=len(rounds) + 1,
            # A redraft is literally caused by the Critic's revise verdict —
            # recorded as a self-FK on the spine, not inferred from round_no.
            triggered_by=rec.last_seq("critic") if feedback else None,
        ):
            decision, consulted = run_approver(
                llm, state["invoice"], state["report"], constraints, feedback, tools
            )
        if tools:
            trace.append(
                _ev("approval", "precedent:consulted", ", ".join(consulted))
                if consulted
                else _ev(
                    "approval",
                    "precedent:declined",
                    "the Approver was offered precedent and did not open it",
                )
            )
        trace.append(
            _ev(
                "approval",
                f"proposed:{decision.status}",
                decision.reasoning + (" [revision after critique]" if feedback else ""),
            )
        )
        return {
            "constraints": constraints,
            "decision": decision,
            "precedents": precedents,
            "trace": trace,
        }

    def critique(state: PipelineState) -> dict:
        decision = state["decision"]
        constraints = state["constraints"]
        rec = _recorder(state)
        rounds_so_far = len(state.get("critique_rounds") or [])
        with rec.turn(
            "critique",
            "critic",
            round_no=rounds_so_far + 1,
            triggered_by=rec.last_seq("approver"),
        ):
            # The same block the Approver was given, so a citation can be
            # checked rather than taken on trust. Empty when it had none.
            precedents = state.get("precedents")
            evidence = (
                precedent_block(precedents)
                if precedents is not None and precedents.has_cases
                else ""
            )
            crit = run_critic(
                llm, state["invoice"], state["report"], constraints, decision, evidence
            )
        trace = [_ev("approval", f"critique:{crit.verdict}", crit.feedback)]
        exhausted = (
            crit.verdict == CritiqueVerdict.REVISE
            and rounds_so_far + 1 > settings.max_critique_rounds
        )

        updates: dict = {"critique_rounds": [CritiqueRound(decision=decision, critique=crit)]}

        # Precedence, top to bottom: hard rules outrank the Critic, which
        # outranks the Approver. The first match terminates the chain — no
        # verdict below can soften it, not even an escalation to a human.
        def override(kind: str, to_status: ApprovalStatus, reasoning: str) -> None:
            """Replace the decision for routing, and record the replacement as
            its own fact: the Approver's words stay verbatim in the round."""
            updates["decision"] = decision.model_copy(
                update={"status": to_status, "reasoning": reasoning}
            )
            updates["overrides"] = [
                OverrideRecord(
                    round_no=rounds_so_far + 1,
                    kind=kind,
                    from_status=decision.status,
                    to_status=to_status,
                    reasoning=reasoning,
                    created_at=datetime.now(UTC).isoformat(timespec="seconds"),
                )
            ]

        if constraints.must_review:
            # Outranks must_reject on purpose. A rejection is an accusation, and
            # where the rules also say a fact could not be established, the
            # accusation is exactly what a person should confirm before it stands.
            if decision.status != ApprovalStatus.NEEDS_REVIEW:
                override(
                    "hard_rule_review",
                    ApprovalStatus.NEEDS_REVIEW,
                    "Hard business rule override: this invoice cannot be "
                    "decided automatically. " + "; ".join(constraints.review_reasons),
                )
                trace.append(
                    _ev("approval", "hard_rule_review", f"{decision.status} -> needs_review")
                )
        elif constraints.must_reject:
            # Already what the rules demand: leave the Approver's own reasoning
            # in place, and keep `hard_rule_override` meaning what it says —
            # an agent tried to talk its way past a hard rule.
            if decision.status != ApprovalStatus.REJECTED:
                override(
                    "hard_rule_reject",
                    ApprovalStatus.REJECTED,
                    "Hard business rule override: critical validation failures "
                    "forbid approval. " + "; ".join(constraints.reject_reasons),
                )
                trace.append(
                    _ev("approval", "hard_rule_override", f"{decision.status} -> rejected")
                )
        elif crit.verdict == CritiqueVerdict.ESCALATE or exhausted:
            reason = (
                "Approver and Critic could not converge; escalating to a human."
                if exhausted
                else crit.feedback
            )
            override(
                "critic_exhausted" if exhausted else "critic_escalation",
                ApprovalStatus.NEEDS_REVIEW,
                f"{decision.reasoning}\n\nEscalated by Critic: {reason}",
            )
            trace.append(_ev("approval", "escalated", reason))
        updates["trace"] = trace
        return updates

    def pay(state: PipelineState) -> dict:
        result = execute_payment(store, state["invoice"], state["run_id"])
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
        final_status = _final_status(state)
        trace = [_ev("record", f"final:{final_status}")]
        invoice = state.get("invoice")
        if invoice is not None and final_status not in (FinalStatus.FAILED, FinalStatus.DUPLICATE):
            # Written mid-run on purpose — the one mutable table: payment
            # idempotency must be visible to the very next run, not after some
            # later persistence step.
            written = store.record_settlement(
                invoice.invoice_number,
                invoice.content_hash(),
                invoice.vendor,
                invoice.total,
                final_status.value,
                state.get("run_pk"),
            )
            trace.append(
                _ev("record", "registry_updated", f"{invoice.invoice_number} -> {final_status}")
                if written
                else _ev("record", "registry_kept", f"{invoice.invoice_number} stays 'paid'")
            )
        return {"final_status": final_status, "trace": trace}

    # -- routing ------------------------------------------------------------

    def after_ingest(state: PipelineState) -> str:
        if state.get("error") or state.get("quarantine_reason"):
            return "record"
        return "validate"

    def after_validate(state: PipelineState) -> str:
        return "record" if state["report"].is_exact_duplicate else "decide"

    def after_critique(state: PipelineState) -> str:
        constraints = state["constraints"]
        rounds = state["critique_rounds"]
        verdict = rounds[-1].critique.verdict
        if (
            verdict == CritiqueVerdict.REVISE
            # A forced outcome is already settled: another Approver round could
            # only reword a decision the rules have made.
            and not constraints.outcome_is_forced
            and len(rounds) <= settings.max_critique_rounds
        ):
            return "decide"  # back to the Approver for another draft
        # The invariant, stated on the one edge where money moves: an invoice
        # the rules have decided is never paid, whatever the two agents said.
        if (
            state["decision"].status == ApprovalStatus.APPROVED
            and not constraints.outcome_is_forced
        ):
            return "pay"
        return "record"

    graph = StateGraph(PipelineState)
    graph.add_node("ingest", ingest)
    graph.add_node("validate", validate)
    graph.add_node("decide", decide)
    graph.add_node("critique", critique)
    graph.add_node("pay", pay)
    graph.add_node("record", record)
    graph.set_entry_point("ingest")
    graph.add_conditional_edges("ingest", after_ingest, ["validate", "record"])
    graph.add_conditional_edges("validate", after_validate, ["decide", "record"])
    graph.add_edge("decide", "critique")
    graph.add_conditional_edges("critique", after_critique, ["decide", "pay", "record"])
    graph.add_edge("pay", "record")
    graph.add_edge("record", END)
    return graph.compile()


def export_graph_image(compiled, out_dir: Path = DOCS_DIR) -> bool:
    """Render the compiled graph to docs/graph.png (mermaid source alongside it).

    Manual step, never part of a run: the PNG is rendered by mermaid.ink, and
    the case allows no external API but Grok. `--export-graph` is the only
    caller; `test_graph_diagram_is_current` catches a stale committed diagram
    offline, by comparing the mermaid source rather than fetching anything.

    Returns True when a new diagram was written, False when the committed one
    was already current. Rendering failures raise: this runs only because
    somebody asked for it, so reporting success on a silent no-op would be
    worse than the traceback — the caller decides how loudly to fail.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    drawable = compiled.get_graph()
    mermaid = drawable.draw_mermaid()
    mmd_path, png_path = out_dir / "graph.mmd", out_dir / "graph.png"
    if png_path.exists() and mmd_path.exists() and mmd_path.read_text() == mermaid:
        return False
    # PNG first: it is the step that can fail, and a written .mmd with a stale
    # .png would leave test_graph_diagram passing over a lying picture.
    png = drawable.draw_mermaid_png()
    png_path.write_bytes(png)
    mmd_path.write_text(mermaid)
    log.info("graph diagram written to %s", png_path)
    return True


def _final_status(state: PipelineState) -> FinalStatus:
    if state.get("error"):
        return FinalStatus.FAILED

    if state.get("quarantine_reason"):
        # Never auto-rejected: deciding whether a forged fence is an attack or
        # an artifact is the one judgement no agent here is fit to make.
        return FinalStatus.NEEDS_REVIEW

    report = state.get("report")
    if report is not None and report.is_exact_duplicate:
        return FinalStatus.DUPLICATE

    payment = state.get("payment")
    if payment is not None:
        # Both payer outcomes named: a status that is merely "not success" can
        # no longer be recorded as a duplicate by default. Nothing else can
        # reach here (PaymentStatus is validated at construction), and if it
        # somehow did it would fall through to the decision below — needs
        # review, never paid.
        match payment.status:
            case PaymentStatus.SUCCESS:
                return FinalStatus.PAID
            case PaymentStatus.SKIPPED_ALREADY_PAID:
                # NOT a duplicate: an exact duplicate never reaches the payer
                # (`after_validate` records it straight away), so the only way
                # here is a *revision* of something already paid — different
                # content, different sum, and a balance or a refund still
                # outstanding. `duplicate` is terminal and reaches no queue,
                # which would settle that difference by forgetting it.
                #
                # Unreachable while REVISION_OF_PAID_INVOICE forces review
                # before the `pay` edge; kept as the backstop for if it ever
                # stops doing so.
                return FinalStatus.NEEDS_REVIEW

    decision = state.get("decision")
    if decision is None:
        return FinalStatus.FAILED

    if decision.status == ApprovalStatus.REJECTED:
        return FinalStatus.REJECTED

    return FinalStatus.NEEDS_REVIEW
