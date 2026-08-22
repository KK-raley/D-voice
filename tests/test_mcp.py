"""Offline tests for the D-VOICE MCP server (mcp SDK optional).

The context-level tests run without the ``mcp`` package installed (the SDK
is imported lazily). FastMCP construction is verified only when the SDK is
present (``pytest.importorskip``); everything else is fully offline.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from vocalis.agents.base import TaskRecord
from vocalis.agents.echo import EchoAgent
from vocalis.server.mcp import DVoiceMCPContext, build_mcp_server


@pytest.fixture()
def ctx(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> DVoiceMCPContext:
    """Isolated context: VOCALIS_HOME in tmp_path, real bus/registry/echo agent."""
    monkeypatch.setenv("VOCALIS_HOME", str(tmp_path))
    return DVoiceMCPContext()


def _patch_tts(
    monkeypatch: pytest.MonkeyPatch, *, speak_error: Exception | None = None
) -> None:
    """Stub TTSService.speak/synthesize - no network, no audio hardware."""
    from vocalis.voice.tts import SpeakResult, TTSService

    async def fake_speak(
        self: TTSService, text: str, profile_name: str | None = None, play: bool = True
    ) -> SpeakResult:
        if speak_error is not None:
            raise speak_error
        return SpeakResult(
            ok=True,
            audio_path=Path("fake.mp3"),
            audio_bytes=b"mp3",
            profile=profile_name or "aria",
            characters=len(text),
        )

    async def fake_synthesize(
        self: TTSService, text: str, profile_name: str | None = None
    ) -> SpeakResult:
        return SpeakResult(
            ok=True,
            audio_path=Path("fake.mp3"),
            audio_bytes=b"mp3",
            profile=profile_name or "aria",
            characters=len(text),
        )

    monkeypatch.setattr(TTSService, "speak", fake_speak)
    monkeypatch.setattr(TTSService, "synthesize", fake_synthesize)


# ----------------------------------------------------------------------
# module loads without the mcp SDK (lazy import)
# ----------------------------------------------------------------------
def test_module_imports_without_mcp_sdk() -> None:
    import vocalis.server.mcp as m

    assert hasattr(m, "DVoiceMCPContext")
    assert hasattr(m, "build_mcp_server")
    assert hasattr(m, "main")


def test_build_mcp_server_requires_mcp_sdk(
    ctx: DVoiceMCPContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SDK missing -> clear RuntimeError with install hint (not a crash)."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object):
        if name == "mcp" or name.startswith("mcp."):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(RuntimeError, match="mcp"):
        build_mcp_server(ctx)


# ----------------------------------------------------------------------
# speak
# ----------------------------------------------------------------------
def test_speak_basic(ctx: DVoiceMCPContext, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_tts(monkeypatch)
    result = asyncio.run(ctx.speak("构建完成，全部测试通过"))
    assert result["ok"] is True
    assert result["audio_path"]
    assert result["profile"] == "aria"  # default profile
    assert result["characters"] == len("构建完成，全部测试通过")
    assert result["played"] is True


def test_speak_agent_profile_mapping(
    ctx: DVoiceMCPContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_tts(monkeypatch)
    # config.tts.agent_voices maps claude-code -> orion
    result = asyncio.run(ctx.speak("done", agent="claude-code"))
    assert result["profile"] == "orion"


def test_speak_explicit_profile_wins(
    ctx: DVoiceMCPContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_tts(monkeypatch)
    result = asyncio.run(ctx.speak("hi", profile="whisper-calm", agent="claude-code"))
    assert result["profile"] == "whisper-calm"


def test_speak_empty_text_raises(ctx: DVoiceMCPContext) -> None:
    with pytest.raises(ValueError):
        asyncio.run(ctx.speak(""))
    with pytest.raises(ValueError):
        asyncio.run(ctx.speak("   "))


def test_speak_publishes_dvoice_saying(
    ctx: DVoiceMCPContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vocalis.server.events import EventType

    _patch_tts(monkeypatch)
    received = []

    async def scenario() -> dict:
        q = ctx.bus.subscribe("dvoice.*")
        result = await ctx.speak("测试播报", agent="echo")
        received.append(await asyncio.wait_for(q.get(), timeout=1.0))
        return result

    result = asyncio.run(scenario())
    assert result["ok"] is True
    assert received[0].type is EventType.DVOICE_SAYING
    assert received[0].data["text"] == "测试播报"


def test_speak_play_failure_still_returns_audio(
    ctx: DVoiceMCPContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Headless box: playback explodes, synthesis-only fallback still succeeds."""
    _patch_tts(monkeypatch, speak_error=RuntimeError("no audio device"))
    result = asyncio.run(ctx.speak("headless test"))
    assert result["ok"] is True
    assert result["audio_path"]
    assert result["played"] is False
    assert "no audio device" in result["play_error"]


# ----------------------------------------------------------------------
# report_progress
# ----------------------------------------------------------------------
def test_report_progress_clamps_high(ctx: DVoiceMCPContext) -> None:
    result = asyncio.run(ctx.report_progress("t1", "claude-code", 1.5))
    assert result == {"ok": True, "task_id": "t1", "progress": 1.0, "narrated": True}


def test_report_progress_clamps_low(ctx: DVoiceMCPContext) -> None:
    result = asyncio.run(ctx.report_progress("t2", "echo", -0.2))
    assert result["progress"] == 0.0


def test_report_progress_event_payload(ctx: DVoiceMCPContext) -> None:
    from vocalis.server.events import EventType

    received = []

    async def scenario() -> dict:
        q = ctx.bus.subscribe("task.progress")
        r = await ctx.report_progress(
            "job-1", "claude-code", 0.95, step="编译中", note="快好了"
        )
        received.append(await asyncio.wait_for(q.get(), timeout=1.0))
        return r

    result = asyncio.run(scenario())
    assert result["ok"] is True
    event = received[0]
    assert event.type is EventType.TASK_PROGRESS
    assert event.data["task_id"] == "job-1"
    assert event.data["agent"] == "claude-code"
    assert event.data["progress"] == 0.95
    assert event.data["current_step"] == "编译中"
    assert event.data["note"] == "快好了"


# ----------------------------------------------------------------------
# get_status
# ----------------------------------------------------------------------
def test_get_status(ctx: DVoiceMCPContext) -> None:
    status = ctx.get_status()
    agent_names = [a["name"] for a in status["agents"]]
    assert "echo" in agent_names
    assert "aria" in status["profiles"]
    assert status["default_profile"] == "aria"
    assert "active_tasks" in status
    assert "recent" in status


def test_get_status_async_reports_brain(
    ctx: DVoiceMCPContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_available() -> bool:
        return True

    monkeypatch.setattr(ctx.brain, "available", fake_available)
    status = asyncio.run(ctx.get_status_async())
    assert status["brain_available"] is True
    assert "echo" in [a["name"] for a in status["agents"]]


def test_get_status_async_brain_failure_is_false(ctx: DVoiceMCPContext) -> None:
    async def boom() -> bool:
        raise RuntimeError("ollama down")

    ctx.brain.available = boom  # type: ignore[method-assign]
    status = asyncio.run(ctx.get_status_async())
    assert status["brain_available"] is False


# ----------------------------------------------------------------------
# dispatch_task
# ----------------------------------------------------------------------
class _FastAgent(EchoAgent):
    """EchoAgent minus the simulated sleeps: instant, for completion tests."""

    name = "fast"

    async def stream_run(self, instruction: str, record: TaskRecord, **_: Any) -> AsyncIterator[float | str]:
        yield "working"
        record.output = f"done: {instruction}"
        yield 1.0


def test_dispatch_task_unknown_agent(ctx: DVoiceMCPContext) -> None:
    result = ctx.dispatch_task("nonexistent", "do things")
    assert result["ok"] is False
    assert "nonexistent" in result["error"]
    assert "echo" in result["error"]  # available list embedded
    assert "echo" in result["available_agents"]


def test_dispatch_task_empty_instruction(ctx: DVoiceMCPContext) -> None:
    result = ctx.dispatch_task("echo", "  ")
    assert result["ok"] is False
    assert "instruction" in result["error"]


def test_dispatch_task_queues_echo(ctx: DVoiceMCPContext) -> None:
    """echo: fire-and-forget queueing returns immediately with a task id."""
    from vocalis.server.events import EventType

    async def scenario() -> tuple[dict, list]:
        q = ctx.bus.subscribe("task.*")
        result = ctx.dispatch_task("echo", "say hi")
        # first lifecycle event (task.queued) flows through the bus at once
        first = await asyncio.wait_for(q.get(), timeout=1.0)
        return result, [first]

    result, events = asyncio.run(scenario())
    assert result["ok"] is True
    assert result["queued"] is True
    assert result["agent"] == "echo"
    assert result["task_id"]  # pre-created TaskRecord id
    assert events[0].type is EventType.TASK_QUEUED
    assert events[0].data["id"] == result["task_id"]


def test_dispatch_task_runs_to_completion(ctx: DVoiceMCPContext) -> None:
    """The background dispatch completes and is recorded (fast stub agent)."""
    ctx.registry.register(_FastAgent(ctx.bus))

    async def scenario() -> dict:
        result = ctx.dispatch_task("fast", "quick job")
        assert ctx._background_tasks, "dispatch must register a background task"
        await asyncio.gather(*list(ctx._background_tasks))
        await asyncio.sleep(0)  # let the done callback run
        return result

    result = asyncio.run(scenario())
    assert result["ok"] is True
    assert any(r.id == result["task_id"] for r in ctx.completed)
    assert any(r.id == result["task_id"] for r in ctx.registry.history)
    assert not ctx._background_tasks  # done callback cleaned up


def test_dispatch_task_events_share_task_id(ctx: DVoiceMCPContext) -> None:
    """queued/started/completed events must all carry the returned task id."""
    from vocalis.server.events import EventType

    ctx.registry.register(_FastAgent(ctx.bus))

    async def scenario() -> tuple[dict, list]:
        q = ctx.bus.subscribe("task.*")
        result = ctx.dispatch_task("fast", "one instruction")
        events = []
        while True:
            event = await asyncio.wait_for(q.get(), timeout=1.0)
            events.append(event)
            if event.type is EventType.TASK_COMPLETED:
                break
        return result, events

    result, events = asyncio.run(scenario())
    kinds = {e.type for e in events}
    assert EventType.TASK_QUEUED in kinds
    assert EventType.TASK_STARTED in kinds
    assert EventType.TASK_COMPLETED in kinds
    assert all(e.data["id"] == result["task_id"] for e in events)


# ----------------------------------------------------------------------
# build_mcp_server (requires the mcp SDK)
# ----------------------------------------------------------------------
def test_build_mcp_server_registers_four_tools(
    ctx: DVoiceMCPContext,
) -> None:
    pytest.importorskip("mcp")
    server = build_mcp_server(ctx)
    lister = getattr(server, "list_tools", None)
    if lister is None:  # FastMCP internal API drifted - build at least worked
        return
    tools = asyncio.run(lister()) if asyncio.iscoroutinefunction(lister) else lister()
    names = {t.name for t in tools}
    assert {"speak", "report_progress", "get_status", "dispatch_task"} <= names


def test_mcp_server_has_no_authorization_tools(ctx: DVoiceMCPContext) -> None:
    """Design decision: no approval/confirmation tools are ever exposed."""
    pytest.importorskip("mcp")
    server = build_mcp_server(ctx)
    lister = getattr(server, "list_tools", None)
    if lister is None:
        return
    tools = asyncio.run(lister()) if asyncio.iscoroutinefunction(lister) else lister()
    names = {t.name for t in tools}
    assert not any(
        keyword in name
        for name in names
        for keyword in ("confirm", "approve", "authoriz", "permission")
    )
