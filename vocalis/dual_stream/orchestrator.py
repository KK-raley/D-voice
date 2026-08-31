"""DualStreamOrchestrator——双流协调器（无 UI 文本展示流）。

架构（与旧三流架构不同，砍掉 text 展示流，只留两条流）::

    LLM 文本增量 ──┐                          ┌─► VoiceStream（流式 TTS + 播放）
                   ├─► DualStreamOrchestrator ─┤
    工具调用      ──┘                          └─► ToolStream（后台任务队列）

协调规则：
1. **平行运转**：两流各自独立工作，互不 await、互不阻塞；工具执行
   期间语音流的事件循环照常运转（见 ``pause_on_tool=False`` 用法）。
2. **自然停顿**：提交工具且 ``pause_on_tool=True``（默认）时，语音流
   进入软停顿——可选先播思考提示音 / filler 短语（"嗯……"），像真人
   "想一下再说"，而非冷场。停顿时长策略见 :mod:`vocalis.dual_stream.pacing`。
3. **结果回灌**：全部在途工具结束后语音自动恢复，工具结果文本
   （``announce``）作为语音继续播报——"工具结果出来后语音继续播报"。
4. **barge-in**：用户打断只停语音（清空待播内容），工具流照常执行
   （持续工作）；默认丢弃未播报的 announce，避免打断后抢话。

事件：工具流复用既有 ``task.*`` 事件；语音流发 ``dualstream.voice.*``；
会话级发 ``dualstream.session.*``。全部经 :class:`~vocalis.server.events.EventBus`
广播，HUD / monitor / notifier 可直接订阅。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from vocalis.dual_stream.pacing import PacingPolicy
from vocalis.dual_stream.tool_stream import ToolFactory, ToolStream
from vocalis.dual_stream.voice_stream import VoiceStream
from vocalis.server.events import Event, EventBus, EventType
from vocalis.server.events import bus as events_bus

logger = logging.getLogger("vocalis.dual_stream.orchestrator")

# -- 会话级事件 -----------------------------------------------------------
SESSION_STARTED = "dualstream.session.started"      #: 双流会话已启动
SESSION_STOPPED = "dualstream.session.stopped"      #: 双流会话已关闭
SESSION_INTERRUPTED = "dualstream.session.interrupted"  #: 用户打断（barge-in）


class DualStreamOrchestrator:
    """协调 VoiceStream 与 ToolStream 的生命周期与交互。

    参数：
        voice:          语音流（缺省新建，默认 Edge-TTS + 真实播放）。
        tools:          工具流（缺省新建，默认 2 个 worker）。
        bus:            事件总线；三条流共用一条（缺省全局单例）。
        pacing:         自然停顿策略（filler / 提示音 / 时长）。
        pause_on_tool:  提交工具时是否让语音进入自然停顿（默认 True；
                        设为 False 即"两流完全并行"模式，适合播报与
                        工具互不相关的场景）。

    典型用法::

        orch = DualStreamOrchestrator()
        await orch.start()
        orch.feed_delta("我查一下天气。")        # LLM 增量 -> 语音流
        orch.submit_tool("weather", fetch, announce="今天晴，25 度。")
        ...                                     # 工具执行期间语音自然停顿
        orch.interrupt()                        # 用户 barge-in：停播不停工具
        await orch.shutdown()
    """

    def __init__(
        self,
        voice: VoiceStream | None = None,
        tools: ToolStream | None = None,
        bus: EventBus | None = None,
        pacing: PacingPolicy | None = None,
        pause_on_tool: bool = True,
    ) -> None:
        self.bus = bus or events_bus
        self.voice = voice or VoiceStream(bus=self.bus)
        self.tools = tools or ToolStream(bus=self.bus)
        self.pacing = pacing or PacingPolicy()
        self.pause_on_tool = pause_on_tool
        self._pending = 0  # 在途工具数（用于"全部完成才恢复"）
        self._announcements: dict[str, str | None] = {}  # task_id -> 结果播报文本
        self._started = False
        self._emit_tasks: set[Any] = set()

    # -- 生命周期 ------------------------------------------------------
    async def start(self) -> None:
        """启动两条流并注册工具完成回调（幂等）。"""
        if self._started:
            return
        self._started = True
        self.voice.start()
        self.tools.start()
        # 工具结果 -> 语音恢复/播报：经 EventBus 解耦（task.* 是既有事件词汇）。
        self.bus.on(EventType.TASK_COMPLETED.value, self._on_tool_done)
        self.bus.on(EventType.TASK_FAILED.value, self._on_tool_done)
        await self.bus.publish(SESSION_STARTED)
        logger.info("dual-stream session started (pause_on_tool=%s)", self.pause_on_tool)

    async def shutdown(self, timeout: float = 5.0) -> None:
        """先排空工具队列（带超时），再停语音流。"""
        if not self._started:
            return
        self._started = False
        # 注销 handler：同一 bus 上反复 start/shutdown 不再累积幽灵回调。
        self.bus.off(EventType.TASK_COMPLETED.value, self._on_tool_done)
        self.bus.off(EventType.TASK_FAILED.value, self._on_tool_done)
        await self.tools.shutdown(timeout)
        await self.voice.stop()
        await self.bus.publish(SESSION_STOPPED)
        logger.info("dual-stream session stopped")

    # -- 语音 API（透传给 VoiceStream） ---------------------------------
    def say(self, text: str) -> None:
        """让语音流说一段完整文本。"""
        self.voice.say(text)

    def feed_delta(self, delta: str) -> None:
        """送入 LLM 流式输出的一段增量文本。"""
        self.voice.feed(delta)

    def flush(self) -> None:
        """通知语音流：本轮文本输入结束（残句也放行合成）。"""
        self.voice.flush()

    # -- 工具 API -------------------------------------------------------
    def submit_tool(
        self,
        name: str,
        factory: ToolFactory,
        *,
        announce: str | None = None,
        expected_wait_s: float | None = None,
    ) -> str:
        """提交一个工具作业；按策略让语音进入自然停顿。

        参数：
            name:            工具名（事件里展示）。
            factory:         无参协程工厂，返回 ``Awaitable``（执行体）。
            announce:        工具完成后要语音播报的结果文本；None 不播报。
            expected_wait_s: 预计耗时（秒），供停顿策略取时长。

        返回：
            task_id（可用于 :attr:`ToolStream.results` 查询）。
        """
        task_id = self.tools.submit(name, factory)
        # 哨兵登记：无论 announce 是否为 None 都写入 key，_on_tool_done
        # 据此区分"本会话提交的任务"与共享 bus 上的外部 task 事件。
        self._announcements[task_id] = announce
        if self.pause_on_tool:
            first_tool = self._pending == 0
            self._pending += 1
            if first_tool:
                # 首个工具触发自然停顿：提示音 -> filler -> 软停顿
                # （都在 pause() 之前入队，遵循 VoiceStream 的 drain 契约）。
                plan = self.pacing.plan_pause(expected_wait_s)
                if plan.chime_audio is not None:
                    self.voice.play_raw(plan.chime_audio)
                if plan.filler:
                    self.voice.say(plan.filler)
                self.voice.pause()
        return task_id

    # -- 打断 -----------------------------------------------------------
    def interrupt(self, reason: str = "barge-in", *, drop_announcements: bool = True) -> None:
        """用户 barge-in：立即停播语音；工具流不受影响、持续执行。

        参数：
            reason:             打断原因（记入事件，便于诊断）。
            drop_announcements: 默认 True——丢弃尚未播报的工具结果文本，
                                避免打断之后系统又抢话。
        """
        self.voice.interrupt()
        if drop_announcements:
            self._announcements.clear()
        self._pending = 0  # 打断即接管：工具完成不再触发"恢复+播报"
        self._emit(SESSION_INTERRUPTED, reason=reason)
        logger.info("interrupted (%s): voice dropped, tools keep running", reason)

    # -- 内部 -----------------------------------------------------------
    async def _on_tool_done(self, event: Event) -> None:
        """task.completed / task.failed 的回调：恢复语音并播报结果。"""
        if not self._started:
            return  # 已 shutdown（迟到事件）或尚未 start：防御性忽略
        task_id = str(event.data.get("task_id", ""))
        if not task_id or task_id not in self._announcements:
            # 非本协调器提交的任务（共享 bus 上其他 agent 的 task 事件）：
            # 忽略，防止外部事件伪造语音恢复/播报。
            return
        announce = self._announcements.pop(task_id, None)
        if not self.pause_on_tool:
            if announce:
                self.voice.say(announce)
            return
        if self._pending > 0:
            self._pending -= 1
        # 全部在途工具结束才恢复语音（多工具并发时不提前抢话）。
        if self._pending == 0 and self.voice.paused:
            self.voice.resume()
        if announce:
            self.voice.say(announce)

    def _emit(self, type_: str, **data: Any) -> None:
        """在同步上下文里发布事件的辅助函数（保存任务引用防 GC）。"""
        task = asyncio.create_task(self.bus.publish(type_, **data))
        self._emit_tasks.add(task)
        task.add_done_callback(self._emit_tasks.discard)
