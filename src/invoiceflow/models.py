"""Domain models shared by every agent in the pipeline.

Everything the LLM produces is bound to one of these schemas via structured
output — no free-text parsing anywhere in the system.
"""

from __future__ import annotations

import hashlib
from datetime import date
from enum import StrEnum

from pydantic import BaseModel, Field


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class IssueCode(StrEnum):
    # inventory
    UNKNOWN_ITEM = "unknown_item"
    OUT_OF_STOCK = "out_of_stock"
    STOCK_EXCEEDED = "stock_exceeded"
    # arithmetic
    LINE_TOTAL_MISMATCH = "line_total_mismatch"
    SUBTOTAL_MISMATCH = "subtotal_mismatch"
    TOTAL_MISMATCH = "total_mismatch"
    # integrity
    NEGATIVE_QUANTITY = "negative_quantity"
    NEGATIVE_AMOUNT = "negative_amount"
    MISSING_VENDOR = "missing_vendor"
    MISSING_DUE_DATE = "missing_due_date"
    SUSPICIOUS_DUE_DATE = "suspicious_due_date"
    UNEXPECTED_CURRENCY = "unexpected_currency"
    NO_LINE_ITEMS = "no_line_items"
    # duplicates
    DUPLICATE_INVOICE = "duplicate_invoice"
    REVISED_INVOICE = "revised_invoice"
    # free-form observation from an agent
    AGENT_OBSERVATION = "agent_observation"


class FinalStatus(StrEnum):
    PAID = "paid"
    REJECTED = "rejected"
    NEEDS_REVIEW = "needs_review"
    DUPLICATE = "duplicate"
    FAILED = "failed"  # pipeline could not process the file at all


class ApprovalStatus(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    NEEDS_REVIEW = "needs_review"


# ---------------------------------------------------------------------------
# Invoice (Extractor agent output)
# ---------------------------------------------------------------------------


class LineItem(BaseModel):
    """A single line on the invoice, exactly as stated by the vendor."""

    item: str = Field(description="Canonical item name, e.g. 'WidgetA' (no spaces)")
    quantity: int = Field(description="Quantity ordered; keep negative values as-is")
    unit_price: float | None = Field(default=None, description="Price per unit")
    line_total: float | None = Field(default=None, description="Stated line total, if present")
    note: str | None = Field(default=None, description="Any note attached to the line")


class Invoice(BaseModel):
    """Structured invoice as extracted from the raw document."""

    invoice_number: str = Field(description="e.g. 'INV-1001'; normalize 'INV 1012' -> 'INV-1012'")
    vendor: str = Field(default="", description="Vendor name; empty string if missing")
    invoice_date: date | None = Field(default=None, description="Issue date")
    due_date: date | None = Field(
        default=None, description="Due date; null if missing or not a real date"
    )
    due_date_raw: str | None = Field(
        default=None, description="Verbatim due date text when it is not a parseable date"
    )
    line_items: list[LineItem] = Field(default_factory=list)
    subtotal: float | None = None
    tax_amount: float | None = None
    extra_charges: float = Field(
        default=0.0, description="Shipping/handling and other non-tax charges"
    )
    total: float | None = Field(default=None, description="Stated total amount")
    currency: str = Field(default="USD")
    payment_terms: str = Field(default="")
    notes: str = Field(default="", description="Free-form notes / cover text from the document")

    def content_hash(self) -> str:
        """Canonical fingerprint used for duplicate detection across formats."""
        items = sorted(
            (li.item, li.quantity, round(li.unit_price or 0, 2)) for li in self.line_items
        )
        canon = f"{self.invoice_number}|{self.vendor.strip().lower()}|{self.total}|{items}"
        return hashlib.sha256(canon.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Validation (Validator agent output)
# ---------------------------------------------------------------------------


class ValidationIssue(BaseModel):
    code: IssueCode
    severity: Severity
    detail: str


class ValidatorSummary(BaseModel):
    """The Validator agent's own read of the tool results."""

    summary: str = Field(description="One-paragraph assessment of the invoice's validity")
    extra_issues: list[ValidationIssue] = Field(
        default_factory=list,
        description="Additional issues the agent noticed that no tool covers "
        "(use code 'agent_observation')",
    )


class ValidationReport(BaseModel):
    issues: list[ValidationIssue] = Field(default_factory=list)
    summary: str = ""
    tools_used: list[str] = Field(default_factory=list)

    @property
    def has_critical(self) -> bool:
        return any(i.severity == Severity.CRITICAL for i in self.issues)

    @property
    def has_warning(self) -> bool:
        return any(i.severity == Severity.WARNING for i in self.issues)

    def issues_at(self, severity: Severity) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == severity]


# ---------------------------------------------------------------------------
# Approval (Approver + Critic agent outputs)
# ---------------------------------------------------------------------------


class ApprovalDecision(BaseModel):
    status: ApprovalStatus
    reasoning: str = Field(description="Business-readable rationale for the decision")
    risk_factors: list[str] = Field(default_factory=list)


class CritiqueVerdict(StrEnum):
    ACCEPT = "accept"
    REVISE = "revise"
    ESCALATE = "escalate"


class Critique(BaseModel):
    verdict: CritiqueVerdict
    feedback: str = Field(description="What the Approver missed, or why the decision stands")


class CritiqueRound(BaseModel):
    decision: ApprovalDecision
    critique: Critique


# ---------------------------------------------------------------------------
# Payment / final result
# ---------------------------------------------------------------------------


class PaymentResult(BaseModel):
    status: str
    vendor: str
    amount: float
    reference: str = ""


class TraceEvent(BaseModel):
    stage: str
    event: str
    detail: str = ""


class InvoiceRunResult(BaseModel):
    """Everything about one invoice's trip through the pipeline; persisted as JSON."""

    run_id: str
    source_file: str
    started_at: str
    finished_at: str = ""
    llm_backend: str = ""
    final_status: FinalStatus
    invoice: Invoice | None = None
    validation: ValidationReport | None = None
    decision: ApprovalDecision | None = None
    critique_rounds: list[CritiqueRound] = Field(default_factory=list)
    payment: PaymentResult | None = None
    error: str = ""
    trace: list[TraceEvent] = Field(default_factory=list)
