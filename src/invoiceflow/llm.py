"""LLM backends: real Grok via langchain-xai, or a deterministic offline stub.

The stub is a drop-in BaseChatModel so every agent runs the exact same code
path (tool-calling loops, structured output) with or without an API key. It
answers by parsing the tagged context blocks the agents put in their own
prompts — deterministic, offline, and free.
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

from . import prompts
from .config import Settings
from .models import (
    ApprovalDecision,
    ApprovalStatus,
    Critique,
    CritiqueVerdict,
    Invoice,
    ValidatorSummary,
)


def build_llm(settings: Settings) -> BaseChatModel:
    backend = settings.resolve_backend()
    if backend == "grok":
        from langchain_xai import ChatXAI

        return ChatXAI(
            model=settings.grok_model,
            api_key=settings.resolve_api_key(),
            temperature=0,
        )
    if backend == "stub":
        return StubChatModel()
    raise ValueError(f"Unknown LLM backend: {backend!r} (expected 'grok', 'stub', or 'auto')")


# ---------------------------------------------------------------------------
# Offline stub
# ---------------------------------------------------------------------------


def _messages_text(messages: list[BaseMessage]) -> str:
    parts = []
    for m in messages:
        if isinstance(m.content, str):
            parts.append(m.content)
    return "\n".join(parts)


class StubChatModel(BaseChatModel):
    """Deterministic offline brain.

    Tool-calling: calls every bound tool once, then acknowledges the results.
    Structured output: parses the tagged blocks in the prompt and applies the
    same business policy the rule engine defines.
    """

    @property
    def _llm_type(self) -> str:
        return "invoiceflow-stub"

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
                {
                    "name": t["function"]["name"],
                    "args": {},
                    "id": f"call_{i}",
                    "type": "tool_call",
                }
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
                return _stub_extract(text)
            if schema is ValidatorSummary:
                return _stub_summarize(text)
            if schema is ApprovalDecision:
                return _stub_decide(text)
            if schema is Critique:
                return _stub_critique(text)
            raise NotImplementedError(f"Stub has no policy for schema {schema}")

        return RunnableLambda(respond)


def _stub_extract(text: str) -> Invoice:
    from .offline import extract_invoice

    doc = prompts.extract_block(text, prompts.DOC_TAG)
    if doc is None:
        raise ValueError("stub extractor: no invoice document block in prompt")
    return extract_invoice(doc)


def _stub_summarize(text: str) -> ValidatorSummary:
    issues = json.loads(prompts.extract_block(text, prompts.ISSUES_TAG) or "[]")
    if not issues:
        return ValidatorSummary(summary="All checks passed; the invoice looks consistent.")
    crit = sum(1 for i in issues if i["severity"] == "critical")
    warn = sum(1 for i in issues if i["severity"] == "warning")
    return ValidatorSummary(
        summary=f"Validation found {crit} critical and {warn} warning issue(s): "
        + "; ".join(i["detail"] for i in issues)
    )


def _stub_decide(text: str) -> ApprovalDecision:
    constraints = json.loads(prompts.extract_block(text, prompts.CONSTRAINTS_TAG) or "{}")
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


def _stub_critique(text: str) -> Critique:
    decision = json.loads(prompts.extract_block(text, prompts.DECISION_TAG) or "{}")
    constraints = json.loads(prompts.extract_block(text, prompts.CONSTRAINTS_TAG) or "{}")
    status = decision.get("status")
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
