"""VoiceStream——双流架构之"语音流"：流式 TTS + 播放队列 + 停顿/恢复控制。

管线三段式，全部 asyncio 化，绝不阻塞事件循环::

    feed(delta) ─► [切句缓冲] ─► synth 队列 ─► TTS 引擎 ─► play 队列 ─► 音频后端

* 低首包延迟：文本以句为单位切片，第一句凑齐即开始合成 + 播放，后续
  句子在流水线里并行推进（合成与播放互不等待）。
* 自然停顿：:meth:`VoiceStream.pause` 是"软停顿"——把已入队的文本播完，
  然后在句子边界挂起等待 :meth:`resume`，不掐断半句话。
* 打断（barge-in）：:meth:`VoiceStream.interrupt` 立即停止当前播放并丢弃
  所有未播内容；之后的增量从新一句重新开始（代际号 seq 递增使旧任务失效）。

合成复用 :class:`~vocalis.voice.tts.TTSEngine` 接口（默认 Edge-TTS），
播放后端通过 :class:`AudioSink` 协议抽象：默认 :class:`PlayerSink` 包装
:class:`~vocalis.voice.tts.InterruptiblePlayer`（线程内播放、可被硬停），
测试注入假 sink 即可完全离线运行。

契约：``pause()`` 之后、``resume()`` 之前不要再 ``feed()`` 新文本——
软停顿的语义是"把存量说完再挂起"，新文本请等恢复后（或打断后）再送。
该契约另有机制兜底：停顿期间入队的新内容会被 play worker 挂起，等
``resume()`` 之后才播出，绝不抢播（见 :meth:`VoiceStream.pause` 与
``_play_worker`` 的 ``pre_pause`` 出生快照）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from vocalis.config import audio_cache_dir
from vocalis.server.events import Event, EventBus, EventType
from vocalis.server.events import bus as events_bus
from vocalis.voice.tts import EdgeTTSEngine, InterruptiblePlayer, TTSEngine, VoiceProfile

logger = logging.getLogger("vocalis.dual_stream.voice_stream")

# -- 语音流自有事件（复用 EventBus 广播；工具流用 task.*，见 tool_stream） --
VOICE_PAUSED = "dualstream.voice.paused"          #: 语音流已进入自然停顿
VOICE_RESUMED = "dualstream.voice.resumed"        #: 语音流从停顿中恢复
VOICE_INTERRUPTED = "dualstream.voice.interrupted"  #: barge-in 打断，已停播
VOICE_IDLE = "dualstream.voice.idle"              #: 队列排空，说完一轮
VOICE_ERROR = "dualstream.voice.error"            #: 某片段合成失败（已跳过）

#: 句末终止符：凑齐一个完整句即切片送合成（中英文标点 + 换行）。
_SENTENCE_END = "。！？!?；;\n"


@dataclass
class _VoiceChunk:
    """待合成文本片段（seq 用于打断后丢弃过期的合成任务）。"""

    seq: int
    text: str
    # 出生快照：片段诞生于 pause() 之前（存量）还是之后（停顿期间的新内容）。
    # 存量受 drain 契约保护（播完再停）；新内容由 play worker 机制挂起。
    pre_pause: bool = True


@dataclass
class _PlayItem:
    """待播放音频（text 仅用于事件播报；提示音等原始音频为 None）。"""

    seq: int
    audio: bytes
    text: str | None = None
    # 同 _VoiceChunk.pre_pause：由合成前的片段继承（或 play_raw 入队时快照）。
    pre_pause: bool = True


class AudioSink(Protocol):
    """播放后端协议：异步播放一个音频片段，可被硬停。"""

    async def play_chunk(self, audio: bytes) -> None:
        """播放一段完整音频（MP3 字节），播完才返回；期间可被 stop_now 打断。"""
        ...

    def stop_now(self) -> None:
        """立即停止当前播放（毫秒级生效）。"""
        ...


class PlayerSink:
    """默认播放后端：音频落盘后交给 InterruptiblePlayer 在线程内播放。

    落盘目录复用全局音频缓存（惰性初始化，避免构造副作用）。
    """

    def __init__(self) -> None:
        self._player = InterruptiblePlayer()
        self._cache_dir: Path | None = None
        self._seq = 0

    async def play_chunk(self, audio: bytes) -> None:
        if self._cache_dir is None:
            self._cache_dir = audio_cache_dir()
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._seq += 1
        path = self._cache_dir / f"voice-stream-{os.getpid()}-{self._seq}.mp3"
        path.write_bytes(audio)
        self._player.play(path)
        while self._player.playing:
            await asyncio.sleep(0.02)

    def stop_now(self) -> None:
        self._player.stop()


class VoiceStream:
    """流式语音输出：增量文本 -> 句级切片 -> 流式合成 -> 排队播放。

    参数：
        engine:          TTS 引擎（:class:`TTSEngine` 接口），默认 Edge-TTS。
        profile:         发音配置（音色 / 语速 / 音调 / 音量）。
        sink:            播放后端，默认 :class:`PlayerSink`（真实音频）。
        bus:             事件总线；默认复用全局单例 ``vocalis.server.events.bus``。
        max_chunk_chars: 未遇句末符时的强制切片长度（防超长句撑爆首包延迟）。

    状态查询：:attr:`paused`（软停顿标志）、:meth:`wait_idle`（等说完）、
    :meth:`wait_paused`（等真正停住）。注意实例须在事件循环线程创建。
    """

    def __init__(
        self,
        engine: TTSEngine | None = None,
        profile: VoiceProfile | None = None,
        sink: AudioSink | None = None,
        bus: EventBus | None = None,
        max_chunk_chars: int = 60,
    ) -> None:
        self.engine = engine or EdgeTTSEngine()
        self.profile = profile or VoiceProfile()
        self.sink: AudioSink = sink or PlayerSink()
        self.bus = bus or events_bus
        self.max_chunk_chars = max(8, int(max_chunk_chars))

        self._buffer = ""
        self._synth_q: asyncio.Queue[_VoiceChunk] = asyncio.Queue()
        self._play_q: asyncio.Queue[_PlayItem] = asyncio.Queue()
        self._resume_evt = asyncio.Event()
        self._resume_evt.set()
        self._paused = False
        self._pause_announced = False
        self._interrupt_seq = 0
        self._synth_busy = False
        self._playing = False
        self._was_busy = False
        self._tasks: list[asyncio.Task[None]] = []
        self._emit_tasks: set[asyncio.Task[Event]] = set()
        self._started = False

    # -- 生命周期 ------------------------------------------------------
    def start(self) -> None:
        """启动合成 / 播放两个 worker 协程（幂等）。"""
        if self._started:
            return
        self._started = True
        self._tasks = [
            asyncio.create_task(self._synth_worker(), name="voice-synth"),
            asyncio.create_task(self._play_worker(), name="voice-play"),
        ]

    async def stop(self) -> None:
        """停止 worker（丢弃未播内容，立即返回）。"""
        if not self._started:
            return
        # 先硬停当前播放：只 cancel worker 掐不死已在播的音频，
        # 否则关闭后当前片段会继续播到自然结束。
        self.sink.stop_now()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        self._started = False

    # -- 输入 API ------------------------------------------------------
    def feed(self, delta: str) -> None:
        """送入一段增量文本（LLM 流式输出），凑齐整句即切片入合成队列。"""
        if not delta:
            return
        self._buffer += delta
        self._drain(final=False)

    def flush(self) -> None:
        """输入结束：把缓冲里的残句也送出去（不完整句也合成）。"""
        self._drain(final=True)

    def say(self, text: str) -> None:
        """一次性说一句话（feed + flush 的便捷封装）。"""
        self.feed(text)
        self.flush()

    def play_raw(self, audio: bytes) -> None:
        """直接把原始音频排入播放队列（思考提示音等，绕过 TTS）。"""
        self._play_q.put_nowait(
            _PlayItem(seq=self._interrupt_seq, audio=audio, pre_pause=not self._paused)
        )

    # -- 停顿 / 恢复 / 打断 --------------------------------------------
    def pause(self) -> None:
        """软停顿：已入队内容播完后在句边界挂起（自然停顿，不掐半句）。"""
        if self._paused:
            return
        self._paused = True
        self._resume_evt.clear()
        self._maybe_announce_pause()

    def resume(self) -> None:
        """从自然停顿中恢复，继续消费播放队列。"""
        if not self._paused:
            return
        self._paused = False
        self._resume_evt.set()
        if self._pause_announced:
            # 只有发布过 VOICE_PAUSED 才发 VOICE_RESUMED：
            # 若停顿尚未"停稳"就被恢复，不发孤儿 RESUMED（无配对 PAUSED）。
            self._pause_announced = False
            self._emit(VOICE_RESUMED)

    def interrupt(self) -> None:
        """硬打断（barge-in）：立即停播、清空一切未播内容，回到就绪态。"""
        self._interrupt_seq += 1
        self._buffer = ""
        self._drain_queue(self._synth_q)
        self._drain_queue(self._play_q)
        self._paused = False
        self._pause_announced = False
        self._resume_evt.set()  # 唤醒可能挂起的 play worker
        self.sink.stop_now()    # 毫秒级掐断当前播放
        self._was_busy = False
        self._emit(VOICE_INTERRUPTED)

    @property
    def paused(self) -> bool:
        """是否处于软停顿（标志位；是否已"停稳"用 :meth:`wait_paused`）。"""
        return self._paused

    async def wait_idle(self, timeout: float = 5.0) -> bool:
        """等待一切输入都合成并播完（软停顿中的挂起状态也算 idle）。"""
        return await self._wait_until(self._is_drained, timeout)

    async def wait_paused(self, timeout: float = 5.0) -> bool:
        """等待语音流真正停稳（paused 且队列排空、无在播/在合成内容）。"""
        return await self._wait_until(lambda: self._paused and self._is_drained(), timeout)

    # -- 内部：切句与队列 ----------------------------------------------
    def _drain(self, final: bool) -> None:
        """把缓冲切成片段塞进合成队列；final=True 时残句也放行。"""
        while True:
            piece = self._next_piece(final)
            if piece is None:
                break
            if piece:
                self._synth_q.put_nowait(
                    _VoiceChunk(
                        seq=self._interrupt_seq, text=piece, pre_pause=not self._paused
                    )
                )

    def _next_piece(self, final: bool) -> str | None:
        """切出下一个片段；无可切返回 None，纯空白片段返回空串。"""
        buf = self._buffer
        if not buf:
            return None
        cut = 0
        for i, ch in enumerate(buf):
            if ch in _SENTENCE_END:
                cut = i + 1
                break
        if cut == 0:
            if len(buf) >= self.max_chunk_chars:
                cut = self.max_chunk_chars  # 超长句强制切片，保住首包延迟
            elif final:
                cut = len(buf)              # 输入结束，残句放行
            else:
                return None                 # 未凑齐整句，继续等增量
        self._buffer = buf[cut:]
        return buf[:cut].strip()

    @staticmethod
    def _drain_queue(queue: asyncio.Queue[Any]) -> None:
        while not queue.empty():
            queue.get_nowait()

    def _is_drained(self) -> bool:
        """合成与播放管线全部排空（无残句、无在途、无在播）。"""
        return (
            not self._buffer
            and self._synth_q.empty()
            and self._play_q.empty()
            and not self._synth_busy
            and not self._playing
        )

    # -- 内部：worker ---------------------------------------------------
    async def _synth_worker(self) -> None:
        """合成 worker：片段 -> TTS 字节 -> 播放队列。"""
        while True:
            chunk = await self._synth_q.get()
            self._synth_busy = True
            try:
                if chunk.seq != self._interrupt_seq:  # 打断后过期
                    continue
                try:
                    audio = await self.engine.synthesize(chunk.text, self.profile)
                except Exception as exc:
                    logger.warning("synthesize failed for %r: %s", chunk.text, exc)
                    self._emit(VOICE_ERROR, text=chunk.text, error=str(exc))
                    continue
                if chunk.seq == self._interrupt_seq:  # 合成期间可能被打断
                    self._play_q.put_nowait(
                        _PlayItem(
                            seq=chunk.seq,
                            audio=audio,
                            text=chunk.text,
                            pre_pause=chunk.pre_pause,
                        )
                    )
            finally:
                self._synth_busy = False

    async def _play_worker(self) -> None:
        """播放 worker：播放队列 -> 音频后端；软停顿时在句边界挂起。"""
        while True:
            item = await self._play_q.get()
            if item.seq != self._interrupt_seq:  # 打断后过期
                continue
            if self._paused and not item.pre_pause and self._is_drained():
                # 软停顿契约内建为机制：停顿期间出生的新内容不抢播，
                # 先挂起等 resume（或 interrupt）后再消费。pause() 之前
                # 入队的存量（filler / 提示音 / 排队句子）不受影响——
                # drain 契约仍由尾部挂起保证（说完存量再停）。
                self._maybe_announce_pause()  # 停顿因新内容而"停稳"：补发 PAUSED
                await self._resume_evt.wait()
                if item.seq != self._interrupt_seq:  # 挂起期间被打断：丢弃
                    continue
            if item.text:
                # 复用既有 TTS 事件类型，HUD 的"正在说话"指示灯天然工作。
                await self.bus.publish(EventType.TTS_SPEAKING, text=item.text)
            self._playing = True
            self._was_busy = True
            try:
                await self.sink.play_chunk(item.audio)
            finally:
                self._playing = False
            self._maybe_announce_pause()
            self._maybe_announce_idle()
            if self._paused and self._is_drained():
                await self._resume_evt.wait()  # 自然停顿：挂起直到 resume/interrupt
                continue

    def _maybe_announce_pause(self) -> None:
        """条件发布 VOICE_PAUSED（只发一次，resume 后复位）。"""
        if self._paused and not self._pause_announced and self._is_drained():
            self._pause_announced = True
            self._emit(VOICE_PAUSED)

    def _maybe_announce_idle(self) -> None:
        """条件发布 VOICE_IDLE（从"忙"到"闲"的下降沿才发）。"""
        if not self._paused and self._is_drained() and self._was_busy:
            self._was_busy = False
            self._emit(VOICE_IDLE)

    # -- 内部：工具函数 --------------------------------------------------
    async def _wait_until(self, predicate: Any, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.01)
        return True

    def _emit(self, type_: str, **data: Any) -> None:
        """在同步上下文里发布事件的辅助函数（保存任务引用防 GC）。"""
        task = asyncio.create_task(self.bus.publish(type_, **data))
        self._emit_tasks.add(task)
        task.add_done_callback(self._emit_tasks.discard)
