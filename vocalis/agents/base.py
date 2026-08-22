"""Agent connector abstractions: statuses, task records, and the base class."""

from __future__ import annotations

import asyncio
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from vocalis.agents.resilience import CircuitBreaker, retry_async
from vocalis.server.events import EventBus, EventType, bus


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AgentStatus(str, Enum):
    IDLE = "idle"
    BUSY = "busy"
    ERROR = "error"
    OFFLINE = "offline"


@dataclass
class TaskRecord:
    """One unit of work dispatched to an agent."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    agent: str = "echo"
    instruction: str = ""
    status: TaskStatus = TaskStatus.QUEUED
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    progress: float = 0.0  # 0..1
    current_step: str = ""
    output: str = ""
    error: str | None = None
    artifacts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "agent": self.agent,
            "instruction": self.instruction,
            "status": self.status.value,
            "createdAt": self.created_at,
            "elapsed": (self.finished_at or time.time()) - self.created_at,
            "progress": round(self.progress, 3),
            "currentStep": self.current_step,
            "output": self.output,
            "error": self.error,
        }


@dataclass
class ConnectorHealth:
    """Rolling health snapshot for a connector (track D2).

    Updated by :meth:`AgentConnector.run` on every dispatch outcome; exposed
    via :meth:`AgentConnector.health_dict` for the HUD/registry.
    """

    last_error: str | None = None
    last_latency_ms: float | None = None
    last_success_ts: float | None = None
    consecutive_failures: int = 0
    total_runs: int = 0
    total_failures: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "last_error": self.last_error,
            "last_latency_ms": (
                round(self.last_latency_ms, 1)
                if self.last_latency_ms is not None
                else None
            ),
            "last_success_ts": self.last_success_ts,
            "consecutive_failures": self.consecutive_failures,
            "total_runs": self.total_runs,
            "total_failures": self.total_failures,
        }


class AgentConnector(ABC):
    """Base class for all agent integrations.

    Subclasses implement :meth:`stream_run`, yielding progress updates that
    become task records and bus events. Connectors should keep the yielded
    strings human-friendly: D-VOICE speaks them aloud.

    Resilience (track D3) is opt-in via two class attributes:

    * ``retry_attempts`` - total tries per dispatch (1 = never retry).
    * ``circuit_breaker`` - optional :class:`CircuitBreaker` wrapping the
      whole stream consumption, so a flaky connector fails fast while open.

    Both default to "off" so existing connectors (echo, openai, ...) behave
    exactly as before.
    """

    name: str = "base"
    description: str = "abstract agent"
    capabilities: tuple[str, ...] = ()

    retry_attempts: int = 1
    circuit_breaker: CircuitBreaker | None = None

    def __init__(self, event_bus: EventBus | None = None) -> None:
        self.bus = event_bus or bus
        self.status: AgentStatus = AgentStatus.IDLE
        self.active_tasks: dict[str, TaskRecord] = {}
        self.health = ConnectorHealth()

    # ------------------------------------------------------------------
    async def run(
        self, instruction: str, record: TaskRecord | None = None, **kwargs: Any
    ) -> TaskRecord:
        """Execute an instruction end-to-end, emitting lifecycle events.

        An externally-created ``record`` (e.g. by the registry, so the
        queued/started/progress events all share one task id) may be passed
        in; otherwise a fresh one is created here.

        Cancellation (track D4): ``asyncio.CancelledError`` marks the record
        as ``CANCELLED`` (error="cancelled"), emits ``task.failed`` so the
        HUD keeps working, cleans up ``active_tasks``, then re-raises so the
        caller's task is cancelled as expected. Cancellation is not counted
        against connector health or any circuit breaker.
        """
        record = record or TaskRecord(agent=self.name, instruction=instruction)
        record.instruction = instruction or record.instruction
        self.active_tasks[record.id] = record
        self.status = AgentStatus.BUSY

        record.status = TaskStatus.RUNNING
        record.started_at = time.time()
        await self.bus.publish(EventType.TASK_STARTED, **record.to_dict())

        started = time.time()
        try:
            await self._execute(instruction, record, **kwargs)
            self._update_health(elapsed_s=time.time() - started)
            record.progress = 1.0
            record.status = TaskStatus.COMPLETED
            await self.bus.publish(EventType.TASK_COMPLETED, **record.to_dict())
        except asyncio.CancelledError:
            record.status = TaskStatus.CANCELLED
            record.error = "cancelled"
            self._update_health(elapsed_s=time.time() - started, cancelled=True)
            await self.bus.publish(EventType.TASK_FAILED, **record.to_dict())
            raise
        except Exception as e:
            record.status = TaskStatus.FAILED
            record.error = str(e)
            self._update_health(elapsed_s=time.time() - started, error=e)
            await self.bus.publish(EventType.TASK_FAILED, **record.to_dict())
            self.status = AgentStatus.ERROR
        finally:
            record.finished_at = time.time()
            self.active_tasks.pop(record.id, None)
            # Recover from ERROR on the next successful dispatch (no TTL yet).
            if self.status == AgentStatus.ERROR and record.status is TaskStatus.COMPLETED:
                self.status = AgentStatus.IDLE
            elif self.status != AgentStatus.ERROR:
                self.status = AgentStatus.IDLE
            await self.bus.publish(
                EventType.AGENT_STATUS,
                agent=self.name,
                status=self.status.value,
                health=self.health_dict(),
            )
        return record

    async def _execute(
        self, instruction: str, record: TaskRecord, **kwargs: Any
    ) -> None:
        """Consume ``stream_run`` under the configured resilience policy.

        Retry wraps the stream consumption, and the circuit breaker wraps
        the whole retry cycle: one dispatch (all its attempts together)
        counts as a single breaker failure. The stream generator is closed
        explicitly via :func:`contextlib.aclosing` so ``finally`` blocks
        inside generators (e.g. CLI child-process teardown) run even when
        cancellation lands while the generator is parked at a ``yield``.
        """

        async def _consume() -> None:
            async with aclosing(self.stream_run(instruction, record, **kwargs)) as stream:
                async for update in stream:
                    if isinstance(update, float):
                        record.progress = min(1.0, max(record.progress, update))
                    elif isinstance(update, str):
                        record.current_step = update
                    await self.bus.publish(EventType.TASK_PROGRESS, **record.to_dict())

        async def _with_retry() -> None:
            await retry_async(_consume, attempts=self.retry_attempts)

        if self.circuit_breaker is not None:
            await self.circuit_breaker.call(_with_retry)
        else:
            await _with_retry()

    # -- health (track D2) ------------------------------------------------
    def _update_health(
        self,
        *,
        elapsed_s: float,
        error: BaseException | None = None,
        cancelled: bool = False,
    ) -> None:
        """Fold one dispatch outcome into the rolling health snapshot."""
        self.health.total_runs += 1
        self.health.last_latency_ms = elapsed_s * 1000.0
        if cancelled:
            return  # user-initiated abort: not a connector failure
        if error is None:
            self.health.last_error = None
            self.health.last_success_ts = time.time()
            self.health.consecutive_failures = 0
        else:
            self.health.last_error = str(error) or error.__class__.__name__
            self.health.consecutive_failures += 1
            self.health.total_failures += 1

    def health_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly snapshot of connector health."""
        return self.health.to_dict()

    @abstractmethod
    def stream_run(
        self, instruction: str, record: TaskRecord, **kwargs: Any
    ) -> AsyncIterator[float | str]:
        """Yield progress floats (0..1) and/or human-readable step strings."""
        raise NotImplementedError
        yield  # pragma: no cover

    async def health_check(self) -> bool:
        return True
