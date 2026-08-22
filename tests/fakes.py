"""Test doubles for the LLM.

FakeBrain is a drop-in BaseChatModel so tests exercise the real agent code
paths (tool loops, structured output, graph routing) without the network.

It does NOT parse invoices: extraction answers come from recorded ground-truth
fixtures (tests/fixtures/extractions/*.json), looked up by the exact document
text in the prompt. Approval/critique answers apply the same deterministic
policy the rule engine defines, read from the tagged blocks in the prompt.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable, RunnableLambda
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import Field

from invoiceflow.models import (
    ApprovalDecision,
    ApprovalStatus,
    Critique,
    CritiqueVerdict,
    Invoice,
    ValidatorSummary,
)
from invoiceflow.prompts import Tag


def _messages_text(messages: list[BaseMessage]) -> str:
    return "\n".join(m.content for m in messages if isinstance(m.content, str))


class FakeBrain(BaseChatModel):
    """Deterministic stand-in for Grok. `extractions` maps exact raw document
    text -> the ground-truth Invoice the real LLM is expected to produce."""

    extractions: dict[str, Invoice] = Field(default_factory=dict)

    @property
    def _llm_type(self) -> str:
        return "fake-brain"

    def bind_tools(self, tools: Any, **kwargs: Any) -> Runnable:
        formatted = [convert_to_openai_tool(t) for t in tools]
        return self.bind(tools=formatted, **kwargs)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        tools = kwargs.get("tools") or []
        has_tool_results = any(isinstance(m, ToolMessage) for m in messages)
        if tools and not has_tool_results:
            tool_calls = [
                {"name": t["function"]["name"], "args": {}, "id": f"call_{i}", "type": "tool_call"}
                for i, t in enumerate(tools)
            ]
            msg = AIMessage(content="Running all validation checks.", tool_calls=tool_calls)
        else:
            msg = AIMessage(content="All requested checks have been executed.")
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def with_structured_output(self, schema: Any, **kwargs: Any) -> Runnable:
        def respond(prompt_value: Any) -> Any:
            messages = (
                prompt_value.to_messages()
                if hasattr(prompt_value, "to_messages")
                else list(prompt_value)
            )
            text = _messages_text(messages)
            if schema is Invoice:
                return self._lookup_extraction(text)
            if schema is ValidatorSummary:
                return _fake_summary(text)
            if schema is ApprovalDecision:
                return _fake_decision(text)
            if schema is Critique:
                return _fake_critique(text)
            raise NotImplementedError(f"FakeBrain has no answer for schema {schema}")

        return RunnableLambda(respond)

    def _lookup_extraction(self, text: str) -> Invoice:
        doc = Tag.DOC.unwrap(text)
        if doc is None:
            raise ValueError("FakeBrain: no invoice document block in prompt")
        try:
            return self.extractions[doc]
        except KeyError:
            raise KeyError(
                "FakeBrain has no recorded extraction for this document; "
                "add a ground-truth fixture under tests/fixtures/extractions/"
            ) from None


def _fake_summary(text: str) -> ValidatorSummary:
    issues = json.loads(Tag.ISSUES.unwrap(text) or "[]")
    if not issues:
        return ValidatorSummary(summary="All checks passed; the invoice looks consistent.")
    crit = sum(1 for i in issues if i["severity"] == "critical")
    warn = sum(1 for i in issues if i["severity"] == "warning")
    return ValidatorSummary(
        summary=f"Validation found {crit} critical and {warn} warning issue(s): "
        + "; ".join(i["detail"] for i in issues)
    )


def _fake_decision(text: str) -> ApprovalDecision:
    constraints = json.loads(Tag.CONSTRAINTS.unwrap(text) or "{}")
    if constraints.get("must_review"):  # outranks must_reject
        return ApprovalDecision(
            status=ApprovalStatus.NEEDS_REVIEW,
            reasoning="Cannot be decided automatically: "
            + "; ".join(constraints.get("review_reasons", [])),
            risk_factors=constraints.get("review_reasons", []),
        )
    if constraints.get("must_reject"):
        return ApprovalDecision(
            status=ApprovalStatus.REJECTED,
            reasoning="Rejected due to critical validation failures: "
            + "; ".join(constraints.get("reject_reasons", [])),
            risk_factors=constraints.get("reject_reasons", []),
        )
    warnings = constraints.get("advisory_warnings", [])
    if warnings:
        return ApprovalDecision(
            status=ApprovalStatus.NEEDS_REVIEW,
            reasoning="Escalating for human review due to unresolved warnings: "
            + "; ".join(warnings),
            risk_factors=warnings,
        )
    scrutiny = constraints.get("scrutiny_reasons", [])
    reasoning = "All validation checks passed; approving for payment."
    if scrutiny:
        reasoning = (
            "High-value invoice given additional scrutiny (" + "; ".join(scrutiny) + "). "
            "No discrepancies found; approving for payment."
        )
    return ApprovalDecision(status=ApprovalStatus.APPROVED, reasoning=reasoning)


def _fake_critique(text: str) -> Critique:
    decision = json.loads(Tag.DECISION.unwrap(text) or "{}")
    constraints = json.loads(Tag.CONSTRAINTS.unwrap(text) or "{}")
    status = decision.get("status")
    if status != "needs_review" and constraints.get("must_review"):
        return Critique(
            verdict=CritiqueVerdict.REVISE,
            feedback="The rules forbid deciding this invoice automatically: "
            + "; ".join(constraints.get("review_reasons", [])),
        )
    if status == "approved" and constraints.get("must_reject"):
        return Critique(
            verdict=CritiqueVerdict.REVISE,
            feedback="The decision approves an invoice with critical validation failures: "
            + "; ".join(constraints.get("reject_reasons", [])),
        )
    if status == "approved" and constraints.get("advisory_warnings"):
        return Critique(
            verdict=CritiqueVerdict.REVISE,
            feedback="Unresolved warnings were not addressed: "
            + "; ".join(constraints["advisory_warnings"]),
        )
    if status == "rejected" and not constraints.get("must_reject"):
        return Critique(
            verdict=CritiqueVerdict.REVISE,
            feedback="Rejection is not supported by any critical validation failure.",
        )
    return Critique(
        verdict=CritiqueVerdict.ACCEPT,
        feedback="Decision is consistent with the validation evidence and business rules.",
    )
