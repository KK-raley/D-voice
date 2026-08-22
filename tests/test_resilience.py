"""Track D connector-resilience tests (offline): cancellation (D4),
retry + circuit breaker (D3), and connector health (D2).

No network, no real CLI agents, no real sleeps: clocks and sleeps are
injected fakes, and the only real subprocess is ``sys.executable -c``.
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest

from vocalis.agents.base import AgentConnector, AgentStatus, TaskRecord, TaskStatus
from vocalis.agents.cli_agent import GenericCLIAgent
from vocalis.agents.registry import AgentRegistry
from vocalis.agents.resilience import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    retry_async,
)
from vocalis.server.events import EventBus, EventType


# ----------------------------------------------------------------------
# helpers: fake connectors, bus, clock
# ----------------------------------------------------------------------
class _FastAgent(AgentConnector):
    """Instant-success connector."""

    name = "fastok"

    async def stream_run(
        self, instruction: str, record: TaskRecord, **_: Any
    ) -> AsyncIterator[float | str]:
        yield "working"
        record.output = f"done: {instruction}"
        yield 1.0


class _FailingAgent(AgentConnector):
    """Connector whose stream always explodes mid-flight."""

    name = "fastfail"

    def __init__(self, event_bus: EventBus | None = None) -> None:
        super().__init__(event_bus)
        self.calls = 0

    async def stream_run(
        self, instruction: str, record: TaskRecord, **_: Any
    ) -> AsyncIterator[float | str]:
        self.calls += 1
        yield "about to fail"
        raise RuntimeError("connector exploded")


class _BlockingAgent(AgentConnector):
    """Parks inside asyncio.sleep until cancelled (D4 test subject)."""

    name = "blocker"

    def __init__(self, event_bus: EventBus | None = None) -> None:
        super().__init__(event_bus)
        self.cancelled_in_stream = False

    async def stream_run(
        self, instruction: str, record: TaskRecord, **_: Any
    ) -> AsyncIterator[float | str]:
        yield "starting"
        try:
            await asyncio.sleep(30.0)
        except asyncio.CancelledError:
            self.cancelled_in_stream = True
            raise
        yield 1.0


class _YieldParkAgent(AgentConnector):
    """Yields one step; its ``finally`` proves generator teardown ran."""

    name = "yieldpark"

    def __init__(self, event_bus: EventBus | None = None) -> None:
        super().__init__(event_bus)
        self.finally_ran = False

    async def stream_run(
        self, instruction: str, record: TaskRecord, **_: Any
    ) -> AsyncIterator[float | str]:
        try:
            yield "step one"
            yield 1.0
        finally:
            self.finally_ran = True


class _GatedBus(EventBus):
    """Bus whose TASK_PROGRESS publishing parks on a gate.

    Simulates a slow HUD relay so ``run()`` gets cancelled while the stream
    generator is suspended at a ``yield`` (the aclosing teardown path).
    """

    def __init__(self) -> None:
        super().__init__()
        self.gate: asyncio.Event = asyncio.Event()

    async def publish(self, type_: EventType | str, **data: Any):
        if type_ is EventType.TASK_PROGRESS:
            await self.gate.wait()
        return await super().publish(type_, **data)


class _FakeClock:
    """Injectable monotonic clock: advance() instead of sleeping."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def _noop_sleep(_: float) -> None:
    """Injectable no-op sleep for offline retry tests."""


# ----------------------------------------------------------------------
# D4: task cancellation
# ----------------------------------------------------------------------
def test_task_status_values_backward_compatible() -> None:
    """Existing statuses keep their names; CANCELLED is purely additive."""
    assert [s.value for s in TaskStatus] == [
        "queued",
        "running",
        "completed",
        "failed",
        "cancelled",
    ]


def test_cancel_marks_record_and_emits_events() -> None:
    """Cancelling a running dispatch: CANCELLED record, task.failed event,
    active_tasks cleanup, CancelledError re-raised, agent back to idle."""

    async def scenario() -> tuple[TaskRecord, _BlockingAgent, list]:
        bus = EventBus()
        agent = _BlockingAgent(bus)
        record = TaskRecord(agent="blocker", instruction="long job")
        q = bus.subscribe("task.*")
        task = asyncio.create_task(agent.run("long job", record=record))

        started = await asyncio.wait_for(q.get(), timeout=1.0)
        assert started.type is EventType.TASK_STARTED
        assert started.data["id"] == record.id
        # let the stream reach its blocking sleep
        await asyncio.sleep(0.05)
        assert agent.active_tasks[record.id] is record

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        events = []
        while True:
            event = await asyncio.wait_for(q.get(), timeout=1.0)
            events.append(event)
            if event.type is EventType.TASK_FAILED:
                break
        return record, agent, events

    record, agent, events = asyncio.run(scenario())

    assert agent.cancelled_in_stream is True
    assert record.status is TaskStatus.CANCELLED
    assert record.error == "cancelled"
    assert record.finished_at is not None
    assert agent.active_tasks == {}
    assert agent.status is AgentStatus.IDLE

    kinds = [e.type for e in events]
    assert EventType.TASK_PROGRESS in kinds  # "starting" step was narrated
    assert kinds[-1] is EventType.TASK_FAILED
    failed = events[-1]
    assert failed.data["id"] == record.id
    assert failed.data["status"] == "cancelled"
    assert failed.data["error"] == "cancelled"
    # cancellation is not a connector failure (D2/D3 semantics)
    health = agent.health_dict()
    assert health["consecutive_failures"] == 0
    assert health["last_error"] is None
    assert health["total_runs"] == 1


def test_cancelled_dispatch_enters_registry_history() -> None:
    """D4: registry.dispatch 取消的任务也进 history（与 completed/failed 对称，
    HUD recent / get_status 可见）。"""

    async def scenario() -> AgentRegistry:
        bus = EventBus()
        registry = AgentRegistry(bus)
        registry.register(_BlockingAgent(bus))
        task = asyncio.create_task(registry.dispatch("blocker", "wait forever"))
        await asyncio.sleep(0.05)  # 进入 running
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return registry

    registry = asyncio.run(scenario())

    assert len(registry.history) == 1
    record = registry.history[0]
    assert record.status is TaskStatus.CANCELLED
    assert record.error == "cancelled"
    # snapshot 的 recent 列表可见取消任务
    assert any(r["status"] == "cancelled" for r in registry.snapshot()["recent"])


def test_cancel_while_generator_parked_at_yield_closes_stream() -> None:
    """Cancellation landing between yields must still run the generator's
    finally block (aclosing teardown) - the mechanism CLI process cleanup
    relies on."""

    async def scenario() -> tuple[TaskRecord, _YieldParkAgent, Any]:
        bus = _GatedBus()
        agent = _YieldParkAgent(bus)
        record = TaskRecord(agent="yieldpark", instruction="x")
        q = bus.subscribe("task.failed")
        task = asyncio.create_task(agent.run("x", record=record))

        # wait until run() is parked on the gated progress publish, which
        # means the generator is suspended at its first yield
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        failed = await asyncio.wait_for(q.get(), timeout=1.0)
        return record, agent, failed

    record, agent, failed = asyncio.run(scenario())
    assert agent.finally_ran is True  # generator teardown ran
    assert record.status is TaskStatus.CANCELLED
    assert failed.data["status"] == "cancelled"


# ----------------------------------------------------------------------
# D3: circuit breaker (pure logic, fake clock)
# ----------------------------------------------------------------------
def test_breaker_full_lifecycle_closed_open_half_open_closed() -> None:
    """3 consecutive failures -> open -> fast-fail -> cool-down -> half-open
    probe -> success -> closed."""

    async def scenario() -> list[CircuitState]:
        clock = _FakeClock()
        cb = CircuitBreaker(failure_threshold=3, reset_timeout_s=30.0, clock=clock)
        assert cb.state is CircuitState.CLOSED

        async def boom() -> None:
            raise RuntimeError("down")

        for _ in range(3):
            with pytest.raises(RuntimeError, match="down"):
                await cb.call(boom)
        assert cb.state is CircuitState.OPEN
        assert cb.consecutive_failures == 3

        invoked: list[int] = []

        async def probe() -> str:
            invoked.append(1)
            return "fine"

        # open circuit: fast-fail without invoking the wrapped call
        with pytest.raises(CircuitOpenError):
            await cb.call(probe)
        assert invoked == []

        # still open just before the cool-down elapses
        clock.advance(29.9)
        with pytest.raises(CircuitOpenError):
            await cb.call(probe)
        assert invoked == []

        # cool-down elapsed: one probe is admitted (half_open observed
        # from inside the probe), success closes the circuit
        clock.advance(0.1)
        seen_states: list[CircuitState] = []

        async def observing_probe() -> str:
            seen_states.append(cb.state)
            return "fine"

        assert await cb.call(observing_probe) == "fine"
        assert seen_states == [CircuitState.HALF_OPEN]
        assert cb.state is CircuitState.CLOSED
        assert cb.consecutive_failures == 0
        return seen_states

    asyncio.run(scenario())


def test_breaker_half_open_failure_reopens_for_full_cooldown() -> None:
    async def scenario() -> None:
        clock = _FakeClock()
        cb = CircuitBreaker(failure_threshold=2, reset_timeout_s=30.0, clock=clock)

        async def boom() -> None:
            raise RuntimeError("down")

        async def fine() -> str:
            return "fine"

        for _ in range(2):
            with pytest.raises(RuntimeError):
                await cb.call(boom)
        assert cb.state is CircuitState.OPEN

        clock.advance(30.0)  # half-open window opens
        with pytest.raises(RuntimeError):
            await cb.call(boom)  # probe fails
        assert cb.state is CircuitState.OPEN  # re-opened
        assert cb.consecutive_failures == 3

        # a fresh full cool-down is required before the next probe
        clock.advance(29.9)
        with pytest.raises(CircuitOpenError):
            await cb.call(fine)
        clock.advance(0.1)
        assert await cb.call(fine) == "fine"
        assert cb.state is CircuitState.CLOSED

    asyncio.run(scenario())


def test_breaker_success_resets_failure_count() -> None:
    async def scenario() -> None:
        cb = CircuitBreaker(failure_threshold=3)

        async def boom() -> None:
            raise RuntimeError("down")

        async def fine() -> str:
            return "fine"

        with pytest.raises(RuntimeError):
            await cb.call(boom)
        with pytest.raises(RuntimeError):
            await cb.call(boom)
        assert cb.consecutive_failures == 2
        assert await cb.call(fine) == "fine"
        assert cb.consecutive_failures == 0
        assert cb.state is CircuitState.CLOSED
        # two more failures after a success must NOT trip the breaker
        with pytest.raises(RuntimeError):
            await cb.call(boom)
        with pytest.raises(RuntimeError):
            await cb.call(boom)
        assert cb.state is CircuitState.CLOSED

    asyncio.run(scenario())


def test_breaker_cancelled_probe_does_not_count_as_failure() -> None:
    async def scenario() -> None:
        clock = _FakeClock()
        cb = CircuitBreaker(failure_threshold=2, reset_timeout_s=30.0, clock=clock)

        async def boom() -> None:
            raise RuntimeError("down")

        for _ in range(2):
            with pytest.raises(RuntimeError):
                await cb.call(boom)
        assert cb.state is CircuitState.OPEN

        clock.advance(30.0)

        async def cancelled() -> None:
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await cb.call(cancelled)
        # still half-open, probe slot freed: the next call is admitted
        async def fine() -> str:
            return "fine"

        assert await cb.call(fine) == "fine"
        assert cb.state is CircuitState.CLOSED

    asyncio.run(scenario())


def test_breaker_rejects_invalid_threshold() -> None:
    with pytest.raises(ValueError):
        CircuitBreaker(failure_threshold=0)


# ----------------------------------------------------------------------
# D3: retry_async (pure logic, fake sleep)
# ----------------------------------------------------------------------
def test_retry_succeeds_after_transient_failure() -> None:
    async def scenario() -> list[float]:
        attempts: list[int] = []
        delays: list[float] = []

        async def flaky() -> str:
            attempts.append(1)
            if len(attempts) < 2:
                raise RuntimeError("transient")
            return "recovered"

        result = await retry_async(
            flaky, attempts=3, base_delay=0.5, sleep=_noop_sleep
        )
        assert result == "recovered"
        assert len(attempts) == 2
        return delays

    asyncio.run(scenario())


def test_retry_all_attempts_fail_raises_last_error() -> None:
    async def scenario() -> list[float]:
        calls: list[int] = []
        delays: list[float] = []

        async def always_fail() -> None:
            calls.append(len(calls))
            raise RuntimeError(f"failure #{len(calls)}")

        with pytest.raises(RuntimeError, match="failure #3"):
            await retry_async(
                always_fail, attempts=3, base_delay=0.25, sleep=_noop_sleep
            )
        assert len(calls) == 3
        return delays

    delays = asyncio.run(scenario())
    assert delays == []  # injected sleep was a no-op; backoff never real-slept


def test_retry_backoff_is_exponential() -> None:
    async def scenario() -> list[float]:
        delays: list[float] = []

        async def record_delay(d: float) -> None:
            delays.append(d)

        async def always_fail() -> None:
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            await retry_async(
                always_fail, attempts=3, base_delay=0.5, sleep=record_delay
            )
        return delays

    assert asyncio.run(scenario()) == [0.5, 1.0]


def test_retry_never_retries_cancellation() -> None:
    async def scenario() -> int:
        calls: list[int] = []

        async def cancel_first() -> None:
            calls.append(1)
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await retry_async(cancel_first, attempts=3, sleep=_noop_sleep)
        return len(calls)

    assert asyncio.run(scenario()) == 1


def test_retry_rejects_invalid_attempts() -> None:
    async def fine() -> str:
        return "fine"

    with pytest.raises(ValueError):
        asyncio.run(retry_async(fine, attempts=0))


def test_retry_integration_via_run_recovers_from_transient_failure() -> None:
    """A connector with retry_attempts=2 survives a first-attempt failure."""

    class FlakyAgent(AgentConnector):
        name = "flaky"
        retry_attempts = 2

        def __init__(self, event_bus: EventBus | None = None) -> None:
            super().__init__(event_bus)
            self.calls = 0

        async def stream_run(
            self, instruction: str, record: TaskRecord, **_: Any
        ) -> AsyncIterator[float | str]:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient glitch")
            yield "recovered"
            record.output = "done"
            yield 1.0

    async def scenario() -> tuple[TaskRecord, FlakyAgent]:
        agent = FlakyAgent(EventBus())
        record = await agent.run("job")
        return record, agent

    record, agent = asyncio.run(scenario())
    assert agent.calls == 2
    assert record.status is TaskStatus.COMPLETED
    assert record.output == "done"
    assert agent.health_dict()["consecutive_failures"] == 0


def test_circuit_breaker_integration_via_run_fails_fast() -> None:
    """A flaky connector trips its breaker: later dispatches fail fast
    without invoking stream_run again."""

    async def scenario() -> tuple[list[TaskRecord], _FailingAgent, CircuitBreaker]:
        bus = EventBus()
        clock = _FakeClock()
        cb = CircuitBreaker(failure_threshold=2, reset_timeout_s=30.0, clock=clock)
        agent = _FailingAgent(bus)
        agent.circuit_breaker = cb

        records = [await agent.run("job") for _ in range(3)]
        return records, agent, cb

    records, agent, cb = asyncio.run(scenario())
    assert cb.state is CircuitState.OPEN

    # first two dispatches actually ran the (failing) stream
    assert [r.status for r in records] == [
        TaskStatus.FAILED,
        TaskStatus.FAILED,
        TaskStatus.FAILED,
    ]
    assert agent.calls == 2  # third dispatch fast-failed: breaker was open
    assert "circuit open" in (records[2].error or "")

    health = agent.health_dict()
    assert health["consecutive_failures"] == 3
    assert health["total_runs"] == 3
    assert health["last_error"] == records[2].error


# ----------------------------------------------------------------------
# D4: CLI subprocess teardown
# ----------------------------------------------------------------------
class _FakeStdout:
    """Pipe stand-in whose readline() parks forever (like a silent child)."""

    async def readline(self) -> bytes:
        await asyncio.Event().wait()
        return b""  # pragma: no cover


class _FakeProc:
    """asyncio subprocess stand-in with an observable lifecycle."""

    def __init__(self, *, ignores_terminate: bool = False) -> None:
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self.stdout = _FakeStdout()
        self._ignores_terminate = ignores_terminate
        self._exited = asyncio.Event()

    def terminate(self) -> None:
        self.terminated = True
        if not self._ignores_terminate:
            self._exited.set()

    def kill(self) -> None:
        self.killed = True
        self._exited.set()

    async def wait(self) -> int:
        await self._exited.wait()
        self.returncode = -9 if self.killed else 1
        return self.returncode


def test_cli_cancel_terminates_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cancelling a CLI dispatch terminates the child process (mocked exec)."""

    async def scenario() -> tuple[TaskRecord, _FakeProc]:
        proc = _FakeProc()

        async def fake_exec(*args: Any, **kwargs: Any) -> _FakeProc:
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        agent = GenericCLIAgent(
            name="stub",
            command=[sys.executable, "-c", "print()"],
            event_bus=EventBus(),
        )
        record = TaskRecord(agent="stub", instruction="x")
        task = asyncio.create_task(agent.run("x", record=record))

        for _ in range(1000):
            if agent._proc is not None:
                break
            await asyncio.sleep(0)
        else:  # pragma: no cover
            pytest.fail("subprocess was never created")

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return record, proc

    record, proc = asyncio.run(scenario())
    assert record.status is TaskStatus.CANCELLED
    assert record.error == "cancelled"
    assert proc.terminated is True
    assert proc.returncode is not None  # child reaped, not left dangling


def test_cli_cancel_terminates_real_subprocess() -> None:
    """End-to-end: a real ``python -c 'sleep'`` child dies on cancellation."""

    async def scenario() -> tuple[TaskRecord, GenericCLIAgent]:
        agent = GenericCLIAgent(
            name="sleeper",
            command=[sys.executable, "-c", "import time; time.sleep(30)"],
            event_bus=EventBus(),
        )
        record = TaskRecord(agent="sleeper", instruction="sleep")
        task = asyncio.create_task(agent.run("sleep", record=record))

        deadline = time.monotonic() + 10.0
        while agent._proc is None and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert agent._proc is not None, "child process was never spawned"

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return record, agent

    record, agent = asyncio.run(scenario())
    assert record.status is TaskStatus.CANCELLED
    assert agent._proc is not None
    assert agent._proc.returncode is not None  # really terminated + reaped


def test_cli_shutdown_escalates_to_kill_when_terminate_ignored() -> None:
    """Child ignoring SIGTERM gets SIGKILLed after the grace period."""

    async def scenario() -> _FakeProc:
        proc = _FakeProc(ignores_terminate=True)
        agent = GenericCLIAgent(
            name="stub",
            command=[sys.executable, "-c", "x"],
            event_bus=EventBus(),
            terminate_grace_s=0.05,
        )
        await agent._shutdown_proc(proc)
        return proc

    proc = asyncio.run(scenario())
    assert proc.terminated is True
    assert proc.killed is True
    assert proc.returncode == -9


def test_cli_shutdown_is_noop_when_process_already_reaped() -> None:
    async def scenario() -> _FakeProc:
        proc = _FakeProc()
        proc.returncode = 0
        agent = GenericCLIAgent(
            name="stub", command=[sys.executable, "-c", "x"], event_bus=EventBus()
        )
        await agent._shutdown_proc(proc)
        return proc

    proc = asyncio.run(scenario())
    assert proc.terminated is False
    assert proc.killed is False


def test_cli_failure_path_also_terminates_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stream failure (non-zero exit emulation) must not leak the child."""

    async def scenario() -> tuple[TaskRecord, _FakeProc]:
        proc = _FakeProc(ignores_terminate=True)  # never exits on its own

        async def fake_exec(*args: Any, **kwargs: Any) -> _FakeProc:
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        agent = GenericCLIAgent(
            name="stub",
            command=[sys.executable, "-c", "x"],
            event_bus=EventBus(),
            timeout_s=0.05,  # watchdog fires immediately
            terminate_grace_s=0.05,  # escalate to kill fast in tests
        )
        record = await agent.run("x")
        return record, proc

    record, proc = asyncio.run(scenario())
    assert record.status is TaskStatus.FAILED
    assert "watchdog" in (record.error or "")
    assert proc.terminated is True
    assert proc.killed is True  # watchdog escalated past ignored SIGTERM
    assert proc.returncode is not None


# ----------------------------------------------------------------------
# D2: connector health
# ----------------------------------------------------------------------
def test_health_success_updates_snapshot() -> None:
    async def scenario() -> dict[str, Any]:
        agent = _FastAgent(EventBus())
        record = await agent.run("job")
        assert record.status is TaskStatus.COMPLETED
        return agent.health_dict()

    health = asyncio.run(scenario())
    assert health["last_error"] is None
    assert health["last_latency_ms"] is not None
    assert health["last_latency_ms"] >= 0
    assert health["last_success_ts"] is not None
    assert health["consecutive_failures"] == 0
    assert health["total_runs"] == 1
    assert health["total_failures"] == 0


def test_health_failure_then_recovery() -> None:
    async def scenario() -> list[dict[str, Any]]:
        agent = _FailingAgent(EventBus())
        snapshots = []
        await agent.run("job 1")
        snapshots.append(agent.health_dict())
        await agent.run("job 2")
        snapshots.append(agent.health_dict())
        return snapshots

    first, second = asyncio.run(scenario())
    assert first["last_error"] == "connector exploded"
    assert first["consecutive_failures"] == 1
    assert first["total_failures"] == 1
    assert first["total_runs"] == 1
    assert first["last_success_ts"] is None

    assert second["consecutive_failures"] == 2
    assert second["total_failures"] == 2
    assert second["total_runs"] == 2


def test_health_recovery_clears_error_on_success() -> None:
    class RecoveringAgent(AgentConnector):
        name = "recovering"
        fail_first = True

        async def stream_run(
            self, instruction: str, record: TaskRecord, **_: Any
        ) -> AsyncIterator[float | str]:
            if self.fail_first:
                self.fail_first = False
                raise RuntimeError("first dispatch failed")
            yield "ok"
            yield 1.0

    async def scenario() -> dict[str, Any]:
        agent = RecoveringAgent(EventBus())
        failed = await agent.run("job")
        assert failed.status is TaskStatus.FAILED
        ok = await agent.run("job")
        assert ok.status is TaskStatus.COMPLETED
        return agent.health_dict()

    health = asyncio.run(scenario())
    assert health["last_error"] is None
    assert health["consecutive_failures"] == 0
    assert health["total_failures"] == 1
    assert health["total_runs"] == 2
    assert health["last_success_ts"] is not None


def test_agent_status_event_carries_health() -> None:
    """AGENT_STATUS payload gains a health snapshot (additive, HUD-safe)."""

    async def scenario() -> Any:
        bus = EventBus()
        agent = _FailingAgent(bus)
        q = bus.subscribe("agent.status")
        await agent.run("job")
        return await asyncio.wait_for(q.get(), timeout=1.0)

    event = asyncio.run(scenario())
    assert event.data["agent"] == "fastfail"
    assert event.data["status"] == "error"
    assert event.data["health"]["last_error"] == "connector exploded"
    assert event.data["health"]["consecutive_failures"] == 1


def test_echo_agent_behaviour_unchanged() -> None:
    """Backward-compat guard: the demo agent still completes untouched."""
    from vocalis.agents.echo import EchoAgent

    async def scenario() -> tuple[TaskRecord, EchoAgent]:
        agent = EchoAgent(EventBus())
        record = await agent.run("demo")
        return record, agent

    record, agent = asyncio.run(scenario())
    assert record.status is TaskStatus.COMPLETED
    assert record.progress == 1.0
    assert agent.status is AgentStatus.IDLE
    assert not agent.active_tasks
