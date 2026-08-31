"""Edge-TTS 后端：包装既有 edge-tts 引擎（preset 音色，零成本降级兜底）。

复用 :mod:`vocalis.voice.tts` 里的枚举/合成逻辑（不重复造轮子），
只是把它适配到 :class:`~vocalis.voice.backends.base.TTSBackend` 接口：
``register_voice`` 对 preset 音色是"校验空操作"，对新名字报错——
Edge-TTS 不支持克隆，克隆请求应由路由层转给 IndexTTS sidecar。

整段合成直接委托 :class:`~vocalis.voice.tts.EdgeTTSEngine`（构造临时
VoiceProfile 透传 rate/pitch/volume），错误统一包装为
:class:`TTSBackendError`——合成逻辑只此一份，避免引擎层与后端层双份
维护导致错误类型漂移。流式路径引擎层没有对应 API（EdgeTTSEngine 只
提供整段合成），保留在适配层实现。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path

from vocalis.voice.backends.base import (
    KIND_PRESET,
    AudioChunk,
    AudioResult,
    BackendCapabilities,
    SynthesisOptions,
    TTSBackend,
    TTSBackendError,
    VoiceInfo,
    require_consent,
)
from vocalis.voice.tts import EdgeTTSEngine, VoiceProfile
from vocalis.voice.tts import list_voices as edge_list_voices

logger = logging.getLogger("vocalis.voice.backends.edge")

#: Edge-TTS 输出固定为 24 kHz MP3。
EDGE_SAMPLE_RATE = 24000


def _edge_importable() -> bool:
    """edge_tts 是否可导入（离线、廉价——不发任何网络请求）。"""
    try:
        import edge_tts  # noqa: F401

        return True
    except Exception:
        return False


class EdgeTTSBackend(TTSBackend):
    """Microsoft Edge 神经音色——免费、无 key、几十种语言，永远在线的兜底。"""

    name = "edge"

    def __init__(self) -> None:
        self._engine = EdgeTTSEngine()

    @property
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            supports_cloning=False,
            supports_streaming=True,
            languages=(),  # 不限：Edge-TTS 覆盖数十个 locale
            sample_rate=EDGE_SAMPLE_RATE,
            requires_gpu=False,
            available=_edge_importable(),
        )

    @staticmethod
    def _profile(text_opts: SynthesisOptions, voice: str) -> VoiceProfile:
        """SynthesisOptions -> 临时 VoiceProfile（委托引擎所需的参数载体）。"""
        return VoiceProfile(
            name=voice,
            voice=voice,
            rate=text_opts.rate,
            pitch=text_opts.pitch,
            volume=text_opts.volume,
        )

    async def synthesize(
        self, text: str, voice: str, opts: SynthesisOptions | None = None
    ) -> AudioResult:
        opts = opts or SynthesisOptions()
        if not _edge_importable():
            raise TTSBackendError(
                "edge-tts 不可用：pip install 'vocalis-voice-agent[voice]'"
            )
        try:
            data = await self._engine.synthesize(text, self._profile(opts, voice))
        except Exception as e:  # 引擎层错误（RuntimeError 等）统一进 TTS 错误体系
            raise TTSBackendError(f"edge-tts 合成失败: {e}") from e
        return AudioResult(
            data=data,
            sample_rate=EDGE_SAMPLE_RATE,
            duration_s=None,  # MP3 帧流不携带时长，交由播放端确定
            format="mp3",
            backend=self.name,
            voice=voice,
        )

    async def stream(
        self, text: str, voice: str, opts: SynthesisOptions | None = None
    ) -> AsyncIterator[AudioChunk]:
        opts = opts or SynthesisOptions()
        try:
            import edge_tts
        except Exception as e:
            raise TTSBackendError(
                "edge-tts 不可用：pip install 'vocalis-voice-agent[voice]'"
            ) from e

        communicate = edge_tts.Communicate(
            text=text,
            voice=voice,
            rate=opts.rate,
            pitch=opts.pitch,
            volume=opts.volume,
        )
        seq = 0
        try:
            async for chunk in communicate.stream():
                if chunk["type"] != "audio":
                    continue
                yield AudioChunk(seq=seq, data=chunk["data"], sample_rate=EDGE_SAMPLE_RATE)
                seq += 1
        except TTSBackendError:
            raise
        except Exception as e:
            raise TTSBackendError(f"edge-tts 流式合成失败: {e}") from e

    async def list_voices(self) -> list[VoiceInfo]:
        """枚举 Edge-TTS 音色（离线时自动回退到内置 fallback 列表）。"""
        raw = await edge_list_voices()
        return [
            VoiceInfo(
                name=str(v.get("ShortName", "")),
                language=str(v.get("Locale", "")),
                kind=KIND_PRESET,
                gender=str(v.get("Gender", "")) or None,
            )
            for v in raw
        ]

    async def register_voice(
        self, ref_path: str | Path, name: str, language: str, consent: str
    ) -> VoiceInfo:
        """preset 音色：校验空操作；新名字：Edge-TTS 不支持克隆，明确拒绝。"""
        require_consent(consent)
        for info in await self.list_voices():
            if info.name == name:
                return info  # 已是内置音色——无需（也无法）注册
        raise TTSBackendError(
            f"edge-tts 后端不支持克隆音色注册（音色 '{name}' 不是 preset 音色）；"
            "请启用 IndexTTS sidecar 后注册克隆音色"
        )

    async def health_check(self) -> bool:
        """edge_tts 可导入即视为健康（合成本身依赖微软在线服务）。"""
        return _edge_importable()
