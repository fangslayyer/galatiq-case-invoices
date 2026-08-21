"""Shared LangGraph state flowing between the agents."""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

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
    source_file: str
    run_id: str
    started_at: str
    raw_text: str
    invoice: Invoice
    extraction_retries: int
    report: ValidationReport
    constraints: RuleConstraints
    decision: ApprovalDecision
    critique_rounds: Annotated[list[CritiqueRound], operator.add]
    payment: PaymentResult
    final_status: FinalStatus
    error: str
    trace: Annotated[list[TraceEvent], operator.add]
