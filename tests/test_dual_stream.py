"""双流架构（dual-stream）离线单测：mock TTS 引擎与播放后端，无网络、无音频硬件。

覆盖四组核心契约（对应双流架构需求）：
1. 双流并行不互相阻塞（工具慢不卡语音首包；语音长不卡工具完成）。
2. 工具执行期间语音进入自然停顿（停顿期间绝无新播放）。
3. 工具结果触发语音恢复，并播报 announce 结果文本（事件顺序可断言）。
4. barge-in 立即停播且清空待播内容，同时工具流持续工作不受影响。

另含 PacingPolicy 纯策略测试（时长 clamp / filler 轮换 / 提示音阈值）
与 filler 集成测试（"嗯……"先于停顿说出来）。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from vocalis.dual_stream import (
    DualStreamOrchestrator,
    PacingPolicy,
    ToolStream,
    VoiceStream,
)
from vocalis.dual_stream.voice_stream import (
    VOICE_INTERRUPTED,
    VOICE_PAUSED,
    VOICE_RESUMED,
)
from vocalis.server.events import Event, EventBus, EventType
from vocalis.voice.tts import TTSEngine, VoiceProfile


# ---------------------------------------------------------------------
# fakes / helpers
# ---------------------------------------------------------------------
class FakeEngine(TTSEngine):
    """假 TTS 引擎：文本 -> ``AUDIO[文本]`` 字节，可配置合成延迟（默认 0）。"""

    name = "fake"

    def __init__(self, delay_s: float = 0.0) -> None:
        self.delay_s = delay_s

    async def synthesize(self, text: str, profile: VoiceProfile) -> bytes:
        if self.delay_s > 0:
            await asyncio.sleep(self.delay_s)
        return f"AUDIO[{text}]".encode()


class FakeAudioSink:
    """假播放后端：每个片段"播" chunk_s 秒，期间可被 stop_now 掐断。"""

    def __init__(self, chunk_s: float = 0.05) -> None:
        self.chunk_s = chunk_s
        self.calls: list[dict[str, Any]] = []  # {"audio", "start", "interrupted"}
        self.stops = 0
        self._stop_flag = False

    async def play_chunk(self, audio: bytes) -> None:
        record: dict[str, Any] = {
            "audio": audio.decode("utf-8"),
            "start": time.monotonic(),
            "interrupted": False,
        }
        self.calls.append(record)
        self._stop_flag = False
        deadline = time.monotonic() + self.chunk_s
        while time.monotonic() < deadline:
            if self._stop_flag:  # barge-in：毫秒级退出播放
                record["interrupted"] = True
                break
            await asyncio.sleep(0.005)

    def stop_now(self) -> None:
        self._stop_flag = True
        self.stops += 1

    def completed(self) -> list[dict[str, Any]]:
        """完整播完（未被掐断）的片段。"""
        return [c for c in self.calls if not c["interrupted"]]


@contextlib.asynccontextmanager
async def _session(
    chunk_s: float = 0.05,
    pacing: PacingPolicy | None = None,
    pause_on_tool: bool = True,
    synth_delay_s: float = 0.0,
):
    """搭建一套隔离的双流环境：独立 bus + fake 引擎/sink，随用随收。"""
    bus = EventBus()
    sink = FakeAudioSink(chunk_s=chunk_s)
    voice = VoiceStream(engine=FakeEngine(delay_s=synth_delay_s), sink=sink, bus=bus)
    tools = ToolStream(bus=bus)
    orch = DualStreamOrchestrator(
        voice=voice, tools=tools, bus=bus, pacing=pacing, pause_on_tool=pause_on_tool
    )
    await orch.start()
    try:
        yield SimpleNamespace(orch=orch, voice=voice, tools=tools, bus=bus, sink=sink)
    finally:
        await orch.shutdown()


async def _wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> bool:
    """轮询等待谓词成真；超时返回 False（不抛异常，让断言给出清晰差异）。"""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.01)
    return True


def _drain(queue: asyncio.Queue[Event]) -> list[Event]:
    """非阻塞取出事件队列中的全部事件。"""
    out: list[Event] = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


# ---------------------------------------------------------------------
# PacingPolicy：纯策略（停顿时长 / filler / 提示音）
# ---------------------------------------------------------------------
def test_pacing_plan_pause_clamps_duration() -> None:
    """停顿时长 clamp 到 [min, max]；未知等待用默认值。"""
    policy = PacingPolicy(min_pause_s=1.0, max_pause_s=3.0, default_pause_s=2.0)
    assert policy.plan_pause(None).pause_s == 2.0  # 未知 -> 默认
    assert policy.plan_pause(0.2).pause_s == 1.0  # 过短 -> 抬到下限
    assert policy.plan_pause(10.0).pause_s == 3.0  # 过长 -> 压到上限
    assert policy.plan_pause(1.7).pause_s == 1.7  # 区间内原样保留


def test_pacing_filler_rotation_and_thresholds() -> None:
    """filler 按配置顺序轮换；filler 与提示音各有触发阈值。"""
    policy = PacingPolicy(
        filler_phrases=("嗯……", "让我想想……"),
        filler_min_pause_s=1.5,
        chime_audio=b"ding",
        chime_min_pause_s=2.5,
    )
    short = policy.plan_pause(1.0)  # 短停顿：安静
    assert short.filler is None and short.chime_audio is None
    mid = policy.plan_pause(2.0)  # 中停顿：只说 filler
    assert mid.filler == "嗯……" and mid.chime_audio is None
    long_wait = policy.plan_pause(3.0)  # 长停顿：filler 轮换 + 提示音
    assert long_wait.filler == "让我想想……" and long_wait.chime_audio == b"ding"


# ---------------------------------------------------------------------
# 契约 1：双流并行，互不阻塞
# ---------------------------------------------------------------------
async def test_parallel_streams_do_not_block_each_other() -> None:
    """慢工具不卡语音首包；长语音不卡工具完成。"""
    async with _session(chunk_s=0.05, pause_on_tool=False) as s:
        tool_done_at: list[float] = []
        s.bus.on(
            EventType.TASK_COMPLETED.value, lambda _e: tool_done_at.append(time.monotonic())
        )

        async def slow_tool() -> str:
            await asyncio.sleep(0.3)
            return "done"

        s.orch.submit_tool("slow", slow_tool)
        s.orch.say("你好。欢迎回来。今天天气不错。")  # 3 句，每句约 0.05s

        # 语音首包先于 0.3s 慢工具完成 -> 工具执行没有阻塞语音流
        assert await _wait_until(lambda: s.sink.calls, 2.0)
        first_play_start = s.sink.calls[0]["start"]
        assert await _wait_until(lambda: bool(tool_done_at), 2.0)
        assert first_play_start < tool_done_at[0]
        # 语音整段播完，流水线未被工具卡死
        assert await s.voice.wait_idle(2.0)
        assert len(s.sink.completed()) == 3

    async with _session(chunk_s=0.12, pause_on_tool=False) as s:
        tool_done_at = []
        s.bus.on(
            EventType.TASK_COMPLETED.value, lambda _e: tool_done_at.append(time.monotonic())
        )

        async def quick_tool() -> str:
            await asyncio.sleep(0.03)
            return "quick"

        s.orch.say("第一句。第二句。第三句。")  # 3 句 * 0.12s ≈ 0.36s
        s.orch.submit_tool("quick", quick_tool)
        # 工具在语音还没说完时已完成 -> 语音没有阻塞工具流
        assert await _wait_until(lambda: len(s.sink.calls) >= 3, 2.0)
        last_play_start = s.sink.calls[2]["start"]
        assert await _wait_until(lambda: bool(tool_done_at), 2.0)
        assert tool_done_at[0] < last_play_start


# ---------------------------------------------------------------------
# 契约 2：工具执行期间语音自然停顿
# ---------------------------------------------------------------------
async def test_voice_pauses_while_tool_running() -> None:
    """工具在途时语音停在句边界，停顿期间绝无新播放。"""
    async with _session(chunk_s=0.03) as s:
        s.orch.say("我先说一句话。")
        assert await s.voice.wait_idle(2.0)

        started = asyncio.Event()
        release = asyncio.Event()

        async def blocked_tool() -> str:
            started.set()
            await release.wait()  # 工具一直不完成
            return "ok"

        s.orch.submit_tool("blocked", blocked_tool)
        assert await _wait_until(started.is_set, 2.0)

        # 语音进入自然停顿并停稳
        assert s.voice.paused
        assert await s.voice.wait_paused(2.0)

        n_before = len(s.sink.calls)
        await asyncio.sleep(0.15)  # 停顿持续一段时间
        assert len(s.sink.calls) == n_before  # 期间无任何新播放（非冷场亦非念稿）

        release.set()
        assert await s.tools.wait_all(2.0)
        # 工具结果触发语音恢复
        assert await _wait_until(lambda: not s.voice.paused, 2.0)


async def test_pause_contract_blocks_post_pause_content() -> None:
    """软停顿契约内建为机制：停顿期间入队的新内容不抢播，resume 后才播。"""
    async with _session(chunk_s=0.03) as s:
        started = asyncio.Event()
        release = asyncio.Event()

        async def blocked_tool() -> str:
            started.set()
            await release.wait()  # 工具一直不完成
            return "ok"

        s.orch.submit_tool("blocked", blocked_tool)
        assert await _wait_until(started.is_set, 2.0)
        assert await s.voice.wait_paused(2.0)  # 语音已停稳

        # 停顿期间违规入队新内容（契约本不允许，机制兜底）
        s.voice.play_raw(b"AUDIO[late-chime]")
        await asyncio.sleep(0.15)
        assert not any("late-chime" in c["audio"] for c in s.sink.calls)  # 不抢播

        release.set()  # 工具完成 -> resume
        assert await s.tools.wait_all(2.0)
        assert await _wait_until(
            lambda: any("late-chime" in c["audio"] for c in s.sink.calls), 2.0
        )  # 恢复后才播出


# ---------------------------------------------------------------------
# 契约 3：工具结果触发语音恢复 + 结果播报
# ---------------------------------------------------------------------
async def test_tool_result_resumes_voice_and_announces() -> None:
    """工具完成后：语音恢复、播报 announce，事件顺序 PAUSED→DONE→RESUMED→播报。"""
    async with _session(chunk_s=0.03) as s:
        events_q = s.bus.subscribe("*")
        s.orch.say("我来查一下天气。")
        assert await s.voice.wait_idle(2.0)

        async def fetch_weather() -> str:
            await asyncio.sleep(0.1)
            return "晴"

        s.orch.submit_tool("weather", fetch_weather, announce="查到了，今天晴。")

        # 工具在途：停顿中，且 announce 尚未播报
        await asyncio.sleep(0.04)
        assert s.voice.paused
        assert not any("查到了" in c["audio"] for c in s.sink.calls)

        # 工具结果出来后，语音继续播报结果文本
        assert await _wait_until(
            lambda: any("查到了" in c["audio"] for c in s.sink.calls), 2.0
        )
        assert await s.voice.wait_idle(2.0)
        assert not s.voice.paused

        # 事件顺序：自然停顿 -> 工具完成 -> 语音恢复 -> 结果播报(TTS_SPEAKING)
        await asyncio.sleep(0.05)  # 等事件全部入队
        events = _drain(events_q)
        types = [e.type for e in events]
        assert types.index(VOICE_PAUSED) < types.index(EventType.TASK_COMPLETED)
        assert types.index(EventType.TASK_COMPLETED) < types.index(VOICE_RESUMED)
        announce_evt = next(
            e
            for e in events
            if e.type == EventType.TTS_SPEAKING and e.data.get("text") == "查到了，今天晴。"
        )
        assert events.index(announce_evt) > types.index(VOICE_RESUMED)


async def test_external_task_events_do_not_disturb_pause_resume() -> None:
    """共享 bus 上外部 agent 的 task.completed 不得伪造语音恢复。"""
    async with _session(chunk_s=0.03) as s:
        s.orch.say("我先说一句话。")
        assert await s.voice.wait_idle(2.0)

        started = asyncio.Event()
        release = asyncio.Event()

        async def blocked_tool() -> str:
            started.set()
            await release.wait()  # 本会话工具一直不完成
            return "ok"

        s.orch.submit_tool("blocked", blocked_tool)
        assert await _wait_until(started.is_set, 2.0)
        assert await s.voice.wait_paused(2.0)  # 本会话工具在途：停顿生效

        # 外部 agent 在共享 bus 上广播 task.completed（task_id 与本会话无关）
        n_before = len(s.sink.calls)
        await s.bus.publish(
            EventType.TASK_COMPLETED, task_id="external-42", name="other-agent", value="x"
        )
        await asyncio.sleep(0.1)
        assert s.voice.paused  # 语音不得被外部事件"恢复"
        assert len(s.sink.calls) == n_before  # 也没有抢话播放

        release.set()  # 本会话自己的工具完成，才允许恢复
        assert await s.tools.wait_all(2.0)
        assert await _wait_until(lambda: not s.voice.paused, 2.0)


# ---------------------------------------------------------------------
# 契约 4：barge-in 停播（且不停工具）
# ---------------------------------------------------------------------
async def test_barge_in_stops_playback_immediately() -> None:
    """打断瞬间掐断当前播放、丢弃待播队列，语音流回到就绪态。"""
    async with _session(chunk_s=0.4) as s:  # 每句"播" 0.4s，共 3 句
        events_q = s.bus.subscribe("*")
        s.orch.say("第一句。第二句。第三句。")
        assert await _wait_until(lambda: s.sink.calls, 2.0)  # 首句开播

        s.orch.interrupt(reason="user-spoke")

        await asyncio.sleep(0.15)
        assert s.sink.stops >= 1  # 当前播放被硬停
        assert s.sink.calls[0]["interrupted"] is True
        n_after_interrupt = len(s.sink.calls)

        await asyncio.sleep(0.25)  # 足够剩下两句播完（若未被丢弃）
        assert len(s.sink.calls) == n_after_interrupt  # 但一句都没有再播
        assert await s.voice.wait_idle(1.0)  # 队列已清空
        assert not s.voice.paused
        assert VOICE_INTERRUPTED in [e.type for e in _drain(events_q)]


async def test_barge_in_keeps_tools_running() -> None:
    """打断只停语音：工具照常执行完成，且不抢话播报旧结果。"""
    async with _session(chunk_s=0.05) as s:
        async def long_tool() -> str:
            await asyncio.sleep(0.25)
            return "result"

        s.orch.submit_tool("long", long_tool, announce="不该被播报。")
        await asyncio.sleep(0.02)
        s.orch.interrupt()  # 用户抢话

        assert await s.tools.wait_all(2.0)  # 工具流不受影响，持续工作
        assert len(s.tools.results) == 1
        assert s.tools.results[0].ok is True

        await asyncio.sleep(0.15)  # 若 announce 未被丢弃，这里就会抢话
        assert not any("不该被播报" in c["audio"] for c in s.sink.calls)


async def test_barge_in_during_synthesis_drops_stale_audio() -> None:
    """合成进行中 barge-in：迟到的旧音频不得入队播出。"""
    async with _session(chunk_s=0.03, synth_delay_s=0.2) as s:
        s.orch.say("这句还在合成。")  # 进入合成管线，0.2s 后才产出音频
        await asyncio.sleep(0.05)  # 确保此刻合成仍在进行、尚未完成
        s.orch.interrupt(reason="user-spoke")

        # 足够旧合成完成（若 seq 校验失效，旧音频就会入队并播出）
        await asyncio.sleep(0.3)
        assert not any("这句还在合成" in c["audio"] for c in s.sink.calls)
        assert await s.voice.wait_idle(1.0)  # 管线排空，无残留


# ---------------------------------------------------------------------
# 集成：自然停顿 + filler（"像真人思考，而非冷场"）
# ---------------------------------------------------------------------
async def test_natural_pause_with_filler() -> None:
    """提交工具后先说 filler 再进入停顿；结果就绪后自动恢复。"""
    pacing = PacingPolicy(
        filler_phrases=("嗯……", "让我想想……"),
        filler_min_pause_s=1.0,
        default_pause_s=2.0,
    )
    async with _session(chunk_s=0.03, pacing=pacing) as s:
        async def slow_query() -> str:
            await asyncio.sleep(0.2)
            return "ok"

        s.orch.submit_tool("query", slow_query, expected_wait_s=2.0)

        # filler（"嗯……"）在停顿前说出来
        assert await _wait_until(
            lambda: any("嗯……" in c["audio"] for c in s.sink.calls), 2.0
        )
        # filler 说完后语音停稳（自然停顿），工具仍在跑
        assert await s.voice.wait_paused(2.0)
        # 工具完成 -> 语音恢复
        assert await s.tools.wait_all(2.0)
        assert await _wait_until(lambda: not s.voice.paused, 2.0)
