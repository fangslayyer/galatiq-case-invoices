"""Application settings, loaded from environment variables / .env."""

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Materialise .env into the real process environment. Settings could parse the
# file by itself, but the unprefixed keys have to be genuinely exported: xAI's
# SDK and LangSmith's tracer both read os.environ directly. Shell variables
# already set win — load_dotenv never overrides them.
load_dotenv(PROJECT_ROOT / ".env")

LANGSMITH_DEFAULT_PROJECT = "invoiceflow"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="INVOICEFLOW_", env_file=".env", extra="ignore")

    grok_model: str = "grok-4.6"
    # Also accept the bare XAI_API_KEY: that is xAI's own conventional name,
    # and what .env.example tells you to set.
    xai_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("INVOICEFLOW_XAI_API_KEY", "XAI_API_KEY"),
    )

    db_path: Path = PROJECT_ROOT / "inventory.db"
    results_dir: Path = PROJECT_ROOT / "results"

    # Business rules
    scrutiny_threshold: float = 10_000.0  # invoices above this get extra scrutiny
    expected_currency: str = "USD"
    max_extraction_retries: int = 2
    max_critique_rounds: int = 2

    def resolve_api_key(self) -> str:
        return self.xai_api_key


def get_settings() -> Settings:
    return Settings()


def langsmith_project() -> str | None:
    """The LangSmith project runs are traced to, or None when tracing is off.

    Development-only observability: turning it on ships prompts and invoice
    text to LangSmith's cloud, so it stays opt-in behind LANGSMITH_TRACING=true
    and is forced off in the test suite (see tests/__init__.py).
    """
    if os.environ.get("LANGSMITH_TRACING", "").strip().lower() != "true":
        return None
    return os.environ.setdefault("LANGSMITH_PROJECT", LANGSMITH_DEFAULT_PROJECT)
