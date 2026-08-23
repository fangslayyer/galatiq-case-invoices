"""The LangSmith tracing gate.

Tracing is development-only observability: switching it on ships prompts and
invoice text to LangSmith's cloud, which is the one thing "no external APIs
beyond Grok" rules out. These tests pin the gate shut by default and keep the
CLI's banner honest about when it is open.
"""

import os

import pytest

from invoiceflow.config import (
    LANGSMITH_DEFAULT_PROJECT,
    TRACING_ENV_VARS,
    langsmith_project,
    tracing_enabled,
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (*TRACING_ENV_VARS, "LANGSMITH_PROJECT"):
        monkeypatch.delenv(var, raising=False)


def test_tracing_is_off_without_env(clean_env) -> None:
    assert tracing_enabled() is False
    assert langsmith_project() is None


def test_test_suite_forces_every_tracing_var_off() -> None:
    """tests/__init__.py hard-codes its own list; catch it drifting from ours."""
    assert all(os.environ.get(var) == "false" for var in TRACING_ENV_VARS)
    assert tracing_enabled() is False


@pytest.mark.parametrize("var", TRACING_ENV_VARS)
def test_any_of_the_tracers_own_env_vars_opens_the_gate(
    clean_env, monkeypatch: pytest.MonkeyPatch, var: str
) -> None:
    """langsmith reads four names; a banner that watched only one would let a
    traced run look untraced."""
    monkeypatch.setenv(var, "true")
    assert tracing_enabled() is True
    assert langsmith_project() == LANGSMITH_DEFAULT_PROJECT


@pytest.mark.parametrize("value", ["false", "1", "yes", "True", ""])
def test_only_the_literal_true_enables_tracing(
    clean_env, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Exactly langsmith's own rule — anything else leaves the tracer asleep."""
    monkeypatch.setenv("LANGSMITH_TRACING", value)
    assert tracing_enabled() is False
    assert langsmith_project() is None


def test_higher_precedence_var_wins(clean_env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING_V2", "false")
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    assert tracing_enabled() is False


def test_explicit_project_is_respected(clean_env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_PROJECT", "invoiceflow-debug")
    assert langsmith_project() == "invoiceflow-debug"
