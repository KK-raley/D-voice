"""Lightweight asyncio event bus powering real-time status reporting.

Every part of the system (VoiceGate, agents, monitor, D-VOICE brain)
publishes events here; the FastAPI server relays them to the HUD over
WebSocket and the monitor/notifier subscribes for watchdog + notification
logic.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger("vocalis.events")

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

    # D-VOICE brain
    DVOICE_SAYING = "dvoice.saying"
    DVOICE_COMMAND = "dvoice.command"
    MONITOR_ALERT = "monitor.alert"

    # Command receipts + high-risk confirmation (P0 trust UX)
    COMMAND_RECEIPT = "command.receipt"
    CONFIRM_REQUESTED = "confirm.requested"
    CONFIRM_RESOLVED = "confirm.resolved"

    # Vision (screen monitoring independent of agent reports)
    VISION_SCREEN = "vision.screen"

    # System
    SYSTEM = "system"


@dataclass
class Event:
    type: EventType | str
    data: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        value = self.type.value if isinstance(self.type, EventType) else str(self.type)
        return {"id": self.id, "ts": self.ts, "type": value, "data": self.data}


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

    def off(self, pattern: str, handler: Handler) -> None:
        """Unregister a handler previously added via :meth:`on`.

        Matches by ``pattern`` + handler identity (bound methods compare equal
        for the same instance + function, so ``bus.off(p, self.cb)`` removes
        what ``bus.on(p, self.cb)`` registered). No-op if not found. Same
        simple (lock-free) implementation style as :meth:`on`.
        """
        handlers = self._handlers.get(pattern)
        if handlers is None:
            return
        if handler in handlers:
            handlers.remove(handler)
        if not handlers:
            self._handlers.pop(pattern, None)

    # -- publish -------------------------------------------------------
    async def publish(self, type_: EventType | str, **data: Any) -> Event:
        event = Event(type=type_, data=data)
        self.history.append(event)
        if len(self.history) > self._history_size:
            self.history = self.history[-self._history_size :]

        for pattern, queues in self._subscribers.items():
            if self._match(pattern, event.type.value if isinstance(event.type, EventType) else str(event.type)):
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
            if self._match(pattern, event.type.value if isinstance(event.type, EventType) else str(event.type)):
                for h in handlers:
                    try:
                        result = h(event)
                        if asyncio.iscoroutine(result):
                            asyncio.create_task(result)  # noqa: RUF006
                    except Exception:
                        logger.exception("event handler failed for %s", event.type)
        return event

    @staticmethod
    def _match(pattern: str, value: str) -> bool:
        if pattern == "*":
            return True
        if pattern.endswith(".*"):
            return value.startswith(pattern[:-1])
        return pattern == value


# Singleton used across the process.
bus = EventBus()
