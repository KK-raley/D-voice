"""可插拔 TTS 后端框架：抽象接口、Edge-TTS 包装、IndexTTS sidecar 客户端、路由器。

典型组装（见 :func:`~vocalis.voice.backends.router.build_router`）::

    router = build_router(VocalisConfig.load())
    await router.synthesize("你好", "my-cloned-voice")   # -> IndexTTS（可用时）
    await router.synthesize("你好", "zh-CN-XiaoxiaoNeural")  # -> Edge-TTS
"""

from vocalis.voice.backends.base import (
    KIND_CLONED,
    KIND_PRESET,
    AudioChunk,
    AudioResult,
    BackendCapabilities,
    BackendUnavailableError,
    ConsentMissingError,
    SynthesisOptions,
    TTSBackend,
    TTSBackendError,
    VoiceInfo,
    require_consent,
)
from vocalis.voice.backends.edge import EdgeTTSBackend
from vocalis.voice.backends.indextts_client import IndexTTSClientBackend
from vocalis.voice.backends.router import TTSRouter, build_router

__all__ = [
    "AudioChunk",
    "AudioResult",
    "BackendCapabilities",
    "BackendUnavailableError",
    "ConsentMissingError",
    "EdgeTTSBackend",
    "IndexTTSClientBackend",
    "KIND_CLONED",
    "KIND_PRESET",
    "SynthesisOptions",
    "TTSBackend",
    "TTSBackendError",
    "TTSRouter",
    "VoiceInfo",
    "build_router",
    "require_consent",
]
