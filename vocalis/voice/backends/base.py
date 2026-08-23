"""TTS 后端抽象：能力协商数据类 + 统一后端接口。

设计动机（IndexTTS-2.5 调研结论）：重型 TTS 引擎（IndexTTS：git clone +
uv 安装、Python 3.10-3.11、CUDA、~10 GB 权重）不能 import 进主框架，
因此后端分为两类：

* **进程内后端**（如 EdgeTTSBackend）：直接包装引擎库；
* **sidecar 客户端后端**（如 IndexTTSClientBackend）：经 HTTP 访问独立
  进程的引擎服务，主框架本机无 GPU 时可优雅降级到 Edge-TTS。

路由与降级的决策依据全部来自 :class:`BackendCapabilities`（能力协商），
而非 try/except 嗅探——上层（:class:`~vocalis.voice.backends.router.TTSRouter`）
据此把克隆音色发给 IndexTTS、preset 音色发给 Edge-TTS。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

#: preset 音色 kind 标识（引擎内置音色，如 Edge-TTS 神经音色）。
KIND_PRESET = "preset"
#: 克隆音色 kind 标识（由参考音频注册而来，仅支持克隆的后端可合成）。
KIND_CLONED = "cloned"


@dataclass(frozen=True)
class BackendCapabilities:
    """后端能力声明——路由与降级决策的唯一依据。

    fields:
        supports_cloning:  是否支持参考音频克隆音色（IndexTTS=True、Edge=False）。
        supports_streaming: 是否支持分块流式合成（首包延迟敏感场景）。
        languages:         支持的语言前缀（BCP-47）；空元组表示不限。
        sample_rate:       输出采样率（Hz）；0 表示由引擎/sidecar 决定。
        requires_gpu:      后端运行是否依赖 GPU（sidecar 宿主机）。
        available:         当前后端是否可用（探测结果，可随运行时变化）。
    """

    supports_cloning: bool = False
    supports_streaming: bool = False
    languages: tuple[str, ...] = ()
    sample_rate: int = 0
    requires_gpu: bool = False
    available: bool = True


@dataclass(frozen=True)
class VoiceInfo:
    """一个可发音色：preset（引擎内置）或 cloned（参考音频克隆）。"""

    name: str
    language: str
    kind: str = KIND_PRESET
    gender: str | None = None


@dataclass(frozen=True)
class SynthesisOptions:
    """合成选项（跨后端并集；各后端只消费自己认识的字段）。

    Edge-TTS 消费 rate/pitch/volume（字符串增量）；IndexTTS 消费
    speed/emotion/duration_factor（IndexTTS-2.5 的情感与时长控制）。
    """

    speed: float = 1.0
    emotion: str | None = None
    duration_factor: float | None = None
    rate: str = "+0%"
    pitch: str = "+0Hz"
    volume: str = "+0%"


@dataclass(frozen=True)
class AudioResult:
    """一次完整合成的结果：音频字节 + 采样率/时长元数据。"""

    data: bytes
    sample_rate: int
    duration_s: float | None
    format: str = "mp3"
    backend: str = ""
    voice: str = ""


@dataclass(frozen=True)
class AudioChunk:
    """流式合成的一个音频分块（seq 保证按序消费）。"""

    seq: int
    data: bytes
    sample_rate: int | None = None
    duration_s: float | None = None


class TTSBackendError(Exception):
    """TTS 后端统一错误（合成失败、非法音色、sidecar 4xx 等）。"""


class BackendUnavailableError(TTSBackendError):
    """后端不可达或引擎不可用——路由层捕获后自动降级到 Edge-TTS。"""


class ConsentMissingError(TTSBackendError, ValueError):
    """克隆音色注册缺少同意声明。

    双继承（TTSBackendError + ValueError）：调用方按 TTS 错误体系捕获
    （``except TTSBackendError``）或按历史习惯捕获 ``except ValueError``
    都能命中，错误类型不再漂移。
    """


def require_consent(consent: str) -> None:
    """克隆音色注册的强制同意检查。

    参考 IndexTTS 官方 vLLM recipe 的合规设计：参考音频属于说话人生物
    特征，注册（提取音色特征）前必须留下明确的同意声明；缺失即拒绝，
    不提供任何绕过路径。
    """
    if not consent or not str(consent).strip():
        raise ConsentMissingError(
            "consent is required: 注册克隆音色需要参考音频所有者的明确同意声明"
        )


class TTSBackend(ABC):
    """可插拔 TTS 后端接口。

    实现约束：
    * ``capabilities.available`` 必须廉价（不发网络请求、不加载模型）；
    * :meth:`health_check` 探测失败时返回 False，**绝不抛异常**；
    * sidecar 类后端不可达时以 :class:`BackendUnavailableError` 上抛，
      由路由层降级，普通错误用 :class:`TTSBackendError`。
    """

    name: str = "base"

    @property
    @abstractmethod
    def capabilities(self) -> BackendCapabilities:
        """能力声明（路由决策依据）。"""

    @abstractmethod
    async def synthesize(
        self, text: str, voice: str, opts: SynthesisOptions | None = None
    ) -> AudioResult:
        """合成完整音频。"""

    @abstractmethod
    def stream(
        self, text: str, voice: str, opts: SynthesisOptions | None = None
    ) -> AsyncIterator[AudioChunk]:
        """流式合成：逐块产出音频（实现为 async 生成器）。"""

    @abstractmethod
    async def list_voices(self) -> list[VoiceInfo]:
        """枚举本后端可用音色。"""

    @abstractmethod
    async def register_voice(
        self, ref_path: str | Path, name: str, language: str, consent: str
    ) -> VoiceInfo:
        """注册克隆音色（consent 必填，见 :func:`require_consent`）。

        不支持克隆的后端对 preset 音色实现为"校验空操作"，对新名字
        抛 :class:`TTSBackendError`。
        """

    @abstractmethod
    async def health_check(self) -> bool:
        """探测后端是否存活（失败返回 False，不抛异常）。"""
