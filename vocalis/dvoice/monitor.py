"""TaskMonitor: watches every agent task in real time.

Subscribes to the event bus and maintains a live view of in-flight work.
Features:
  * progress narration (D-VOICE speaks milestones at 25/50/75/100%)
  * watchdog: alerts when a task makes no progress for N seconds
    (stalled tasks also fire the completion hooks with status="stalled")
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from vocalis.config import MonitorConfig, VocalisConfig
from vocalis.server.events import Event, EventBus, EventType, bus

logger = logging.getLogger("vocalis.dvoice.monitor")

MILESTONES = (0.25, 0.5, 0.75)


@dataclass
class TrackedTask:
    task_id: str
    agent: str
    instruction: str
    status: str = "running"
    progress: float = 0.0
    last_update: float = field(default_factory=time.time)
    last_milestone: float = 0.0
    started_at: float = field(default_factory=time.time)


class TaskMonitor:
    def __init__(
        self,
        config: VocalisConfig | None = None,
        event_bus: EventBus | None = None,
        on_narration=None,
        on_completion=None,
    ) -> None:
        self.config = config or VocalisConfig.load()
        self.cfg: MonitorConfig = self.config.monitor
        self.bus = event_bus or bus
        self.on_narration = on_narration  # async callable(str) -> None
        self.on_completion = on_completion  # async callable(TrackedTask) -> None
        self.tracked: dict[str, TrackedTask] = {}
        self._queue = None
        self._watchdog_task: asyncio.Task | None = None
        self._consume_task: asyncio.Task | None = None
        self._running = False

    # ------------------------------------------------------------------
    async def start(self) -> None:
        self._queue = self.bus.subscribe("task.*")
        self._running = True
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())
        # Keep a reference so the consume loop is never garbage-collected.
        self._consume_task = asyncio.create_task(self._consume())

    async def stop(self) -> None:
        self._running = False
        if self._queue is not None:
            self.bus.unsubscribe(self._queue)
        pending = [t for t in (self._watchdog_task, self._consume_task) if t is not None]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    # ------------------------------------------------------------------
    async def _consume(self) -> None:
        assert self._queue is not None
        while self._running:
            try:
                event: Event = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            try:
                await self._handle(event)
            except Exception:
                logger.exception("failed handling event %s", event.type)

    async def _handle(self, event: Event) -> None:
        data = event.data
        task_id = data.get("id", "")
        if event.type == EventType.TASK_STARTED:
            self.tracked[task_id] = TrackedTask(
                task_id=task_id, agent=data.get("agent", "?"),
                instruction=data.get("instruction", ""),
            )
        elif event.type == EventType.TASK_PROGRESS:
            task = self.tracked.get(task_id)
            if not task:
                return
            task.progress = float(data.get("progress", task.progress))
            task.last_update = time.time()
            for m in MILESTONES:
                if task.last_milestone < m <= task.progress:
                    task.last_milestone = m
                    await self._narrate(
                        f"{task.agent} milestone: {int(m*100)} percent. "
                        f"Current step: {data.get('current_step', '')}."
                    )
        elif event.type in (EventType.TASK_COMPLETED, EventType.TASK_FAILED):
            task = self.tracked.pop(task_id, None)
            if not task:
                return
            task.status = "completed" if event.type == EventType.TASK_COMPLETED else "failed"
            if self.on_completion:
                try:
                    await self.on_completion(task)
                except Exception:
                    logger.exception("completion hook failed for %s", task_id)

    # ------------------------------------------------------------------
    async def _watchdog_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self.cfg.poll_interval_s)
            now = time.time()
            for task in list(self.tracked.values()):
                stall = now - task.last_update
                if stall > self.cfg.watchdog_timeout_s:
                    await self.bus.publish(
                        EventType.MONITOR_ALERT,
                        message=f"{task.agent} task '{task.instruction[:40]}' appears stalled "
                        f"(no update for {stall:.0f}s)",
                        task_id=task.task_id,
                    )
                    await self._narrate(
                        f"Watchdog: {task.agent} has been silent for {stall:.0f} seconds."
                    )
                    task.last_update = now  # avoid alert spam
                    # Surface stalls through the same notification channel
                    # (voice chime / toast) as completions.
                    if self.on_completion:
                        stalled = self.tracked.pop(task.task_id, task)
                        stalled.status = "stalled"
                        try:
                            await self.on_completion(stalled)
                        except Exception:
                            logger.exception("stall hook failed for %s", task.task_id)

    async def _narrate(self, text: str) -> None:
        if self.on_narration:
            try:
                await self.on_narration(text)
            except Exception:
                logger.exception("narration failed")

    # ------------------------------------------------------------------
    def live_view(self) -> list[dict]:
        return [
            {
                "id": t.task_id,
                "agent": t.agent,
                "instruction": t.instruction,
                "progress": round(t.progress, 3),
                "status": t.status,
                "running_for": round(time.time() - t.started_at, 1),
            }
            for t in self.tracked.values()
        ]
