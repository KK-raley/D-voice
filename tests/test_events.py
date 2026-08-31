"""EventBus.on/off 注册表单测：handler 可按 pattern + handler 精确注销。

双流协调器（DualStreamOrchestrator.shutdown）依赖 ``off()`` 注销回调，
避免同一 bus 上反复 start/shutdown 累积幽灵 handler。完全离线，
不触碰全局单例 ``vocalis.server.events.bus``。
"""

from __future__ import annotations

import asyncio

from vocalis.server.events import Event, EventBus, EventType


async def test_off_removes_registered_handler() -> None:
    """off(pattern, handler) 后，同 pattern 的后续事件不再触发该 handler。"""
    bus = EventBus()
    seen: list[str] = []

    def handler(event: Event) -> None:
        seen.append(str(event.data.get("task_id", "")))

    bus.on(EventType.TASK_COMPLETED.value, handler)
    await bus.publish(EventType.TASK_COMPLETED, task_id="t1")
    assert seen == ["t1"]

    bus.off(EventType.TASK_COMPLETED.value, handler)
    await bus.publish(EventType.TASK_COMPLETED, task_id="t2")
    assert seen == ["t1"]  # 注销后不再收到


async def test_off_only_removes_target_handler() -> None:
    """off 只移除指定 handler；同 pattern 上的其他 handler 照常工作。"""
    bus = EventBus()
    hits_a: list[str] = []
    hits_b: list[str] = []

    def handler_a(event: Event) -> None:
        hits_a.append("a")

    def handler_b(event: Event) -> None:
        hits_b.append("b")

    bus.on(EventType.TASK_FAILED.value, handler_a)
    bus.on(EventType.TASK_FAILED.value, handler_b)
    bus.off(EventType.TASK_FAILED.value, handler_a)

    await bus.publish(EventType.TASK_FAILED, task_id="t1", error="boom")
    assert hits_a == []
    assert hits_b == ["b"]


async def test_off_with_unregistered_handler_is_noop() -> None:
    """off 未注册的 handler / 不存在的 pattern / 重复 off 均为 no-op。"""
    bus = EventBus()

    def handler(event: Event) -> None:
        ...

    bus.off(EventType.TASK_FAILED.value, handler)  # 从未 on 过
    bus.off("never.registered", handler)  # 不存在的 pattern

    bus.on(EventType.TASK_FAILED.value, handler)
    bus.off(EventType.TASK_FAILED.value, handler)  # 移除
    bus.off(EventType.TASK_FAILED.value, handler)  # 重复 off：幂等

    seen: list[str] = []
    bus.on(EventType.TASK_FAILED.value, lambda _e: seen.append("x"))
    await bus.publish(EventType.TASK_FAILED, task_id="t1", error="e")
    assert seen == ["x"]  # bus 状态未被 off 破坏


class _Counter:
    """协程 handler 载体：模拟 orchestrator 注册的绑定方法回调。"""

    def __init__(self) -> None:
        self.hits = 0

    async def on_event(self, event: Event) -> None:
        self.hits += 1


async def test_off_supports_bound_method_handlers() -> None:
    """同一实例的绑定方法可正确注销（orchestrator 的实际用法）。"""
    bus = EventBus()
    counter = _Counter()

    bus.on(EventType.TASK_COMPLETED.value, counter.on_event)
    await bus.publish(EventType.TASK_COMPLETED, task_id="t1")
    await asyncio.sleep(0.01)  # 协程 handler 经 create_task 异步执行
    assert counter.hits == 1

    bus.off(EventType.TASK_COMPLETED.value, counter.on_event)
    await bus.publish(EventType.TASK_COMPLETED, task_id="t2")
    await asyncio.sleep(0.01)
    assert counter.hits == 1  # 注销后不再触发
