"""Per-run observability: agent turns and the LLM round-trips inside them.

`RunRecorder` is a plain in-memory collector created per pipeline run. Graph
nodes open a turn around each agent call; a `TelemetryHandler` registered on
the graph's `config` sees every chat-model round-trip (LangChain propagates
callbacks into the nodes via contextvars) and files it under the currently
open turn. The pipeline is single-threaded, so "current turn" is unambiguous.

Everything here is buffered, not written: `RunStore.finish_run` persists the
whole recorder in one transaction at the end of the run.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class LLMCall(BaseModel):
    """One round-trip to the model, as the llm_calls table stores it."""

    seq: int
    model: str = ""
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    tool_calls: int = 0
    latency_ms: int | None = None
    started_at: str = ""
    error: str = ""
    langsmith_run_id: str | None = None


class Turn(BaseModel):
    """One agent's turn at one node — the agent_invocations grain."""

    seq: int
    node: str
    agent: str
    round_no: int = 1
    triggered_by_seq: int | None = None
    outcome: str = "ok"  # ok | retried | failed
    error: str = ""
    started_at: str = ""
    duration_ms: int | None = None
    calls: list[LLMCall] = Field(default_factory=list)


class RunRecorder:
    """Collects the turns of one run. Single-threaded by construction."""

    def __init__(self) -> None:
        self.turns: list[Turn] = []
        self.current: Turn | None = None

    def last_seq(self, agent: str) -> int | None:
        for turn in reversed(self.turns):
            if turn.agent == agent:
                return turn.seq
        return None

    @contextmanager
    def turn(self, node: str, agent: str, *, round_no: int = 1, triggered_by: int | None = None):
        """Open a turn around an agent call; closes it whatever happens.

        An exception marks the turn failed and propagates — recording must
        never swallow the pipeline's own errors.
        """
        t = Turn(
            seq=len(self.turns) + 1,
            node=node,
            agent=agent,
            round_no=round_no,
            triggered_by_seq=triggered_by,
            started_at=_now(),
        )
        self.turns.append(t)
        self.current = t
        t0 = time.monotonic()
        try:
            yield t
        except Exception as exc:
            t.outcome = "failed"
            t.error = str(exc)
            raise
        finally:
            t.duration_ms = int((time.monotonic() - t0) * 1000)
            self.current = None

    def record_call(self, call: LLMCall) -> None:
        if self.current is None:
            # A model call outside any turn: nothing to attribute it to. This
            # only happens when the graph is driven without a recorder-aware
            # caller; dropping beats guessing.
            log.debug("LLM call outside any turn — dropped from telemetry")
            return
        call.seq = len(self.current.calls) + 1
        self.current.calls.append(call)


class TelemetryHandler(BaseCallbackHandler):
    """Files every chat-model round-trip under the recorder's open turn.

    Registered once per run on the graph's config; LangChain's contextvar
    propagation carries it into every `llm.invoke` the agents make, so the
    agent code needs no changes and LangSmith tracing is unaffected.
    """

    def __init__(self, recorder: RunRecorder, default_model: str = "") -> None:
        self.recorder = recorder
        self.default_model = default_model
        self._starts: dict[UUID, tuple[float, str, str]] = {}  # run_id -> (t0, iso, model)

    def on_chat_model_start(  # type: ignore[override]
        self,
        serialized: dict[str, Any],
        messages: Any,
        *,
        run_id: UUID,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        model = (metadata or {}).get("ls_model_name") or self.default_model
        self._starts[run_id] = (time.monotonic(), _now(), model)

    def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any) -> None:
        t0, started_at, model = self._starts.pop(run_id, (None, _now(), self.default_model))
        call = LLMCall(
            seq=0,
            model=model,
            started_at=started_at,
            langsmith_run_id=str(run_id),
            latency_ms=None if t0 is None else int((time.monotonic() - t0) * 1000),
        )
        for gens in response.generations:
            for gen in gens:
                message = getattr(gen, "message", None)
                usage = getattr(message, "usage_metadata", None)
                if usage:
                    call.input_tokens += usage.get("input_tokens", 0)
                    call.output_tokens += usage.get("output_tokens", 0)
                    call.total_tokens += usage.get("total_tokens", 0)
                    call.cached_input_tokens += (usage.get("input_token_details") or {}).get(
                        "cache_read", 0
                    )
                    call.reasoning_tokens += (usage.get("output_token_details") or {}).get(
                        "reasoning", 0
                    )
                if message is not None:
                    call.tool_calls += len(getattr(message, "tool_calls", None) or [])
                    name = (getattr(message, "response_metadata", None) or {}).get("model_name")
                    if name:
                        call.model = name
        self.recorder.record_call(call)

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        t0, started_at, model = self._starts.pop(run_id, (None, _now(), self.default_model))
        self.recorder.record_call(
            LLMCall(
                seq=0,
                model=model,
                started_at=started_at,
                error=str(error),
                langsmith_run_id=str(run_id),
                latency_ms=None if t0 is None else int((time.monotonic() - t0) * 1000),
            )
        )
