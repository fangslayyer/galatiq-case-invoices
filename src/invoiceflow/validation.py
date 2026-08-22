"""Validation tools — the deterministic instruments the Validator agent wields.

Each check is plain, unit-testable Python. The LLM's job is to decide which
checks to run and to interpret the combined results; it never does the math
itself. Tools are closed over a ValidationContext (current invoice + db) so
the model cannot hallucinate arguments.
"""

from __future__ import annotations

from collections import defaultdict
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

    def add(self, code: IssueCode, severity: Severity, detail: str) -> None:
        self.issues.append(ValidationIssue(code=code, severity=severity, detail=detail))


def _report(ctx: ValidationContext, tool_name: str, found: list[ValidationIssue]) -> str:
    ctx.tools_used.append(tool_name)
    if not found:
        return f"{tool_name}: OK, no issues found."
    lines = [f"- [{i.severity.upper()}] {i.code}: {i.detail}" for i in found]
    return f"{tool_name}: {len(found)} issue(s) found:\n" + "\n".join(lines)


def check_inventory(ctx: ValidationContext) -> str:
    """Verify every line item exists in inventory and the *aggregate* ordered
    quantity per item fits available stock."""
    before = len(ctx.issues)
    totals: dict[str, int] = defaultdict(int)
    for li in ctx.invoice.line_items:
        totals[li.item] += li.quantity
    for item, qty in totals.items():
        rec = ctx.db.get_item(item)
        if rec is None:
            ctx.add(
                IssueCode.UNKNOWN_ITEM,
                Severity.WARNING,
                f"'{item}' is not in the inventory database",
            )
        elif rec.stock == 0:
            ctx.add(
                IssueCode.OUT_OF_STOCK,
                Severity.CRITICAL,
                f"'{item}' has zero stock (ordered {qty})",
            )
        elif qty > rec.stock:
            ctx.add(
                IssueCode.STOCK_EXCEEDED,
                Severity.CRITICAL,
                f"'{item}' total ordered quantity {qty} exceeds available stock {rec.stock}",
            )
    return _report(ctx, "check_inventory", ctx.issues[before:])


def verify_arithmetic(ctx: ValidationContext) -> str:
    """Recompute line totals, subtotal, and grand total and compare with the
    amounts the vendor stated."""
    before = len(ctx.issues)
    inv = ctx.invoice
    computed_subtotal = 0.0
    for li in inv.line_items:
        if li.unit_price is None:
            continue
        expected = li.quantity * li.unit_price
        computed_subtotal += expected
        if li.line_total is not None and abs(li.line_total - expected) > MONEY_TOLERANCE:
            ctx.add(
                IssueCode.LINE_TOTAL_MISMATCH,
                Severity.WARNING,
                f"'{li.item}': stated line total {li.line_total:.2f} != "
                f"{li.quantity} x {li.unit_price:.2f} = {expected:.2f}",
            )
    if inv.subtotal is not None and abs(inv.subtotal - computed_subtotal) > MONEY_TOLERANCE:
        ctx.add(
            IssueCode.SUBTOTAL_MISMATCH,
            Severity.WARNING,
            f"stated subtotal {inv.subtotal:.2f} != computed {computed_subtotal:.2f}",
        )
    if inv.total is not None:
        expected_total = (
            (inv.subtotal if inv.subtotal is not None else computed_subtotal)
            + (inv.tax_amount or 0.0)
            + inv.extra_charges
        )
        if abs(inv.total - expected_total) > MONEY_TOLERANCE:
            ctx.add(
                IssueCode.TOTAL_MISMATCH,
                Severity.WARNING,
                f"stated total {inv.total:.2f} != subtotal + tax + charges = {expected_total:.2f}",
            )
    return _report(ctx, "verify_arithmetic", ctx.issues[before:])


def check_integrity(ctx: ValidationContext) -> str:
    """Sanity-check the invoice data itself: required fields, negative values,
    suspicious dates, unexpected currency."""
    before = len(ctx.issues)
    inv = ctx.invoice
    if not inv.vendor.strip():
        ctx.add(IssueCode.MISSING_VENDOR, Severity.CRITICAL, "vendor name is missing")
    if not inv.line_items:
        ctx.add(IssueCode.NO_LINE_ITEMS, Severity.CRITICAL, "invoice has no line items")
    for li in inv.line_items:
        if li.quantity < 0:
            ctx.add(
                IssueCode.NEGATIVE_QUANTITY,
                Severity.CRITICAL,
                f"'{li.item}' has negative quantity {li.quantity}",
            )
    if inv.total is not None and inv.total < 0:
        ctx.add(
            IssueCode.NEGATIVE_AMOUNT, Severity.CRITICAL, f"total amount is negative ({inv.total})"
        )
    if inv.due_date is None:
        detail = (
            f"due date is not a parseable date: '{inv.due_date_raw}'"
            if inv.due_date_raw
            else "due date is missing"
        )
        ctx.add(IssueCode.MISSING_DUE_DATE, Severity.WARNING, detail)
    elif inv.invoice_date is not None and inv.due_date <= inv.invoice_date:
        ctx.add(
            IssueCode.SUSPICIOUS_DUE_DATE,
            Severity.WARNING,
            f"due date {inv.due_date} is not after invoice date {inv.invoice_date}",
        )
    if inv.currency.upper() != ctx.expected_currency.upper():
        ctx.add(
            IssueCode.UNEXPECTED_CURRENCY,
            Severity.WARNING,
            f"invoice currency is {inv.currency}, expected {ctx.expected_currency}",
        )
    return _report(ctx, "check_integrity", ctx.issues[before:])


def check_duplicate(ctx: ValidationContext) -> str:
    """Compare against the processed-invoice registry: exact duplicates must
    never be paid twice; same-number-different-content means a revision."""
    before = len(ctx.issues)
    inv = ctx.invoice
    prior = ctx.db.get_processed(inv.invoice_number)
    if prior is not None:
        if prior.content_hash == inv.content_hash():
            ctx.add(
                IssueCode.DUPLICATE_INVOICE,
                Severity.CRITICAL,
                f"{inv.invoice_number} was already processed "
                f"(status: {prior.final_status}) with identical content",
            )
        else:
            ctx.add(
                IssueCode.REVISED_INVOICE,
                Severity.WARNING,
                f"{inv.invoice_number} was already processed (status: {prior.final_status}) "
                "but the content differs — this looks like a revised invoice",
            )
    return _report(ctx, "check_duplicate", ctx.issues[before:])


def forged_fence_issue(raw_text: str) -> ValidationIssue | None:
    """The prompt-safety verdict on a raw document, or None if it is clean.

    Takes text rather than a ValidationContext because the ingest gate runs it
    before an Invoice exists — see `_prompt_safety_gate` in graph.py. That gate
    is the real defense; `check_prompt_safety` below is the backstop for any
    path that reaches the validator without passing it.
    """
    forged = Tag.scan(raw_text)
    if not forged:
        return None
    labels = ", ".join(sorted(f"<{tag}>" for tag in forged))
    return ValidationIssue(
        code=IssueCode.PROMPT_INJECTION_ATTEMPT,
        severity=Severity.WARNING,
        detail=f"source document contains this pipeline's own fence label(s) {labels}; "
        "it is trying to forge prompt structure",
    )


def check_prompt_safety(ctx: ValidationContext) -> str:
    """Detect a prompt-injection attempt: fence labels this pipeline uses,
    forged inside the vendor-supplied document text."""
    before = len(ctx.issues)
    issue = forged_fence_issue(ctx.raw_text)
    if issue is not None:
        ctx.issues.append(issue)
    return _report(ctx, "check_prompt_safety", ctx.issues[before:])


ALL_CHECKS = {
    "check_inventory": check_inventory,
    "verify_arithmetic": verify_arithmetic,
    "check_integrity": check_integrity,
    "check_duplicate": check_duplicate,
    "check_prompt_safety": check_prompt_safety,
}


def build_tools(ctx: ValidationContext) -> list[BaseTool]:
    """Wrap the checks as no-argument LangChain tools bound to this context."""

    def make(name: str, fn) -> BaseTool:
        @tool(name, description=fn.__doc__ or name)
        def _run() -> str:
            return fn(ctx)

        return _run

    return [make(name, fn) for name, fn in ALL_CHECKS.items()]
