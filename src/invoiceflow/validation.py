"""Validation tools — the deterministic instruments the Validator agent wields.

Each check is plain, unit-testable Python. The LLM's job is to decide which
checks to run and to interpret the combined results; it never does the math
itself. Tools are closed over a ValidationContext (current invoice + db) so
the model cannot hallucinate arguments. Each check is a plain function that
adds issues to the context; @check wraps it into the tool the agent sees.
"""

from __future__ import annotations

import functools
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field

from langchain_core.tools import BaseTool, tool

from .db import Database
from .models import Invoice, IssueCode, Severity, ValidationIssue
from .prompts import Tag

MONEY_TOLERANCE = 0.01


@dataclass
class ValidationContext:
    """Per-invoice context; collects every issue any tool finds."""

    invoice: Invoice
    db: Database
    expected_currency: str = "USD"
    # The document as loaded, before any agent saw it — the only text that can
    # carry an injection attempt. Empty when a caller checks an invoice alone.
    raw_text: str = ""
    issues: list[ValidationIssue] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)

    def add_issue(self, code: IssueCode, severity: Severity, detail: str) -> None:
        self.issues.append(ValidationIssue(code=code, severity=severity, detail=detail))

    def report(self, tool_name: str, since: int) -> str:
        """Close out a check: record that it ran, and render what it found.

        `since` is `len(self.issues)` captured before the check started, so
        `issues[since:]` is exactly this tool's own findings — the checks all
        append into one shared list.
        """
        self.tools_used.append(tool_name)
        found = self.issues[since:]
        if not found:
            return f"{tool_name}: OK, no issues found."
        lines = [f"- [{i.severity.upper()}] {i.code}: {i.detail}" for i in found]
        return f"{tool_name}: {len(found)} issue(s) found:\n" + "\n".join(lines)


Check = Callable[[ValidationContext], str]

#: Every check, keyed by the name the LLM calls it by. Populated by @check.
ALL_CHECKS: dict[str, Check] = {}


def check(fn: Callable[[ValidationContext], None]) -> Check:
    """Turn an issue-finding function into a registered validation tool.

    The body only finds issues; this wrapper owns the bookkeeping around it —
    the checkpoint, the report, the registry entry. All three take the name
    from `fn.__name__`, so the registry key, the name the LLM calls, and the
    entry in `tools_used` cannot drift apart.
    """

    @functools.wraps(fn)
    def run(ctx: ValidationContext) -> str:
        before = len(ctx.issues)
        fn(ctx)
        return ctx.report(fn.__name__, before)

    ALL_CHECKS[fn.__name__] = run
    return run


@check
def check_inventory(ctx: ValidationContext) -> None:
    """Verify every line item exists in inventory and the *aggregate* ordered
    quantity per item fits available stock."""
    invoice_qty_totals: dict[str, int] = defaultdict(int)

    for li in ctx.invoice.line_items:
        invoice_qty_totals[li.item] += li.quantity

    for item, invoice_qty in invoice_qty_totals.items():
        record = ctx.db.get_item(item)
        if record is None:
            ctx.add_issue(
                IssueCode.UNKNOWN_ITEM,
                Severity.WARNING,
                f"'{item}' is not in the inventory database",
            )
        elif record.stock == 0:
            ctx.add_issue(
                IssueCode.OUT_OF_STOCK,
                Severity.CRITICAL,
                f"'{item}' has zero stock (ordered {invoice_qty})",
            )
        elif invoice_qty > record.stock:
            ctx.add_issue(
                IssueCode.STOCK_EXCEEDED,
                Severity.CRITICAL,
                f"'{item}' total ordered quantity {invoice_qty} exceeds available stock {record.stock}",
            )


@check
def verify_arithmetic(ctx: ValidationContext) -> None:
    """Recompute line totals, subtotal, and grand total and compare with the
    amounts the vendor stated."""
    invoice = ctx.invoice
    computed_subtotal = 0.0

    for li in invoice.line_items:
        if li.unit_price is None:
            continue
        expected = li.quantity * li.unit_price
        computed_subtotal += expected
        if li.line_total is not None and abs(li.line_total - expected) > MONEY_TOLERANCE:
            ctx.add_issue(
                IssueCode.LINE_TOTAL_MISMATCH,
                Severity.WARNING,
                f"'{li.item}': stated line total {li.line_total:.2f} != "
                f"{li.quantity} x {li.unit_price:.2f} = {expected:.2f}",
            )

    if invoice.subtotal is not None and abs(invoice.subtotal - computed_subtotal) > MONEY_TOLERANCE:
        ctx.add_issue(
            IssueCode.SUBTOTAL_MISMATCH,
            Severity.WARNING,
            f"stated subtotal {invoice.subtotal:.2f} != computed {computed_subtotal:.2f}",
        )

    if invoice.total is not None:
        expected_total = (
            (invoice.subtotal if invoice.subtotal is not None else computed_subtotal)
            + (invoice.tax_amount or 0.0)
            + invoice.extra_charges
        )
        if abs(invoice.total - expected_total) > MONEY_TOLERANCE:
            ctx.add_issue(
                IssueCode.TOTAL_MISMATCH,
                Severity.WARNING,
                f"stated total {invoice.total:.2f} != subtotal + tax + charges = {expected_total:.2f}",
            )


@check
def check_integrity(ctx: ValidationContext) -> None:
    """Sanity-check the invoice data itself: required fields, negative values,
    suspicious dates, unexpected currency."""
    invoice = ctx.invoice

    if not invoice.vendor.strip():
        ctx.add_issue(IssueCode.MISSING_VENDOR, Severity.CRITICAL, "vendor name is missing")

    if not invoice.line_items:
        ctx.add_issue(IssueCode.NO_LINE_ITEMS, Severity.CRITICAL, "invoice has no line items")

    for li in invoice.line_items:
        if li.quantity < 0:
            ctx.add_issue(
                IssueCode.NEGATIVE_QUANTITY,
                Severity.CRITICAL,
                f"'{li.item}' has negative quantity {li.quantity}",
            )
    if invoice.total is None:
        # An absent total is a hole, not a finding: every other total check
        # below is guarded by `is not None`, so without this the invoice would
        # pass validation by having nothing left to check. Critical because
        # nothing can proceed without it — what the pipeline *does* about it is
        # the rule engine's call, not this severity's (see evaluate_rules).
        ctx.add_issue(
            IssueCode.MISSING_TOTAL,
            Severity.CRITICAL,
            "no total amount could be extracted, so the amount owed cannot be established",
        )
    elif invoice.total < 0:
        ctx.add_issue(
            IssueCode.NEGATIVE_AMOUNT, Severity.CRITICAL, f"total amount is negative ({invoice.total})"
        )

    if invoice.due_date is None:
        detail = (
            f"due date is not a parseable date: '{invoice.due_date_raw}'"
            if invoice.due_date_raw
            else "due date is missing"
        )
        ctx.add_issue(IssueCode.MISSING_DUE_DATE, Severity.WARNING, detail)
    elif invoice.invoice_date is not None and invoice.due_date <= invoice.invoice_date:
        ctx.add_issue(
            IssueCode.SUSPICIOUS_DUE_DATE,
            Severity.WARNING,
            f"due date {invoice.due_date} is not after invoice date {invoice.invoice_date}",
        )

    if invoice.currency.upper() != ctx.expected_currency.upper():
        ctx.add_issue(
            IssueCode.UNEXPECTED_CURRENCY,
            Severity.WARNING,
            f"invoice currency is {invoice.currency}, expected {ctx.expected_currency}",
        )


@check
def check_duplicate(ctx: ValidationContext) -> None:
    """Compare against the processed-invoice registry: exact duplicates must
    never be paid twice; same-number-different-content means a revision."""
    inv = ctx.invoice
    prior = ctx.db.get_processed(inv.invoice_number)
    if prior is not None:
        if prior.content_hash == inv.content_hash():
            ctx.add_issue(
                IssueCode.DUPLICATE_INVOICE,
                Severity.CRITICAL,
                f"{inv.invoice_number} was already processed "
                f"(status: {prior.final_status}) with identical content",
            )
        else:
            ctx.add_issue(
                IssueCode.REVISED_INVOICE,
                Severity.WARNING,
                f"{inv.invoice_number} was already processed (status: {prior.final_status}) "
                "but the content differs — this looks like a revised invoice",
            )


def forged_fence_issue(raw_text: str) -> ValidationIssue | None:
    """The prompt-safety verdict on a raw document, or None if it is clean.

    Takes text rather than a ValidationContext because the ingest gate runs it
    before an Invoice exists — see `_prompt_safety_gate` in graph.py. That gate
    is the real defense; `check_prompt_safety` below is the backstop for any
    path that reaches the validator without passing it.
    """
    forged_tags = Tag.scan(raw_text)
    if not forged_tags:
        return None
    labels = ", ".join(sorted(f"<{tag}>" for tag in forged_tags))
    return ValidationIssue(
        code=IssueCode.PROMPT_INJECTION_ATTEMPT,
        severity=Severity.WARNING,
        detail=f"source document contains this pipeline's own fence label(s) {labels}; "
        "it is trying to forge prompt structure",
    )


@check
def check_prompt_safety(ctx: ValidationContext) -> None:
    """Detect a prompt-injection attempt: fence labels this pipeline uses,
    forged inside the vendor-supplied document text."""
    issue = forged_fence_issue(ctx.raw_text)
    if issue is not None:
        ctx.issues.append(issue)


def build_tools(ctx: ValidationContext) -> list[BaseTool]:
    """Wrap the checks as no-argument LangChain tools bound to this context."""

    def make(name: str, fn: Check) -> BaseTool:
        @tool(name, description=fn.__doc__ or name)
        def _run() -> str:
            return fn(ctx)

        return _run

    return [make(name, fn) for name, fn in ALL_CHECKS.items()]
