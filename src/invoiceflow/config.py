"""Application settings, loaded from environment variables / .env."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="INVOICEFLOW_", env_file=".env", extra="ignore")

    grok_model: str = "grok-3"
    xai_api_key: str = ""  # also read from bare XAI_API_KEY, see resolve_api_key

    db_path: Path = PROJECT_ROOT / "inventory.db"
    results_dir: Path = PROJECT_ROOT / "results"

    # Business rules
    scrutiny_threshold: float = 10_000.0  # invoices above this get extra scrutiny
    expected_currency: str = "USD"
    max_extraction_retries: int = 2
    max_critique_rounds: int = 2

    def resolve_api_key(self) -> str:
        import os

        return self.xai_api_key or os.environ.get("XAI_API_KEY", "")


def get_settings() -> Settings:
    return Settings()
