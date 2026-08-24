"""Shared LangGraph state flowing between the agents."""

from __future__ import annotations

import operator
from typing import Annotated, Required, TypedDict

from .models import (
    ApprovalDecision,
    CritiqueRound,
    FinalStatus,
    Invoice,
    OverrideRecord,
    PaymentResult,
    PrecedentBundle,
    TraceEvent,
    ValidationReport,
)
from .recording import RunRecorder
from .rules import RuleConstraints


class PipelineState(TypedDict, total=False):
    """State is `total=False` because LangGraph nodes return *partial* updates —
    only the keys they changed. The keys the runner supplies at `invoke()` are
    marked Required; the rest are filled in as the graph advances, and which
    ones exist at a given node is guaranteed by the graph's topology (see the
    routing functions in graph.py) rather than by this type.
    """

    # Supplied by Pipeline.run() at invoke time — present at every node.
    source_file_path: Required[str]
    run_id: Required[str]
    started_at: Required[str]
    trace: Required[Annotated[list[TraceEvent], operator.add]]
    critique_rounds: Required[Annotated[list[CritiqueRound], operator.add]]

    # Observability plumbing, also supplied by the runner. Optional so the
    # graph can still be driven bare in tests: nodes fall back to a throwaway
    # recorder and a registry entry with no run reference.
    recorder: RunRecorder
    run_pk: int  # runs.id — exists from begin_run, so the registry can FK it

    # Produced as the pipeline runs.
    raw_text: str
    # Set only by the ingest gate: the document forged prompt structure, so it
    # was never shown to an LLM. Distinct from `error` — this is not a failure.
    quarantine_reason: str
    invoice: Invoice
    extraction_retries: int
    # One entry per failed extraction attempt: the feedback fed back into the
    # retry prompt (extraction_attempts in the run store).
    extraction_attempts: list[str]
    # Which document (by content) this run processed, and how many runs have
    # now touched it — 2+ means a reprocess, surfaced as a CLI notice.
    document_id: int
    document_run_no: int
    report: ValidationReport
    constraints: RuleConstraints
    # What history says about this invoice's open questions, resolved once in
    # `decide` and reused by every later round and by the Critic: a redraft must
    # be judged against the same evidence as the draft it replaces.
    precedents: PrecedentBundle
    decision: ApprovalDecision
    # System overrides of agent decisions (hard rules, critic escalation) —
    # appended by the critique node, persisted as decision_overrides.
    overrides: Annotated[list[OverrideRecord], operator.add]
    payment: PaymentResult
    final_status: FinalStatus
    error: str
