"""IndexTTS 引擎封装与环境探测（sidecar 进程内部使用）。

关键设计：
* **懒加载**——模型实例推迟到首次合成才创建（~10 GB 权重只加载一次），
  启动时只做轻量探测（torch CUDA + indextts 可导入性），保证 sidecar
  在无 GPU / 未装 indextts 的机器上也能启动（engine="none" 降级模式）；
* **真实依赖隔离**——``indextts`` / ``torch`` 绝不出现在 import 顶部，
  也不进主框架依赖，只在 GPU 机器的 sidecar 进程内按需导入；
* ``infer`` 系列调用全部经 ``asyncio.to_thread`` 卸载，避免阻塞事件循环。
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
import threading
import wave
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from vocalis.voice.backends.base import SynthesisOptions

logger = logging.getLogger("vocalis.voice.sidecar.engine")

#: IndexTTS-2 / 2.5 的输出采样率（模型固定输出，文档化常量）。
INDEX_SAMPLE_RATE = 22050


@dataclass(frozen=True)
class EngineAudio:
    """一次完整合成的引擎输出：WAV 字节 + 元数据。"""

    wav: bytes
    sample_rate: int
    duration_s: float


class SynthesisEngine(Protocol):
    """合成引擎协议（sidecar 内部；测试注入 MockEngine 即可离线运行）。

    ``ref_audio`` 是参考音频的**绝对路径**（server 从注册表解析音色名得到）；
    ``stream`` 产出裸 PCM16 mono 字节块（采样率经 ``sample_rate`` 属性声明，
    由 server 写进响应头 ``X-Sample-Rate``）。
    """

    sample_rate: int

    async def synthesize(
        self, text: str, ref_audio: str, opts: SynthesisOptions
    ) -> EngineAudio: ...  # pragma: no cover - 协议声明

    def stream(
        self, text: str, ref_audio: str, opts: SynthesisOptions
    ) -> AsyncIterator[bytes]: ...  # pragma: no cover - 协议声明


@dataclass
class SidecarState:
    """sidecar 运行状态：引擎实例（None=降级模式）与 GPU 探测结果。"""

    engine: SynthesisEngine | None
    gpu: bool


def probe_gpu() -> bool:
    """CUDA 是否可用；torch 未安装/导入失败一律视为 False（不抛异常）。"""
    try:
        import torch

        return bool(torch.cuda.is_available())
    except Exception:
        return False


def probe_indextts() -> bool:
    """indextts 包是否可导入（只探测包存在性，不加载模型）。"""
    try:
        import indextts  # noqa: F401

        return True
    except Exception:
        return False


def probe_state(model_dir: str | Path | None = None) -> SidecarState:
    """启动时的自动探测：无 GPU 或 indextts 未安装 -> engine=None 降级模式。

    降级模式下 /health 仍返回 200（服务活着），合成端点返回 503——
    主框架据此走 Edge-TTS 兜底，而不是把整条 TTS 链路打死。
    """
    gpu = probe_gpu()
    if not gpu:
        logger.warning("CUDA 不可用：sidecar 以 engine='none' 降级模式启动（合成将返回 503）")
        return SidecarState(engine=None, gpu=False)
    if not probe_indextts():
        logger.warning(
            "indextts 包未安装：sidecar 以 engine='none' 降级模式启动；"
            "请在 GPU 机器上安装 IndexTTS（git clone + uv，Python 3.10-3.11）"
        )
        return SidecarState(engine=None, gpu=True)
    logger.info("IndexTTS 环境就绪（GPU=True），引擎将懒加载自 %s", model_dir or "checkpoints")
    return SidecarState(engine=IndexTTSEngine(model_dir), gpu=True)


def _wav_duration(wav: bytes) -> tuple[int, float]:
    """解析 WAV 字节 -> (采样率, 时长秒)；失败返回 (0, 0.0)。"""
    import io

    try:
        with wave.open(io.BytesIO(wav), "rb") as wf:
            rate = wf.getframerate()
            frames = wf.getnframes()
            return rate, frames / rate if rate else 0.0
    except (wave.Error, EOFError):
        return 0, 0.0


class IndexTTSEngine:
    """懒加载的 IndexTTS-2.5 包装（只在 GPU 机器的 sidecar 进程内使用）。

    参数：
        model_dir:   权重目录（checkpoints，含 config.yaml）。
        sample_rate: 声明的输出采样率（写进流式响应头；IndexTTS-2/2.5 为 22050）。

    说明：``emotion`` 映射到 IndexTTS2 的文本情感指令（use_emo_text /
    emo_text）；``speed`` / ``duration_factor`` 引擎本身不支持原生变速，
    仅在协议里透传，实际效果以引擎版本为准。

    线程安全：``_lock`` 只保护懒加载（模型只建一次）；IndexTTS2 实例
    本身**非线程安全**，``_infer_lock`` 把所有 infer 系列调用串行化——
    FastAPI 并发请求经 ``asyncio.to_thread`` 落到不同线程，没有这把锁
    会多线程同时驱动同一模型实例。
    """

    def __init__(
        self, model_dir: str | Path | None = None, sample_rate: int = INDEX_SAMPLE_RATE
    ) -> None:
        self.model_dir = Path(model_dir or "checkpoints")
        self.sample_rate = sample_rate
        self._model: Any = None
        self._lock = threading.Lock()
        self._infer_lock = threading.Lock()

    # -- 模型懒加载 -----------------------------------------------------
    def _ensure_model(self) -> Any:  # pragma: no cover - 需真实 indextts 环境
        """线程安全地加载 IndexTTS2 模型（首次调用耗时：读 ~10 GB 权重）。"""
        with self._lock:
            if self._model is None:
                from indextts.infer_v2_5 import IndexTTS2

                cfg_path = self.model_dir / "config.yaml"
                logger.info("加载 IndexTTS2 模型：%s", self.model_dir)
                self._model = IndexTTS2(
                    cfg_path=str(cfg_path),
                    model_dir=str(self.model_dir),
                )
            return self._model

    def _infer_kwargs(self, opts: SynthesisOptions) -> dict[str, Any]:
        """SynthesisOptions -> IndexTTS2 infer 关键字（情感走文本指令模式）。"""
        kwargs: dict[str, Any] = {}
        if opts.emotion:
            kwargs["use_emo_text"] = True
            kwargs["emo_text"] = opts.emotion
        return kwargs

    # -- SynthesisEngine 协议实现 -----------------------------------------
    async def synthesize(
        self, text: str, ref_audio: str, opts: SynthesisOptions
    ) -> EngineAudio:  # pragma: no cover - 需真实 indextts 环境
        """整段合成：infer 落盘临时 WAV 后读回（EngineAudio）。"""
        out_path: Any = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                out_path = tmp.name

            def _infer() -> None:
                model = self._ensure_model()
                # IndexTTS2 非线程安全：infer 全程持锁串行化（懒加载锁
                # 只保证模型单例，不保护推理本身）
                with self._infer_lock:
                    model.infer(
                        spk_audio_prompt=str(ref_audio),
                        text=text,
                        output_path=str(out_path),
                        **self._infer_kwargs(opts),
                    )

            await asyncio.to_thread(_infer)
            wav = Path(out_path).read_bytes()
        finally:
            if out_path is not None:
                Path(out_path).unlink(missing_ok=True)
        sample_rate, duration_s = _wav_duration(wav)
        return EngineAudio(wav=wav, sample_rate=sample_rate, duration_s=duration_s)

    async def stream(
        self, text: str, ref_audio: str, opts: SynthesisOptions
    ) -> AsyncIterator[bytes]:  # pragma: no cover - 需真实 indextts 环境
        """段级流式合成（infer_generator）；无该 API 时退化为整段合成再切块。"""
        model = await asyncio.to_thread(self._ensure_model)
        generator = getattr(model, "infer_generator", None)
        if generator is None:
            # 旧版/精简版引擎：整段合成后按 ~4096 帧切块（伪流式）
            audio = await self.synthesize(text, ref_audio, opts)
            async for chunk in _wav_to_pcm_chunks(audio.wav):
                yield chunk
            return

        # infer_generator 是同步生成器：线程内迭代，经 queue 回灌事件循环。
        # 与 infer 同理持 _infer_lock——流式也是推理，不能与其他请求并发。
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[tuple[bytes | None, BaseException | None]] = asyncio.Queue()

        def _produce() -> None:  # runs in worker thread
            try:
                with self._infer_lock:
                    for piece in generator(
                        text=text,
                        spk_audio_prompt=str(ref_audio),
                        **self._infer_kwargs(opts),
                    ):
                        asyncio.run_coroutine_threadsafe(
                            queue.put((piece, None)), loop
                        ).result()
            except BaseException as e:  # noqa: BLE001 - 线程边界必须搬运一切异常
                asyncio.run_coroutine_threadsafe(queue.put((None, e)), loop).result()
            else:
                asyncio.run_coroutine_threadsafe(queue.put((None, None)), loop).result()

        asyncio.get_running_loop()
        task = asyncio.create_task(asyncio.to_thread(_produce))
        try:
            while True:
                piece, err = await queue.get()
                if err is not None:
                    raise err
                if piece is None:
                    break
                yield _audio_to_pcm16(piece)
        finally:
            await task


def _audio_to_pcm16(piece: Any) -> bytes:  # pragma: no cover - 需真实引擎
    """infer_generator 的产出（numpy 波形或 (sr, waveform)）-> PCM16 字节。"""
    import numpy as np

    if isinstance(piece, tuple):
        piece = piece[1]
    array = np.asarray(piece)
    if array.dtype != np.int16:
        array = np.clip(array, -1.0, 1.0)
        array = (array * 32767.0).astype("<i2")
    return array.tobytes()


async def _wav_to_pcm_chunks(wav: bytes) -> AsyncIterator[bytes]:  # pragma: no cover
    """WAV 字节 -> PCM16 块流（伪流式回退路径）。"""
    import io

    with wave.open(io.BytesIO(wav), "rb") as wf:
        while True:
            frames = wf.readframes(4096)
            if not frames:
                break
            yield frames
