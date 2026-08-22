"""D-VOICE MCP server: proactive voice narration for MCP-capable coding agents.

What this is
------------
D-VOICE (Vocalis) packaged as a Model Context Protocol server. Any MCP-capable
coding agent (Claude Code, Codex, opencode, ...) connects over stdio and calls
these tools so it can *actively speak up*: narrate progress, announce results,
dispatch work through the agent registry. This inverts the classic pattern -
the agent reports to the user by voice instead of the user passively polling
a dashboard (competitive-analysis finding G8: the agent that speaks first wins).

Design decision: NO authorization / confirmation tools
------------------------------------------------------
This server deliberately does NOT implement any request_confirmation /
approve-style tools. Spoken authorization ("say yes to continue") is far too
casual a channel for security-relevant decisions - easy to mishear, easy to
spoof, and it leaves no audit trail. Authorization stays in the agent's
native UI; D-VOICE is a narration layer, never an approval authority.

Run (stdio transport)::

    python -m vocalis.server.mcp

Requires the ``mcp`` extra::

    pip install 'vocalis-voice-agent[mcp]'

The ``mcp`` SDK is imported lazily inside :func:`build_mcp_server`, so this
module (and everything that imports it) works on machines without the SDK.
Both the mcp 1.x line (``mcp.server.fastmcp.FastMCP``) and mcp 2.x
(``mcp.server.mcpserver.MCPServer``, FastMCP's renamed successor with the
same tool/run/list_tools surface) are supported.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import TYPE_CHECKING, Any

from vocalis.agents.base import TaskRecord
from vocalis.agents.registry import AgentRegistry, build_default_registry
from vocalis.config import VocalisConfig
from vocalis.dvoice.assistant import DVoiceBrain
from vocalis.server.events import EventBus, EventType, bus
from vocalis.voice.tts import TTSService

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from mcp.server.fastmcp import FastMCP

logger = logging.getLogger("vocalis.server.mcp")


class DVoiceMCPContext:
    """Headless component assembly for the MCP server.

    Mirrors ``AppState`` in :mod:`vocalis.server.app` (config / registry /
    TTS / brain wired onto one event bus) but without FastAPI and without
    the TaskMonitor polling loop: under MCP the *agent* calls
    ``report_progress`` / ``speak`` proactively, so there is nothing to poll.

    Every method is a plain callable (async where the underlying APIs are)
    so the whole context is unit-testable without an MCP client or SDK.
    """

    def __init__(
        self,
        config: VocalisConfig | None = None,
        event_bus: EventBus | None = None,
        registry: AgentRegistry | None = None,
        tts: TTSService | None = None,
        brain: DVoiceBrain | None = None,
    ) -> None:
        self.config = config or VocalisConfig.load()
        self.bus = event_bus or bus
        self.registry = registry or build_default_registry(self.bus, self.config)
        self.tts = tts or TTSService(self.config, self.bus)
        self.brain = brain or DVoiceBrain(self.config, self.registry, self.bus)
        # Fire-and-forget dispatch tasks. Kept referenced so the GC never
        # cancels a running agent task mid-flight.
        self._background_tasks: set[asyncio.Task[TaskRecord]] = set()
        # TaskRecords finished via dispatch_task (bounded log, newest last).
        self.completed: list[TaskRecord] = []

    # ------------------------------------------------------------------
    # speak
    # ------------------------------------------------------------------
    async def speak(
        self,
        text: str,
        profile: str | None = None,
        agent: str | None = None,
    ) -> dict[str, Any]:
        """Speak ``text`` aloud and return the synthesis result.

        Voice profile resolution: explicit ``profile`` > per-agent mapping
        (``tts.profile_for_agent``) > ``default_profile``. Publishes a
        ``dvoice.saying`` event first so the HUD mirrors what is spoken.

        On headless machines playback may fail outright (no audio device);
        that is caught and degraded to synthesis-only so the caller still
        receives ``audio_path`` and knows synthesis itself succeeded.
        """
        if not text or not text.strip():
            raise ValueError("text must be a non-empty string")

        profile_name = profile
        if profile_name is None and agent is not None:
            profile_name = self.tts.profile_for_agent(agent)

        await self.bus.publish(EventType.DVOICE_SAYING, text=text)

        play_error: str | None = None
        try:
            result = await self.tts.speak(text, profile_name=profile_name, play=True)
        except Exception as e:  # playback exploded (headless box): synth only
            play_error = str(e)
            logger.warning("playback failed (%s); falling back to synthesis-only", e)
            result = await self.tts.synthesize(text, profile_name=profile_name)

        payload: dict[str, Any] = {
            "ok": bool(result.ok),
            "audio_path": str(result.audio_path) if result.audio_path else None,
            "profile": result.profile or profile_name or self.config.tts.default_profile,
            "characters": result.characters or len(text),
        }
        if play_error is None:
            payload["played"] = True
        else:
            payload["played"] = False
            payload["play_error"] = play_error
        if not result.ok:
            payload["error"] = result.error or "synthesis failed"
        return payload

    # ------------------------------------------------------------------
    # report_progress
    # ------------------------------------------------------------------
    async def report_progress(
        self,
        task_id: str,
        agent: str,
        progress: float,
        step: str = "",
        note: str = "",
    ) -> dict[str, Any]:
        """Publish a ``task.progress`` event for the narration pipeline.

        This is the G8 entry point: an agent proactively reports where it
        is, and D-VOICE turns the event into spoken narration. ``progress``
        is clamped to [0, 1].
        """
        progress = min(1.0, max(0.0, float(progress)))
        await self.bus.publish(
            EventType.TASK_PROGRESS,
            task_id=task_id,
            agent=agent,
            progress=progress,
            current_step=step,
            note=note,
        )
        return {"ok": True, "task_id": task_id, "progress": progress, "narrated": True}

    # ------------------------------------------------------------------
    # get_status
    # ------------------------------------------------------------------
    def get_status(self) -> dict[str, Any]:
        """Synchronous status snapshot (no brain probe).

        Contains registry snapshot (agents / active_tasks / recent), the
        current default voice profile, and the registered profile names.
        Tests use this path; ``get_status_async`` adds brain availability.
        """
        snapshot = self.registry.snapshot()
        return {
            **snapshot,
            "default_profile": self.config.tts.default_profile,
            "profiles": sorted(self.tts.profiles()),
        }

    async def get_status_async(self) -> dict[str, Any]:
        """Full status snapshot including brain availability (async probe)."""
        status = self.get_status()
        try:
            status["brain_available"] = await asyncio.wait_for(
                self.brain.available(), timeout=3.0
            )
        except Exception:
            status["brain_available"] = False
        return status

    # ------------------------------------------------------------------
    # dispatch_task
    # ------------------------------------------------------------------
    def dispatch_task(self, agent: str, instruction: str) -> dict[str, Any]:
        """Queue ``instruction`` on ``agent`` (fire-and-forget background task).

        Requires a running event loop (MCP tools run inside one). Returns
        immediately with the pre-created task id; lifecycle events
        (queued/started/progress/completed) flow through the bus with that
        same id, mirroring ``AgentRegistry.dispatch``.
        """
        if agent not in self.registry.connectors:
            available = sorted(self.registry.connectors)
            return {
                "ok": False,
                "error": f"agent '{agent}' not registered - available: {available}",
                "available_agents": available,
            }
        if not instruction or not instruction.strip():
            return {"ok": False, "error": "instruction must be a non-empty string"}

        connector = self.registry.connectors[agent]
        # Create the record up-front so queued/started/progress events all
        # carry the same id (same trick as AgentRegistry.dispatch).
        record = TaskRecord(agent=agent, instruction=instruction)

        async def _run() -> TaskRecord:
            await self.bus.publish(EventType.TASK_QUEUED, **record.to_dict())
            result = await connector.run(instruction, record=record)
            self.registry.history.append(result)
            self.registry.history = self.registry.history[-100:]
            return result

        task = asyncio.create_task(_run())
        self._background_tasks.add(task)
        task.add_done_callback(self._on_task_done)
        return {"ok": True, "task_id": record.id, "agent": agent, "queued": True}

    def _on_task_done(self, task: asyncio.Task[TaskRecord]) -> None:
        """Bookkeeping for finished background dispatches."""
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.warning("background agent task failed: %s", exc)
            return
        self.completed.append(task.result())
        self.completed = self.completed[-100:]


# ----------------------------------------------------------------------
# Server construction (lazy SDK import)
# ----------------------------------------------------------------------
def _load_mcp_server_cls() -> type:
    """Return the high-level MCP server class for whichever SDK is installed.

    * mcp 1.x: ``mcp.server.fastmcp.FastMCP``
    * mcp 2.x: ``mcp.server.mcpserver.MCPServer`` (FastMCP's renamed
      successor - same ``@server.tool()`` / ``run()`` / ``list_tools()``
      surface, so both are used identically here)

    Raises ``RuntimeError`` with install instructions when the ``mcp`` SDK
    is missing or broken.
    """
    try:
        from mcp.server.fastmcp import FastMCP

        return FastMCP
    except ImportError:
        pass
    try:
        from mcp.server.mcpserver import MCPServer

        return MCPServer
    except ImportError as e:
        try:
            import mcp  # noqa: F401

            hint = (
                "A broken 'mcp' install was found - try: "
                "pip install --force-reinstall 'vocalis-voice-agent[mcp]'"
            )
        except ImportError:
            hint = (
                "Install it with: pip install 'vocalis-voice-agent[mcp]' "
                "(or plain: pip install mcp)"
            )
        raise RuntimeError(f"The D-VOICE MCP server requires the 'mcp' SDK. {hint}") from e


def build_mcp_server(ctx: DVoiceMCPContext) -> "FastMCP":
    """Build a FastMCP server exposing the four D-VOICE tools.

    On mcp 2.x the returned object is ``MCPServer`` (API-compatible
    successor of FastMCP). Raises ``RuntimeError`` with install
    instructions when the ``mcp`` SDK is missing (it is imported lazily so
    the module itself always loads).
    """
    server_cls = _load_mcp_server_cls()
    server: "FastMCP" = server_cls(
        "dvoice",
        instructions=(
            "D-VOICE 语音播报服务 / D-VOICE narration server. "
            "Use these tools to speak to the user proactively (progress, "
            "results, alerts) instead of making them poll a screen. "
            "NOTE: this server intentionally offers NO authorization tools - "
            "approvals stay in your native agent UI; D-VOICE only narrates."
        ),
    )

    @server.tool()
    async def speak(
        text: str, profile: str | None = None, agent: str | None = None
    ) -> dict[str, Any]:
        """让 D-VOICE 立即开口说话 / Speak text aloud to the user now.

        在里程碑、最终结果、需要用户注意（如等待输入）时调用，让用户
        不看终端也能听到进展。文本建议口语化、3 句以内，中英文皆可。
        音色解析顺序：显式 profile > agent 专属音色 > 默认音色。
        无音频设备的机器上播放失败不影响合成结果（仍返回 audio_path）。

        Use when there is something the user should *hear* without watching
        the terminal: long-running milestones, final results, blocked-on-you
        alerts. Returns ok / audio_path / profile / characters.
        """
        return await ctx.speak(text, profile=profile, agent=agent)

    @server.tool()
    async def report_progress(
        task_id: str, agent: str, progress: float, step: str = "", note: str = ""
    ) -> dict[str, Any]:
        """汇报任务进度，D-VOICE 会语音播报 / Report task progress to be narrated.

        在有意义的节点调用（编译通过、测试过半、部署完成），不要每个
        小步骤都调。progress 取值 0..1（超出会自动收敛到边界），
        step/note 会成为播报内容。这是"主动汇报而非被动轮询"的入口。

        Call at meaningful milestones so the user hears progress while away
        from the screen. Progress is clamped to [0,1]; step is the current
        step name, note an optional one-liner for narration.
        """
        return await ctx.report_progress(task_id, agent, progress, step=step, note=note)

    @server.tool()
    async def get_status() -> dict[str, Any]:
        """查询 D-VOICE 系统状态 / Get a full status snapshot.

        返回已注册 agents、活跃与最近任务、默认音色、可用音色 profiles、
        本地 brain 是否可用。会话开始时调用一次，或派发任务后查询结果。

        Returns registered agents, active/recent tasks, the default voice
        profile, available profiles, and local brain availability. Good to
        call once at session start.
        """
        return await ctx.get_status_async()

    @server.tool()
    async def dispatch_task(agent: str, instruction: str) -> dict[str, Any]:
        """派发任务给已注册的 agent / Dispatch a task to a registered agent.

        后台异步执行，立即返回 task_id（不阻塞）。任务生命周期事件会
        进入事件总线；用 get_status 查询后续状态。传入未注册的 agent
        会返回可用 agent 列表。

        Fire-and-forget: returns a task id immediately while the connector
        runs in the background. Query outcomes via get_status.
        """
        return ctx.dispatch_task(agent, instruction)

    return server


def main() -> None:
    """Entry point: assemble the context and run the MCP server over stdio."""
    # stdout is the MCP stdio protocol channel - every log must go to stderr.
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    ctx = DVoiceMCPContext()
    server = build_mcp_server(ctx)
    logger.info("D-VOICE MCP server starting (stdio transport)")
    server.run()  # stdio by default


if __name__ == "__main__":
    main()
