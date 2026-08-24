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
    REVISION_OF_PAID_INVOICE = "revision_of_paid_invoice"
    # prompt safety
    PROMPT_INJECTION_ATTEMPT = "prompt_injection_attempt"
    # free-form observation from an agent
    AGENT_OBSERVATION = "agent_observation"


class RuleReasonKind(StrEnum):
    """Which constraint bucket a rule reason belongs to (rule_reasons.kind)."""

    REJECT = "reject"
    REVIEW = "review"
    SCRUTINY = "scrutiny"
    ADVISORY = "advisory"
    #: A finding a person has already settled often enough that the rules stopped
    #: insisting on another one. Kept apart from `advisory` deliberately: the
    #: whole point of a discharge is that the warning is answered, and an
    #: answered warning listed among the open ones would be re-litigated.
    PRECEDENT = "precedent"


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
    #: What this finding is *about*, when it is about a thing: the item name, the
    #: currency, the invoice number. Empty when the finding is about the vendor's
    #: practice as such — arithmetic drift and dating quirks belong to the vendor,
    #: not to any one value on the page. It exists so precedent can be matched on
    #: the question rather than on the prose in `detail`, which would be guesswork.
    subject: str = ""


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

        `subject` is cleared for the same reason the code is: it is the key
        precedent is looked up and accumulated by, and a model that could mint
        one could write itself a history.
        """
        return [
            issue.model_copy(
                update={
                    "code": IssueCode.AGENT_OBSERVATION,
                    "severity": (
                        Severity.WARNING if issue.severity == Severity.CRITICAL else issue.severity
                    ),
                    "subject": "",
                }
            )
            for issue in issues
        ]


class ValidationReport(BaseModel):
    issues: list[ValidationIssue] = Field(default_factory=list)
    summary: str = ""
    tools_used: list[str] = Field(default_factory=list)
    # The subset of tools_used the pipeline ran because the agent skipped them.
    # Persisted as validation_tool_runs.invoked_by: it is the honest measure of
    # how much of the tool loop's coverage the agent actually chose.
    safety_net_tools: list[str] = Field(default_factory=list)

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
# Precedent — what human reviewers have already settled
# ---------------------------------------------------------------------------


class PrecedentCase(BaseModel):
    """One prior invoice where a person answered this same question.

    Only ever built from a run a *human* resolved. An automatic decision is not
    evidence about anything: letting one in would have the system's own output
    vote for its next output, and a single approval would compound into
    unlimited authority.
    """

    run_id: str
    invoice_number: str
    total: float | None = None
    currency: str = "USD"
    #: The quantity this finding's code puts at risk on *that* invoice — the
    #: discrepancy for an arithmetic finding, the total for a currency one.
    #: Comparability is measured on it, never on the invoice total by default.
    at_risk: float = 0.0
    reviewed_at: str = ""
    action: str = ""  # override_approve | confirm
    note: str = ""
    #: This case's contribution to `Precedent.support`, after the comparability
    #: and recency multipliers.
    weight: float = 0.0


class Precedent(BaseModel):
    """History's answer to one open question on the invoice being decided.

    The question is `(code, subject, vendor)` — an exact key, deliberately:
    two invoices are similar here when they raise the same finding about the
    same thing for the same vendor, which is a stricter and more auditable test
    than any resemblance between the documents themselves.
    """

    code: IssueCode
    subject: str
    vendor: str
    #: The current invoice's finding, verbatim, so a citation can be read
    #: without joining back to the validation report.
    detail: str = ""
    at_risk: float = 0.0
    burden: float = 0.0
    support: float = 0.0
    cases: list[PrecedentCase] = Field(default_factory=list)
    #: Prior invoices on this key a person *rejected*. Any at all zeroes the
    #: support: mixed history is not evidence, it is a disagreement.
    rejections: int = 0
    #: Why release is barred outright, whatever the arithmetic says. Empty when
    #: nothing bars it. Kept as prose because it is shown to a reviewer.
    blocked_by: str = ""
    #: Set by `rules.precedent_releases` when the bundle is built — the policy
    #: call, made once so the rule engine, the citation row and the dashboard
    #: cannot each reach a different conclusion from the same numbers.
    released: bool = False
    #: The burden/support breakdown, term by term. Persisted rather than only
    #: the two totals: it is what makes `precedent_citations` a training log
    #: and not merely an audit trail.
    terms: dict[str, float] = Field(default_factory=dict)

    @property
    def cited_run_ids(self) -> list[str]:
        return [c.run_id for c in self.cases]

    def summary_line(self) -> str:
        """One line a person or a model can read the whole verdict from."""
        who = f"{self.code}" + (f" '{self.subject}'" if self.subject else "")
        if self.blocked_by:
            return f"{who} @ {self.vendor}: precedent does not apply — {self.blocked_by}"
        if not self.cases:
            return f"{who} @ {self.vendor}: no comparable prior decision exists"
        cases = f"{len(self.cases)} prior invoice(s) approved by a person"
        if self.rejections:
            cases += f", but {self.rejections} rejected by a person"
        verdict = "settled" if self.released else "not settled"
        return (
            f"{who} @ {self.vendor}: {cases} "
            f"(support {self.support:.2f} vs burden {self.burden:.2f} — {verdict})"
        )


class PrecedentBundle(BaseModel):
    """Everything history has to say about one invoice's open questions."""

    findings: list[Precedent] = Field(default_factory=list)

    def for_issue(self, code: IssueCode, subject: str) -> Precedent | None:
        for p in self.findings:
            if p.code == code and p.subject == subject:
                return p
        return None

    @property
    def has_cases(self) -> bool:
        """Whether history has anything at all to say.

        This is the gate on offering the Approver its tool: binding a schema and
        paying a round-trip so a model can be told "no prior cases" is cost for
        nothing, and the same sentence fits in one line of a block it already reads.
        """
        return any(p.cases for p in self.findings)


# ---------------------------------------------------------------------------
# Vendor window — what else this vendor billed, close in time
# ---------------------------------------------------------------------------


class RecentInvoice(BaseModel):
    """One invoice already in the registry, as the scrutiny rule needs to read it.

    Deliberately thin: an invoice number, a date, a sum and where it stands.
    Nothing here re-decides a prior invoice — it is counted, not re-judged.
    """

    invoice_number: str
    #: Verbatim, exactly as recorded. `vendor_key` is the one place that decides
    #: when two spellings are one company, so this must not arrive normalised.
    vendor: str = ""
    invoice_date: date | None = None
    total: float = 0.0
    final_status: FinalStatus = FinalStatus.PAID

    def summary_line(self) -> str:
        """How this invoice is cited in a scrutiny reason a person will read."""
        when = f", {self.invoice_date}" if self.invoice_date else ""
        return f"{self.invoice_number} (${self.total:,.2f}{when}, {self.final_status})"


class VendorWindow(BaseModel):
    """The same vendor's *other* invoices dated within `days` of this one.

    The scrutiny threshold is a rule about money leaving the company, and money
    does not care how many pages it was billed on. This is the population the
    rule engine applies it to: three invoices of $4,860, $4,320 and $5,400 four
    days apart are one $14,580 payment as far as the threshold is concerned.

    Empty is the normal case and means exactly what it says — nothing else from
    this vendor is close enough in time to be part of the same payment.
    """

    days: int = 0
    invoices: list[RecentInvoice] = Field(default_factory=list)

    @property
    def total(self) -> float:
        return sum(i.total for i in self.invoices)


# ---------------------------------------------------------------------------
# Approval (Approver + Critic agent outputs)
# ---------------------------------------------------------------------------


class ApprovalDecision(BaseModel):
    status: ApprovalStatus
    reasoning: str = Field(description="Business-readable rationale for the decision")
    risk_factors: list[str] = Field(default_factory=list)


class CritiqueVerdict(StrEnum):
    """What the Critic concluded about the *Approver's decision*.

    Every member judges the decision, never the invoice: AFFIRM on a rejection
    means the rejection was right, not that the invoice is good. Named `affirm`
    rather than `accept` for exactly that reason — an "accepted" invoice is the
    one thing this enum never means.
    """

    AFFIRM = "affirm"
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
    paid_at: str = ""


class TraceEvent(BaseModel):
    stage: str
    event: str
    detail: str = ""
    at: str = ""  # ISO timestamp; gives the trace per-stage timings


class OverrideRecord(BaseModel):
    """What the *system* did about an agent decision: a hard rule or the
    critique loop replacing it. The agent's own words stay untouched in
    `critique_rounds`; this row is the replacement and its justification."""

    round_no: int
    kind: str  # hard_rule_review | hard_rule_reject | critic_escalation | critic_exhausted
    from_status: ApprovalStatus
    to_status: ApprovalStatus
    reasoning: str
    created_at: str = ""


class HumanReview(BaseModel):
    """One person acting on a run in the dashboard — confirm or overturn."""

    reviewed_at: str
    reviewer: str = "dashboard"
    action: str  # confirm | override_approve | override_reject
    from_status: FinalStatus
    to_status: FinalStatus
    note: str = ""


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
    # 2, 3, ... when this same document (by content) was processed before —
    # the CLI surfaces it as a non-blocking reprocessing notice.
    document_run_no: int = 1
    # System overrides of agent decisions, in order. The decisions themselves
    # stay verbatim in critique_rounds; these are what replaced them and why.
    overrides: list[OverrideRecord] = Field(default_factory=list)
    # Every person who acted on this run, newest last. The agents' output is
    # never edited by a review; the effective status is derived instead.
    human_reviews: list[HumanReview] = Field(default_factory=list)
    # Set when a person acts on the run in the dashboard — overturning it or
    # confirming it as it stands. Empty means no human has looked at it yet,
    # which is what lets the UI count outstanding auto-rejections.
    human_reviewed_at: str = ""
