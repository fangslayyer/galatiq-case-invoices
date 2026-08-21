"""Deterministic business rules that constrain the Approver agent.

The LLM reasons *within* these guardrails, never against them: a critical
validation issue is always a hard rejection, no matter how persuasive the
invoice notes are. Warnings and scrutiny are advisory — the agent weighs them.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .models import Invoice, Severity, ValidationReport


class RuleConstraints(BaseModel):
    must_reject: bool = False
    reject_reasons: list[str] = Field(default_factory=list)
    requires_scrutiny: bool = False
    scrutiny_reasons: list[str] = Field(default_factory=list)
    advisory_warnings: list[str] = Field(default_factory=list)


def evaluate_rules(
    invoice: Invoice, report: ValidationReport, scrutiny_threshold: float
) -> RuleConstraints:
    c = RuleConstraints()
    for issue in report.issues_at(Severity.CRITICAL):
        c.must_reject = True
        c.reject_reasons.append(f"{issue.code}: {issue.detail}")
    for issue in report.issues_at(Severity.WARNING):
        c.advisory_warnings.append(f"{issue.code}: {issue.detail}")
    if invoice.total is not None and invoice.total > scrutiny_threshold:
        c.requires_scrutiny = True
        c.scrutiny_reasons.append(
            f"total ${invoice.total:,.2f} exceeds the ${scrutiny_threshold:,.0f} review threshold"
        )
    return c
