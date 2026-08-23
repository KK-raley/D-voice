"""TTSRouter——按能力路由 + 自动降级的统一合成入口。

路由规则（全部基于 :class:`~vocalis.voice.backends.base.BackendCapabilities`
能力协商，不做 try/except 嗅探）：

1. **克隆音色**（在克隆后端的注册表里）-> IndexTTS sidecar（可用时）；
2. **preset 音色**（Edge 命名模式或后端音色表内）-> Edge-TTS；
3. **IndexTTS 不可用**（sidecar 未启动 / 无 GPU / 引擎未就绪）-> 克隆音色
   经预设音色映射表降级到 Edge-TTS，绝不静默失败也绝不抛连接异常；
4. **IndexTTS 合成中途挂掉** -> :class:`BackendUnavailableError` 被捕获，
   自动用 Edge-TTS 重试（流式路径仅在首块产出前可降级——之后切换格式
   会产生损坏的音频流，错误如实上抛）。

可用性探测带 TTL：sidecar 恢复后最多一个 TTL 周期即自动回归克隆路径。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import AsyncIterator, Sequence

from vocalis.voice.backends.base import (
    AudioChunk,
    AudioResult,
    BackendUnavailableError,
    SynthesisOptions,
    TTSBackend,
    TTSBackendError,
    VoiceInfo,
)
from vocalis.voice.backends.edge import EdgeTTSBackend
from vocalis.voice.backends.indextts_client import IndexTTSClientBackend

logger = logging.getLogger("vocalis.voice.backends.router")

#: Edge-TTS preset 音色命名模式（zh-CN-XiaoxiaoNeural / en-US-GuyNeural ...）。
#: 命中即视为 preset——离线环境下 Edge 音色表只有 4 个 fallback 条目，
#: 模式匹配保证其余几十个官方音色也能正确路由（而不是被当成克隆音色降级）。
_PRESET_VOICE_RE = re.compile(r"^[a-z]{2,3}(-[A-Za-z]{2,8})?-\w+Neural$")

#: 可用性探测的默认重探周期（秒）。
_PROBE_TTL_S = 30.0


class TTSRouter:
    """多后端统一入口：synthesize / stream / list_voices / register_voice。

    参数：
        backends:      后端列表（顺序即优先级；典型为 [edge, indextts]）。
        default_voice: 克隆音色降级到 Edge 时使用的兜底 preset 音色。
        preset_map:    克隆音色名 -> preset 音色的显式映射（优先于 default_voice）。
        probe_ttl_s:   克隆后端可用性探测的重探周期。
    """

    def __init__(
        self,
        backends: Sequence[TTSBackend],
        default_voice: str = "zh-CN-XiaoxiaoNeural",
        preset_map: dict[str, str] | None = None,
        probe_ttl_s: float = _PROBE_TTL_S,
    ) -> None:
        self.backends: list[TTSBackend] = list(backends)
        self.default_voice = default_voice
        self.preset_map: dict[str, str] = dict(preset_map or {})
        self._probe_ttl_s = probe_ttl_s
        # 克隆后端的音色缓存（name -> VoiceInfo）；探测成功时刷新。
        self._cloned: dict[str, VoiceInfo] = {}
        self._probe_ts: float = 0.0  # 上次探测时间；0 = 下次调用必须探测
        # 探测 single-flight 锁：TTL 过期瞬间的并发请求只发一次 health_check。
        self._probe_lock = asyncio.Lock()
        # preset 音色缓存（惰性取自 fallback 后端的音色表）。
        self._presets: set[str] | None = None

    # -- 后端选择 -------------------------------------------------------
    @property
    def cloning(self) -> TTSBackend | None:
        """第一个支持克隆的后端（IndexTTS sidecar 客户端）。"""
        for backend in self.backends:
            if backend.capabilities.supports_cloning:
                return backend
        return None

    @property
    def fallback(self) -> TTSBackend | None:
        """第一个非克隆后端（Edge-TTS，永远在线的兜底）。"""
        for backend in self.backends:
            if not backend.capabilities.supports_cloning:
                return backend
        return None

    # -- 统一入口 ---------------------------------------------------------
    async def synthesize(
        self, text: str, voice: str, opts: SynthesisOptions | None = None
    ) -> AudioResult:
        """合成完整音频；克隆后端不可用/中途挂掉时自动降级 Edge-TTS。"""
        backend, routed_voice = await self._route(voice)
        if backend is self.fallback:
            # 已是兜底路径，失败直接上抛（没有更低的层级可退）
            return await backend.synthesize(text, routed_voice, opts)
        try:
            return await backend.synthesize(text, routed_voice, opts)
        except BackendUnavailableError as e:
            logger.warning("克隆后端合成失败，降级到 Edge-TTS: %s", e)
            self._probe_ts = 0.0  # 下次调用重新探测（sidecar 恢复即回归）
            fb = self._require_fallback()
            return await fb.synthesize(text, self._map_voice(voice), opts)

    async def stream(
        self, text: str, voice: str, opts: SynthesisOptions | None = None
    ) -> AsyncIterator[AudioChunk]:
        """流式合成；仅当克隆后端在首块产出前失败才降级 Edge-TTS。"""
        backend, routed_voice = await self._route(voice)
        if backend is self.fallback:
            async for chunk in backend.stream(text, routed_voice, opts):
                yield chunk
            return
        yielded = False
        try:
            async for chunk in backend.stream(text, routed_voice, opts):
                yielded = True
                yield chunk
        except BackendUnavailableError as e:
            if yielded:
                # 已输出部分音频，中途切换格式会产生损坏的流：如实上抛。
                raise
            logger.warning("克隆后端流式合成失败，降级到 Edge-TTS: %s", e)
            self._probe_ts = 0.0
            fb = self._require_fallback()
            async for chunk in fb.stream(text, self._map_voice(voice), opts):
                yield chunk

    async def list_voices(self) -> list[VoiceInfo]:
        """合并所有后端的音色表；单个后端挂掉只贡献空集，不抛异常。"""
        merged: list[VoiceInfo] = []
        for backend in self.backends:
            try:
                voices = await backend.list_voices()
            except TTSBackendError as e:
                logger.info("后端 %s 音色枚举失败（跳过）: %s", backend.name, e)
                continue
            merged.extend(voices)
        return merged

    async def register_voice(
        self, ref_path: str, name: str, language: str, consent: str
    ) -> VoiceInfo:
        """注册克隆音色（委托给克隆后端；不可用时明确报错而非静默降级）。"""
        cloning = self.cloning
        await self._ensure_probe()
        if cloning is None or not cloning.capabilities.available:
            raise TTSBackendError(
                "语音克隆后端不可用（IndexTTS sidecar 未启用或已下线），"
                "无法注册克隆音色；请检查 sidecar 服务与 [sidecar] 配置"
            )
        info = await cloning.register_voice(ref_path, name, language, consent)
        self._cloned[info.name] = info  # 注册即入缓存，合成立即可路由
        return info

    # -- 路由决策 ---------------------------------------------------------
    async def _route(self, voice: str) -> tuple[TTSBackend, str]:
        """为一次合成选择 (后端, 实际音色)。"""
        await self._ensure_probe()
        cloning = self.cloning
        if cloning is not None and cloning.capabilities.available:
            if voice in self._cloned:
                return cloning, voice
        fb = self._require_fallback()
        if await self._is_preset(fb, voice):
            return fb, voice
        # 既不是已注册克隆音色也不是 preset：按映射表降级（可能从未配置过
        # sidecar，也可能 sidecar 挂了——统一走 Edge 兜底并留痕）。
        mapped = self._map_voice(voice)
        logger.info("音色 %r 无法克隆（后端不可用或未注册），降级为 %r", voice, mapped)
        return fb, mapped

    async def _ensure_probe(self) -> None:
        """按 TTL 探测克隆后端可用性并刷新音色缓存（无克隆后端则跳过）。

        single-flight：TTL 过期瞬间的并发请求经 ``_probe_lock`` 串行化，
        拿到锁后 double-check——等待期间别的协程已完成探测就不再重复，
        健康探测在并发下只发一次。
        """
        cloning = self.cloning
        if cloning is None:
            return
        now = time.monotonic()
        if self._probe_ts and (now - self._probe_ts) < self._probe_ttl_s:
            return
        async with self._probe_lock:
            # double-check：等锁期间探测可能已被并发协程完成
            now = time.monotonic()
            if self._probe_ts and (now - self._probe_ts) < self._probe_ttl_s:
                return
            self._probe_ts = now
            alive = await cloning.health_check()  # 契约：失败返回 False，绝不抛
            if alive and cloning.capabilities.available:
                try:
                    infos = await cloning.list_voices()
                    self._cloned = {info.name: info for info in infos}
                except TTSBackendError as e:
                    logger.info("克隆后端音色枚举失败（缓存清空）: %s", e)
                    self._cloned = {}

    async def aclose(self) -> None:
        """关闭路由器持有的后端资源（sidecar 客户端的 httpx 连接池等）。

        逐个调用有 ``aclose`` 的后端（如 IndexTTSClientBackend）；无该
        协议的后端（EdgeTTSBackend 无连接状态）自动跳过，单个关闭失败
        只记日志不影响其余后端。
        """
        for backend in self.backends:
            aclose = getattr(backend, "aclose", None)
            if aclose is None:
                continue
            try:
                await aclose()
            except Exception:  # noqa: BLE001 - 清理路径绝不级联失败
                logger.warning("关闭后端 %s 失败", backend.name, exc_info=True)

    async def _is_preset(self, fallback: TTSBackend, voice: str) -> bool:
        """voice 是否为 Edge preset 音色（命名模式或音色表命中）。"""
        if _PRESET_VOICE_RE.match(voice):
            return True
        if self._presets is None:
            try:
                self._presets = {info.name for info in await fallback.list_voices()}
            except TTSBackendError:
                self._presets = set()
        return voice in self._presets

    def _map_voice(self, voice: str) -> str:
        """克隆音色 -> Edge preset 音色（显式映射优先，其次语言默认，最后兜底）。"""
        return self.preset_map.get(voice, self.default_voice)

    def _require_fallback(self) -> TTSBackend:
        fb = self.fallback
        if fb is None:
            raise TTSBackendError("无可用 TTS 后端：至少需要注册 Edge-TTS 后端")
        return fb


def build_router(config=None) -> TTSRouter:
    """按配置组装路由器（默认 Edge only，sidecar.enabled 时加 IndexTTS 客户端）。

    典型配置（``~/.vocalis/config.toml``）::

        [sidecar]
        enabled = true
        base_url = "http://gpu-box:8765"
        timeout_s = 30.0
        fallback_voice = "zh-CN-XiaoxiaoNeural"
        preset_map = { li = "zh-CN-XiaoxiaoNeural" }   # 克隆音色降级映射
    """
    if config is None:
        from vocalis.config import VocalisConfig

        config = VocalisConfig.load()

    backends: list[TTSBackend] = [EdgeTTSBackend()]
    if config.sidecar.enabled:
        backends.append(
            IndexTTSClientBackend(
                base_url=config.sidecar.base_url,
                timeout_s=config.sidecar.timeout_s,
            )
        )
    return TTSRouter(
        backends,
        default_voice=config.sidecar.fallback_voice,
        preset_map=dict(config.sidecar.preset_map),
    )
