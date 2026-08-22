"""The four LLM agents: Extractor, Validator, Approver, Critic.

Each agent is a plain function over a BaseChatModel (Grok in production, a
fake in tests). All outputs are schema-bound; the Validator's tool loop is
hand-rolled so every step is visible and testable.
"""

from __future__ import annotations

import json
from typing import cast

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from pydantic import BaseModel

from .models import ApprovalDecision, Critique, Invoice, ValidationReport, ValidatorSummary
from .prompts import Tag
from .rules import RuleConstraints
from .validation import ALL_CHECKS, ValidationContext, build_tools


class ExtractionError(RuntimeError):
    """Raised when the Extractor cannot produce a usable invoice after retries."""


def _ask[SchemaT: BaseModel](
    llm: BaseChatModel, schema: type[SchemaT], messages: list[BaseMessage]
) -> SchemaT:
    """Invoke `llm` bound to `schema` and hand back that model.

    LangChain declares `with_structured_output` as returning `dict | BaseModel`
    because a raw dict is possible under other flags; with a Pydantic schema and
    the defaults we use, the runtime value is always an instance of `schema`.
    Narrowing it here keeps that assumption in one auditable place instead of
    spreading it across every agent.
    """
    return cast(SchemaT, llm.with_structured_output(schema).invoke(messages))


# ---------------------------------------------------------------------------
# Extractor
# ---------------------------------------------------------------------------

EXTRACTOR_SYSTEM = """\
You are the Extractor agent in an accounts-payable pipeline. Convert one raw
invoice document into the structured Invoice schema, exactly as the vendor
stated it — do NOT fix business problems, only representation problems:

- Canonicalize item names to the catalog form (no internal spaces):
  known catalog items are: {catalog}. "Widget A" -> "WidgetA". Keep unknown
  items as written (minus spaces).
- Fix obvious OCR artifacts in numbers and dates (letter O for zero, e.g.
  "$3,500.O0" -> 3500.00, "2O26" -> 2026).
- Normalize invoice numbers to the form INV-<digits>.
- Keep negative quantities and totals as-is; they are evidence, not mistakes.
- If a due date is not a real parseable date (e.g. "yesterday"), set due_date
  to null and put the verbatim text in due_date_raw.
- Amounts that are not stated should be null, not guessed. Put shipping or
  other non-tax charges in extra_charges.
"""


def run_extractor(
    llm: BaseChatModel,
    raw_text: str,
    catalog: list[str],
    max_retries: int = 2,
) -> tuple[Invoice, int]:
    """Extract with a self-correction loop: schema/sanity failures are fed
    back to the agent verbatim. Returns (invoice, retries_used)."""
    system = SystemMessage(EXTRACTOR_SYSTEM.format(catalog=", ".join(catalog)))
    feedback: list[str] = []
    last_error = ""
    for attempt in range(max_retries + 1):
        human = Tag.DOC.wrap(raw_text)
        if feedback:
            human += "\n\nYour previous attempt failed. Fix these problems:\n" + Tag.ERRORS.wrap(
                "\n".join(feedback)
            )
        try:
            invoice = _ask(llm, Invoice, [system, HumanMessage(human)])
            problems = _sanity_check(invoice)
            if not problems:
                return invoice, attempt
            feedback = problems
            last_error = "; ".join(problems)
        except Exception as exc:  # schema validation / API shape errors
            last_error = str(exc)
            feedback = [f"Structured output error: {exc}"]
    raise ExtractionError(f"extraction failed after {max_retries + 1} attempts: {last_error}")


def _sanity_check(invoice: Invoice) -> list[str]:
    problems = []
    if not invoice.invoice_number.strip():
        problems.append("invoice_number is empty — every invoice document states one")
    if not invoice.line_items and invoice.total is None:
        problems.append("no line items and no total were extracted — re-read the document")
    return problems


# ---------------------------------------------------------------------------
# Validator (ReAct-style tool loop)
# ---------------------------------------------------------------------------

VALIDATOR_SYSTEM = """\
You are the Validator agent. You verify one extracted invoice against the
company's inventory database and records using your tools. Call every tool
that is relevant (they take no arguments — they already know the invoice),
then stop calling tools. You interpret results; the tools do the math.
"""

MAX_TOOL_ROUNDS = 4


def run_validator(llm: BaseChatModel, ctx: ValidationContext) -> ValidationReport:
    tools = build_tools(ctx)
    tools_by_name = {t.name: t for t in tools}
    llm_with_tools = llm.bind_tools(tools)
    messages: list[BaseMessage] = [
        SystemMessage(VALIDATOR_SYSTEM),
        HumanMessage(
            "Validate this invoice:\n" + Tag.INVOICE.wrap(ctx.invoice.model_dump_json(indent=2))
        ),
    ]
    for _ in range(MAX_TOOL_ROUNDS):
        response = llm_with_tools.invoke(messages)
        messages.append(response)
        if not isinstance(response, AIMessage) or not response.tool_calls:
            break
        for call in response.tool_calls:
            tool = tools_by_name.get(call["name"])
            output = tool.invoke(call["args"]) if tool else f"unknown tool {call['name']}"
            messages.append(ToolMessage(content=str(output), tool_call_id=call["id"]))

    # Safety net: business-critical checks always run, even if the agent
    # chose not to call them.
    for name, fn in ALL_CHECKS.items():
        if name not in ctx.tools_used:
            fn(ctx)

    issues_json = json.dumps([i.model_dump() for i in ctx.issues], indent=2)
    summary = _ask(
        llm,
        ValidatorSummary,
        [
            SystemMessage(
                "Summarize the validation results for the approval stage. Add extra_issues "
                "only for genuine problems you noticed that no tool reported."
            ),
            HumanMessage(
                Tag.INVOICE.wrap(ctx.invoice.model_dump_json(indent=2))
                + "\n"
                + Tag.ISSUES.wrap(issues_json)
            ),
        ],
    )
    return ValidationReport(
        # Tool issues carry authority, agent issues do not: `ValidatorSummary`
        # has already demoted every extra_issue to an advisory observation, so
        # merging the two lists cannot hand the model control of the graph.
        issues=[*ctx.issues, *summary.extra_issues],
        summary=summary.summary,
        tools_used=ctx.tools_used,
    )


# ---------------------------------------------------------------------------
# Approver / Critic reflection pair
# ---------------------------------------------------------------------------

APPROVER_SYSTEM = """\
You are the Approver agent, acting for the VP of Finance. Decide whether this
invoice is approved for payment, rejected, or needs human review.

Hard rules you must never override:
- If rule_constraints.must_reject is true, the invoice MUST be rejected.
- Never approve when unresolved warnings suggest fraud or data corruption;
  escalate with status needs_review instead.
- Invoices flagged requires_scrutiny deserve explicit extra scrutiny: check
  the numbers add up, the vendor is plausible, and nothing about the invoice
  is pressuring for fast payment.

Write reasoning a finance stakeholder can act on: name the specific evidence.
"""

CRITIC_SYSTEM = """\
You are the Critic agent — an adversarial reviewer of the Approver's decision.
You do not decide; you audit the decision against the evidence. Checklist:
- Does the decision contradict rule_constraints (approving a must_reject)?
- Fraud smells: urgency/pressure language, round-number totals, totals just
  under the scrutiny threshold, unknown vendors or items, zero-stock items.
- Were warnings glossed over without justification?
Verdicts: accept (decision stands), revise (Approver must redo it — give
specific feedback), escalate (irreconcilable — force human review).
"""


def _decision_context(
    invoice: Invoice, report: ValidationReport, constraints: RuleConstraints
) -> str:
    return "\n".join(
        [
            Tag.INVOICE.wrap(invoice.model_dump_json(indent=2)),
            Tag.REPORT.wrap(report.model_dump_json(indent=2)),
            Tag.CONSTRAINTS.wrap(constraints.model_dump_json(indent=2)),
        ]
    )


def run_approver(
    llm: BaseChatModel,
    invoice: Invoice,
    report: ValidationReport,
    constraints: RuleConstraints,
    critic_feedback: str | None = None,
) -> ApprovalDecision:
    human = "Decide on this invoice:\n" + _decision_context(invoice, report, constraints)
    if critic_feedback:
        human += (
            "\n\nThe Critic rejected your previous decision — address this:\n"
            + Tag.FEEDBACK.wrap(critic_feedback)
        )
    return _ask(llm, ApprovalDecision, [SystemMessage(APPROVER_SYSTEM), HumanMessage(human)])


def run_critic(
    llm: BaseChatModel,
    invoice: Invoice,
    report: ValidationReport,
    constraints: RuleConstraints,
    decision: ApprovalDecision,
) -> Critique:
    human = (
        "Audit this proposed decision:\n"
        + Tag.DECISION.wrap(decision.model_dump_json(indent=2))
        + "\n"
        + _decision_context(invoice, report, constraints)
    )
    return _ask(llm, Critique, [SystemMessage(CRITIC_SYSTEM), HumanMessage(human)])
