"""Lightweight asyncio event bus powering real-time status reporting.

Every part of the system (VoiceGate, agents, monitor, Jarvis brain) publishes
events here; the FastAPI server relays them to the HUD over WebSocket and the
monitor/notifier subscribes for watchdog + notification logic.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable

Handler = Callable[["Event"], Any]


class EventType(str, Enum):
    # Voice pipeline
    VOICE_DETECTED = "voice.detected"
    VOICE_ACCEPTED = "voice.accepted"
    VOICE_REJECTED = "voice.rejected"
    ASR_PARTIAL = "asr.partial"
    ASR_FINAL = "asr.final"
    TTS_SPEAKING = "tts.speaking"

    # Agent lifecycle
    TASK_QUEUED = "task.queued"
    TASK_STARTED = "task.started"
    TASK_PROGRESS = "task.progress"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    AGENT_STATUS = "agent.status"

    # Jarvis brain
    JARVIS_SAYING = "jarvis.saying"
    JARVIS_COMMAND = "jarvis.command"
    MONITOR_ALERT = "monitor.alert"

    # System
    SYSTEM = "system"


@dataclass
class Event:
    type: EventType
    data: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "ts": self.ts, "type": self.type.value, "data": self.data}


class EventBus:
    """Fan-out pub/sub with asyncio queues (drop-safe for slow consumers)."""

    def __init__(self, history_size: int = 256) -> None:
        self._subscribers: dict[str, list[asyncio.Queue[Event]]] = defaultdict(list)
        self._handlers: dict[str, list[Handler]] = defaultdict(list)
        self.history: list[Event] = []
        self._history_size = history_size

    # -- subscribe -----------------------------------------------------
    def subscribe(self, pattern: str = "*") -> asyncio.Queue[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=512)
        self._subscribers[pattern].append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[Event]) -> None:
        for pattern in list(self._subscribers):
            queues = self._subscribers[pattern]
            if q in queues:
                queues.remove(q)
            if not queues:
                del self._subscribers[pattern]

    def on(self, pattern: str, handler: Handler) -> None:
        self._handlers[pattern].append(handler)

    # -- publish -------------------------------------------------------
    async def publish(self, type_: EventType, **data: Any) -> Event:
        event = Event(type=type_, data=data)
        self.history.append(event)
        if len(self.history) > self._history_size:
            self.history = self.history[-self._history_size :]

        for pattern, queues in self._subscribers.items():
            if self._match(pattern, event.type.value):
                for q in queues:
                    try:
                        q.put_nowait(event)
                    except asyncio.QueueFull:  # slow consumer: drop oldest
                        try:
                            q.get_nowait()
                            q.put_nowait(event)
                        except Exception:
                            pass

        for pattern, handlers in self._handlers.items():
            if self._match(pattern, event.type.value):
                for h in handlers:
                    result = h(event)
                    if asyncio.iscoroutine(result):
                        asyncio.create_task(result)  # noqa: RUF006
        return event

    def publish_sync(self, type_: EventType, **data: Any) -> Event:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop and loop.is_running():
            return loop.run_until_complete(self.publish(type_, **data))
        return asyncio.run(self.publish(type_, **data))

    @staticmethod
    def _match(pattern: str, value: str) -> bool:
        if pattern == "*":
            return True
        if pattern.endswith(".*"):
            return value.startswith(pattern[:-1])
        return pattern == value


# Singleton used across the process.
bus = EventBus()
