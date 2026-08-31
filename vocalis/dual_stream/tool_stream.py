"""ToolStream——双流架构之"工具流"：asyncio 任务队列持续执行。

工具 / 长任务永远在后台 worker 协程中执行，语音流（VoiceStream）不
等待、不被阻塞：提交即返回 task_id，进度与结果全部经 :class:`~vocalis.server.events.EventBus`
以既有 ``task.*`` 事件类型广播（与 agent 任务监控共用一套事件词汇，
HUD / monitor / notifier 天然兼容）：

* ``task.queued``    —— 已入队（data: task_id, name）
* ``task.started``   —— 某 worker 开始执行
* ``task.completed`` —— 成功（data.value 携带返回值）
* ``task.failed``    —— 抛异常（data.error 携带错误信息）

失败不会击穿 worker：异常被转成 ``task.failed`` 事件后继续消费下一
个任务。执行历史保存在 :attr:`results`（有界 deque）供诊断。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from vocalis.server.events import Event, EventBus, EventType
from vocalis.server.events import bus as events_bus

logger = logging.getLogger("vocalis.dual_stream.tool_stream")

#: 工具工厂：返回一个待执行的协程（重新入队安全，避免消费同一个 coroutine）。
ToolFactory = Callable[[], Awaitable[Any]]


@dataclass
class _ToolJob:
    """队列中的一个工具作业。"""

    task_id: str
    name: str
    factory: ToolFactory


@dataclass
class ToolResult:
    """一次工具执行的最终结果（成功或失败）。"""

    task_id: str
    name: str
    ok: bool
    value: Any = None
    error: str | None = None


class ToolStream:
    """可配置并发度的后台工具执行队列。

    用法::

        tools = ToolStream(bus=event_bus, max_workers=2)
        tools.start()                       # 启动 worker 协程池
        task_id = tools.submit("查天气", fetch_weather)
        ...                                  # 语音流照常工作
        await tools.wait_all(timeout=5)      # 需要时再等
        await tools.shutdown()

    注意：实例（内部含 asyncio.Queue）须在事件循环所在线程创建。
    """

    def __init__(self, bus: EventBus | None = None, max_workers: int = 2) -> None:
        self.bus = bus or events_bus
        self.max_workers = max(1, int(max_workers))
        self.results: deque[ToolResult] = deque(maxlen=256)
        self._queue: asyncio.Queue[_ToolJob] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []
        self._running = 0
        self._started = False

    # -- 生命周期 ------------------------------------------------------
    def start(self) -> None:
        """启动 worker 协程池（幂等）。"""
        if self._started:
            return
        self._started = True
        self._workers = [
            asyncio.create_task(self._worker(), name=f"tool-worker-{i}")
            for i in range(self.max_workers)
        ]

    async def shutdown(self, timeout: float = 5.0) -> None:
        """先等队列排空（带超时），再取消全部 worker。"""
        if not self._started:
            return
        await self.wait_all(timeout)
        for task in self._workers:
            task.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers = []
        self._started = False

    # -- 提交与查询 ----------------------------------------------------
    def submit(self, name: str, factory: ToolFactory, task_id: str | None = None) -> str:
        """入队一个工具作业并立即返回 task_id（绝不阻塞语音流）。"""
        task_id = task_id or uuid.uuid4().hex[:12]
        self._queue.put_nowait(_ToolJob(task_id=task_id, name=name, factory=factory))
        self._emit(EventType.TASK_QUEUED, task_id=task_id, name=name)
        return task_id

    @property
    def pending(self) -> int:
        """仍在队列中或正在执行的任务数。"""
        return self._queue.qsize() + self._running

    async def wait_all(self, timeout: float = 5.0) -> bool:
        """等待全部任务完成；超时返回 False（不抛异常，便于优雅关闭）。"""
        deadline = time.monotonic() + timeout
        while self.pending > 0:
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.01)
        return True

    # -- 内部 ----------------------------------------------------------
    async def _worker(self) -> None:
        """worker 主循环：取作业 -> 发 started -> 执行 -> 发 completed/failed。"""
        while True:
            job = await self._queue.get()
            self._running += 1
            try:
                await self.bus.publish(
                    EventType.TASK_STARTED, task_id=job.task_id, name=job.name
                )
                try:
                    value = await job.factory()
                except Exception as exc:
                    # 工具失败不击穿 worker：转成事件，继续服务后续任务。
                    logger.warning("tool %r (%s) failed: %s", job.name, job.task_id, exc)
                    result = ToolResult(
                        task_id=job.task_id, name=job.name, ok=False, error=str(exc)
                    )
                    await self.bus.publish(
                        EventType.TASK_FAILED,
                        task_id=job.task_id,
                        name=job.name,
                        error=str(exc),
                    )
                else:
                    result = ToolResult(
                        task_id=job.task_id, name=job.name, ok=True, value=value
                    )
                    # 事件最终要经 HUD WebSocket 走 json.dumps：bytes / Path
                    # 等不可 JSON 序列化的返回值兜底转成 repr 字符串，
                    # 避免下游序列化崩溃（results 里仍保留原始值供诊断）。
                    safe_value = (
                        value
                        if isinstance(value, (str, int, float, bool, type(None), list, dict))
                        else repr(value)
                    )
                    await self.bus.publish(
                        EventType.TASK_COMPLETED,
                        task_id=job.task_id,
                        name=job.name,
                        value=safe_value,
                    )
                self.results.append(result)
            except asyncio.CancelledError:
                raise  # shutdown 主动取消：正常退出路径
            finally:
                self._running -= 1

    def _emit(self, type_: EventType | str, **data: Any) -> None:
        """在同步上下文（submit）里发布事件的辅助函数。"""
        task = asyncio.create_task(self.bus.publish(type_, **data))
        # 保存引用防止任务被 GC；完成后自动丢弃。
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)


#: 已提交但尚未完成的后台发布任务（防 GC）。
_BACKGROUND_TASKS: set[asyncio.Task[Event]] = set()
