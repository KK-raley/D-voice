"""Agent connector abstractions: statuses, task records, and the base class."""

from __future__ import annotations

import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Callable

from vocalis.server.events import EventBus, EventType, bus


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


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


class AgentConnector(ABC):
    """Base class for all agent integrations.

    Subclasses implement :meth:`stream_run`, yielding progress updates that
    the registry turns into task records and bus events. Connectors should
    keep the yielded strings human-friendly: JARVIS speaks them aloud.
    """

    name: str = "base"
    description: str = "abstract agent"
    capabilities: list[str] = []

    def __init__(self, event_bus: EventBus | None = None) -> None:
        self.bus = event_bus or bus
        self.status: AgentStatus = AgentStatus.IDLE
        self.active_tasks: dict[str, TaskRecord] = {}

    # ------------------------------------------------------------------
    async def run(self, instruction: str, **kwargs: Any) -> TaskRecord:
        """Execute an instruction end-to-end, emitting lifecycle events."""
        record = TaskRecord(agent=self.name, instruction=instruction)
        self.active_tasks[record.id] = record
        self.status = AgentStatus.BUSY

        record.status = TaskStatus.RUNNING
        record.started_at = time.time()
        await self.bus.publish(EventType.TASK_STARTED, **record.to_dict())

        try:
            async for update in self.stream_run(instruction, record, **kwargs):
                if isinstance(update, float):
                    record.progress = min(1.0, max(record.progress, update))
                elif isinstance(update, str):
                    record.current_step = update
                await self.bus.publish(EventType.TASK_PROGRESS, **record.to_dict())
            record.progress = 1.0
            record.status = TaskStatus.COMPLETED
            await self.bus.publish(EventType.TASK_COMPLETED, **record.to_dict())
        except Exception as e:
            record.status = TaskStatus.FAILED
            record.error = str(e)
            await self.bus.publish(EventType.TASK_FAILED, **record.to_dict())
            self.status = AgentStatus.ERROR
        finally:
            record.finished_at = time.time()
            self.active_tasks.pop(record.id, None)
            if self.status != AgentStatus.ERROR:
                self.status = AgentStatus.IDLE
            await self.bus.publish(
                EventType.AGENT_STATUS, agent=self.name, status=self.status.value
            )
        return record

    @abstractmethod
    def stream_run(
        self, instruction: str, record: TaskRecord, **kwargs: Any
    ) -> AsyncIterator[float | str]:
        """Yield progress floats (0..1) and/or human-readable step strings."""
        raise NotImplementedError
        yield  # pragma: no cover

    async def health_check(self) -> bool:
        return True
