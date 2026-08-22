"""Domain models shared by every agent in the pipeline.

Everything the LLM produces is bound to one of these schemas via structured
output — no free-text parsing anywhere in the system.
"""

from __future__ import annotations

import hashlib
from datetime import date
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


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
    MISSING_TOTAL = "missing_total"
    MISSING_DUE_DATE = "missing_due_date"
    SUSPICIOUS_DUE_DATE = "suspicious_due_date"
    UNEXPECTED_CURRENCY = "unexpected_currency"
    NO_LINE_ITEMS = "no_line_items"
    # duplicates
    DUPLICATE_INVOICE = "duplicate_invoice"
    REVISED_INVOICE = "revised_invoice"
    # prompt safety
    PROMPT_INJECTION_ATTEMPT = "prompt_injection_attempt"
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
    """The Validator agent's own read of the tool results.

    `extra_issues` is the agent's free-form observation channel, and it is
    clamped on the way in: the model may report anything it noticed, but it may
    not mint the codes and severities the rest of the pipeline routes on. An
    unclamped summary claiming `duplicate_invoice`/`critical` would send the run
    straight to `record` as a duplicate, and *any* critical code would trip
    `must_reject` in `evaluate_rules` — control flow authored by the text being
    judged. Same principle as `Tag.wrap`: structure is ours, model output is
    data.
    """

    summary: str = Field(description="One-paragraph assessment of the invoice's validity")
    extra_issues: list[ValidationIssue] = Field(
        default_factory=list,
        description="Additional issues the agent noticed that no tool covers "
        "(use code 'agent_observation')",
    )

    @field_validator("extra_issues")
    @classmethod
    def _demote_to_observations(cls, issues: list[ValidationIssue]) -> list[ValidationIssue]:
        """Rewrite every agent-authored issue into an advisory observation.

        A prompt asking for `agent_observation` is a request; this is the
        constraint. CRITICAL is downgraded rather than dropped, so the agent's
        concern still reaches the Approver as an advisory warning: it loses its
        authority over the graph, not its voice.
        """
        return [
            issue.model_copy(
                update={
                    "code": IssueCode.AGENT_OBSERVATION,
                    "severity": (
                        Severity.WARNING if issue.severity == Severity.CRITICAL else issue.severity
                    ),
                }
            )
            for issue in issues
        ]


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

    @property
    def is_exact_duplicate(self) -> bool:
        """True when the registry already holds this invoice byte-for-byte.

        The one issue code that steers the graph by itself: an exact duplicate
        skips approval entirely and is recorded unpaid. The code alone is a
        sufficient test because `check_duplicate` is its only author — it raises
        `REVISED_INVOICE` for a same-number-different-content match, and
        `ValidatorSummary` demotes every agent-authored issue to
        `AGENT_OBSERVATION`. Kept here rather than spelled out at each routing
        site so the two cannot drift apart.
        """
        return any(i.code == IssueCode.DUPLICATE_INVOICE for i in self.issues)


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


class PaymentStatus(StrEnum):
    """The outcomes the payer can produce. Only these two exist: the mock bank
    reports success, and the idempotency guard refuses an already-paid invoice.
    Typing the field makes `PaymentResult(status=...)` the validation boundary
    for whatever the banking API hands back."""

    SUCCESS = "success"
    SKIPPED_ALREADY_PAID = "skipped_already_paid"


class PaymentResult(BaseModel):
    status: PaymentStatus
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
    source_file_path: str
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
    # Set when a person acts on the run in the dashboard — overturning it or
    # confirming it as it stands. Empty means no human has looked at it yet,
    # which is what lets the UI count outstanding auto-rejections.
    human_reviewed_at: str = ""
