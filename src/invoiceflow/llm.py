"""LLM factory: xAI Grok is the single reasoning engine.

There is deliberately no rule-based fallback parser here — understanding
messy invoice documents is the LLM's job. "Offline" in the case brief means
no external non-Grok APIs (payment and inventory are mocked locally), not a
hand-rolled backup brain. Tests inject a fake chat model instead.
"""

from __future__ import annotations

from langchain_core.language_models import BaseChatModel

from .config import Settings


class MissingApiKeyError(RuntimeError):
    pass


def build_llm(settings: Settings) -> BaseChatModel:
    api_key = settings.resolve_api_key()
    if not api_key:
        raise MissingApiKeyError(
            "XAI_API_KEY is not set. Export it or put it in .env — the pipeline's "
            "reasoning engine is xAI Grok and there is no non-LLM fallback."
        )
    from langchain_xai import ChatXAI

    return ChatXAI(model=settings.grok_model, api_key=api_key, temperature=0)
