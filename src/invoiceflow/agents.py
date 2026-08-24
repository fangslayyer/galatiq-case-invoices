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
from langchain_core.tools import BaseTool
from pydantic import BaseModel

from .models import ApprovalDecision, Critique, Invoice, ValidationReport, ValidatorSummary
from .prompts import Tag
from .rules import RuleConstraints
from .validation import ALL_CHECKS, ValidationContext, build_tools


class ExtractionError(RuntimeError):
    """Raised when the Extractor cannot produce a usable invoice after retries.

    `attempts` carries the feedback each failed attempt was given — the same
    strings a successful run returns — so even a dead run keeps its
    self-correction evidence (extraction_attempts in the run store).
    """

    def __init__(self, message: str, attempts: list[str] | None = None):
        super().__init__(message)
        self.attempts = attempts or []


def _tool_loop(
    llm: BaseChatModel,
    tools: list[BaseTool],
    messages: list[BaseMessage],
    max_rounds: int,
) -> list[str]:
    """Let the model call `tools` until it stops, extending `messages` in place.

    Hand-rolled rather than delegated to an agent executor so that every step is
    visible and testable — and shared by the two agents that use tools so the
    Approver's loop cannot quietly diverge from the Validator's.

    Returns the tool names actually called, in order. The Validator does not need
    that (each check records itself), but the Approver does: whether it chose to
    consult precedent is exactly the measure of whether offering the tool was
    worth the round-trip.
    """
    tools_by_name = {t.name: t for t in tools}
    llm_with_tools = llm.bind_tools(tools)
    called: list[str] = []
    for _ in range(max_rounds):
        response = llm_with_tools.invoke(messages)
        messages.append(response)
        if not isinstance(response, AIMessage) or not response.tool_calls:
            break
        for call in response.tool_calls:
            tool = tools_by_name.get(call["name"])
            output = tool.invoke(call["args"]) if tool else f"unknown tool {call['name']}"
            called.append(call["name"])
            messages.append(ToolMessage(content=str(output), tool_call_id=call["id"]))
    return called


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
) -> tuple[Invoice, list[str]]:
    """Extract with a self-correction loop: schema/sanity failures are fed
    back to the agent verbatim.

    Returns (invoice, attempts): one entry per *failed* attempt, holding the
    feedback that went into the next prompt — the evidence the run store keeps
    as extraction_attempts. An empty list is a clean first pass.
    """
    system = SystemMessage(EXTRACTOR_SYSTEM.format(catalog=", ".join(catalog)))
    attempts: list[str] = []
    feedback: list[str] = []
    for _ in range(max_retries + 1):
        human = Tag.DOC.wrap(raw_text)
        if feedback:
            human += "\n\nYour previous attempt failed. Fix these problems:\n" + Tag.ERRORS.wrap(
                "\n".join(feedback)
            )
        try:
            invoice = _ask(llm, Invoice, [system, HumanMessage(human)])
            problems = _sanity_check(invoice)
            if not problems:
                return invoice, attempts
            feedback = problems
        except Exception as exc:  # schema validation / API shape errors
            feedback = [f"Structured output error: {exc}"]
        attempts.append("; ".join(feedback))
    raise ExtractionError(
        f"extraction failed after {max_retries + 1} attempts: {attempts[-1]}", attempts
    )


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
    messages: list[BaseMessage] = [
        SystemMessage(VALIDATOR_SYSTEM),
        HumanMessage(
            "Validate this invoice:\n" + Tag.INVOICE.wrap(ctx.invoice.model_dump_json(indent=2))
        ),
    ]
    # The names come back but are ignored: each check records itself into
    # ctx.tools_used, which is what `safety_net` below is computed against.
    _tool_loop(llm, build_tools(ctx), messages, MAX_TOOL_ROUNDS)

    # Safety net: business-critical checks always run, even if the agent
    # chose not to call them. Which ones it skipped is recorded — that gap is
    # the honest measure of the tool loop (validation_tool_runs.invoked_by).
    safety_net = [name for name in ALL_CHECKS if name not in ctx.tools_used]
    for name in safety_net:
        ALL_CHECKS[name](ctx)

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
        safety_net_tools=safety_net,
    )


# ---------------------------------------------------------------------------
# Approver / Critic reflection pair
# ---------------------------------------------------------------------------

APPROVER_SYSTEM = """\
You are the Approver agent, acting for the VP of Finance. Decide whether this
invoice is approved for payment, rejected, or needs human review.

Hard rules you must never override:
- If rule_constraints.must_review is true, the invoice MUST get needs_review —
  even if must_reject is also true. Something the decision depends on could not
  be established, so it is not yours to approve or reject; a person confirms it.
- Otherwise, if rule_constraints.must_reject is true, the invoice MUST be
  rejected.
- Never approve when unresolved warnings suggest fraud or data corruption;
  escalate with status needs_review instead.
- Invoices flagged requires_scrutiny deserve explicit extra scrutiny: check
  the numbers add up, the vendor is plausible, and nothing about the invoice
  is pressuring for fast payment.

rule_constraints is a floor, not a verdict. must_reject and must_review being
false means no hard rule *caught* this invoice — never that it is safe, and
never on its own a reason to approve. Approval is your own affirmative finding
and you must reach it from the evidence:

- A warning is discharged by evidence, not by a story that would explain it.
  "Probably a new SKU", "likely a rounding artifact", "purchasing can sort it
  out later" are guesses. If the invoice, the validation report and the
  inventory catalog together cannot settle the point, it is unsettled, and an
  unsettled point on an irreversible payment belongs to a human.
- Naming a risk in risk_factors is not resolving it. Any risk still live when
  you stop reasoning decides the outcome — it does not merely accompany it.

Write reasoning a finance stakeholder can act on: name the specific evidence.
"""

#: Appended to APPROVER_SYSTEM only when precedent is on the table, so an invoice
#: history has nothing to say about is judged on a prompt byte-identical to the
#: one this pipeline used before precedent existed.
APPROVER_PRECEDENT = """
Prior human decisions are evidence — the one kind that can settle a question the
documents themselves cannot. Your tool find_similar_invoices returns how people
decided invoices raising these same findings from this same vendor: the amounts,
the dates, and the reviewers' own notes.

- rule_constraints.precedent_discharged lists findings a run of human decisions
  has already answered, naming the invoices they were answered on. Those are
  discharged by evidence, which is exactly the standard above — treat them as
  settled and do not re-open them. If a citation looks wrong to you, call the
  tool and read the cases before you act on that doubt.
- A finding NOT in that list is not settled, however many cases the tool returns.
  One prior approval is worth knowing when you are choosing between rejecting an
  invoice and escalating it; it is never on its own a reason to approve one.
- History is about this vendor's habits. It says nothing about whether the sums
  on this particular invoice are right, and cannot discharge anything else.
"""

CRITIC_SYSTEM = """\
You are the Critic agent — an adversarial reviewer of the Approver's decision.
You do not decide; you audit the decision against the evidence.

Audit against the evidence, not against rule_constraints. The Approver has
already read those constraints, so re-deriving its conclusion from them is not
a second opinion — "no hard rule fired, so approval is allowed" is the
beginning of your job, not the end of it. Checklist:
- Does the decision contradict rule_constraints (approving a must_reject)?
- Fraud smells: urgency/pressure language, round-number totals, totals just
  under the scrutiny threshold, unknown vendors or items, zero-stock items.
- Were warnings glossed over without justification? A warning the Approver
  named and then set aside with a benign guess was glossed over: check that
  each one was answered with evidence, not with a plausible explanation.
Verdicts: affirm (the Approver's decision stands), revise (Approver must redo
it — give specific feedback), escalate (irreconcilable — force human review).
Every verdict judges the decision, not the invoice: affirming a rejection says
the rejection was right, not that the invoice is sound.
"""

#: Appended to CRITIC_SYSTEM only when the Approver was given precedent, so the
#: audit is against the same evidence and never against an empty block.
CRITIC_PRECEDENT = """
This decision was made with access to prior human decisions, in the <precedent>
block below. Audit any use of them:
- A citation is evidence only if the block actually holds those cases. One that
  names invoices the block does not show is a story with numbers in it.
- Findings listed in rule_constraints.precedent_discharged are settled, and an
  Approver that re-opened one is wrong in the opposite direction — say so.
- A single prior approval is not a rule. If approval rests on one case and
  nothing else, that is glossing over the finding, not answering it.
"""


#: One round is enough for a tool that takes no arguments and answers in full;
#: the second exists only so a model that calls it twice is not truncated.
MAX_APPROVER_TOOL_ROUNDS = 2


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
    tools: list[BaseTool] | None = None,
) -> tuple[ApprovalDecision, list[str]]:
    """Decide one invoice. Returns (decision, tools the agent chose to call).

    `tools` is empty for most invoices — the graph offers precedent only where
    history could actually answer something — and when it is, this takes exactly
    the path it took before precedent existed: one structured-output call, no
    bound schema, no extra round-trip, and the unextended system prompt.

    There is deliberately no safety net here, unlike the Validator's. A skipped
    check leaves the pipeline blind about the invoice; a skipped precedent lookup
    leaves it blind about nothing, because the rule engine has already read the
    same history and acted on it. Forcing the block in would spend tokens on
    every flagged invoice to tell the model something its constraints already say.
    """
    human = "Decide on this invoice:\n" + _decision_context(invoice, report, constraints)
    if critic_feedback:
        human += (
            "\n\nThe Critic rejected your previous decision — address this:\n"
            + Tag.FEEDBACK.wrap(critic_feedback)
        )
    system = APPROVER_SYSTEM + (APPROVER_PRECEDENT if tools else "")
    messages: list[BaseMessage] = [SystemMessage(system), HumanMessage(human)]
    consulted = _tool_loop(llm, tools, messages, MAX_APPROVER_TOOL_ROUNDS) if tools else []
    # The whole transcript, not a fresh conversation: whatever the tool returned
    # is the evidence the decision has to be made on, and starting over would
    # throw it away between looking and deciding.
    return _ask(llm, ApprovalDecision, messages), consulted


def run_critic(
    llm: BaseChatModel,
    invoice: Invoice,
    report: ValidationReport,
    constraints: RuleConstraints,
    decision: ApprovalDecision,
    precedent: str = "",
) -> Critique:
    """Audit one decision. `precedent` is the block the Approver was given — the
    same evidence, so the audit can check a citation rather than take it."""
    human = (
        "Audit this proposed decision:\n"
        + Tag.DECISION.wrap(decision.model_dump_json(indent=2))
        + "\n"
        + _decision_context(invoice, report, constraints)
        + (f"\n{precedent}" if precedent else "")
    )
    system = CRITIC_SYSTEM + (CRITIC_PRECEDENT if precedent else "")
    return _ask(llm, Critique, [SystemMessage(system), HumanMessage(human)])
