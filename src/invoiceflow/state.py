"""Shared LangGraph state flowing between the agents."""

from __future__ import annotations

import operator
from typing import Annotated, Required, TypedDict

from .models import (
    ApprovalDecision,
    CritiqueRound,
    FinalStatus,
    Invoice,
    PaymentResult,
    TraceEvent,
    ValidationReport,
)
from .rules import RuleConstraints


class PipelineState(TypedDict, total=False):
    """State is `total=False` because LangGraph nodes return *partial* updates —
    only the keys they changed. The keys the runner supplies at `invoke()` are
    marked Required; the rest are filled in as the graph advances, and which
    ones exist at a given node is guaranteed by the graph's topology (see the
    routing functions in graph.py) rather than by this type.
    """

    # Supplied by Pipeline.run() at invoke time — present at every node.
    source_file: Required[str]
    run_id: Required[str]
    started_at: Required[str]
    trace: Required[Annotated[list[TraceEvent], operator.add]]
    critique_rounds: Required[Annotated[list[CritiqueRound], operator.add]]

    # Produced as the pipeline runs.
    raw_text: str
    invoice: Invoice
    extraction_retries: int
    report: ValidationReport
    constraints: RuleConstraints
    decision: ApprovalDecision
    payment: PaymentResult
    final_status: FinalStatus
    error: str
