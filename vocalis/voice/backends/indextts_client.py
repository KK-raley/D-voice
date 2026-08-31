"""IndexTTS sidecar 的 HTTP 客户端后端。

设计要点（IndexTTS-2.5 调研结论）：
* IndexTTS 依赖重（CUDA + ~10 GB 权重 + Python 3.10-3.11），跑在**独立
  进程**的 sidecar 服务里（见 :mod:`vocalis.voice.sidecar`），本类只做
  HTTP 适配——主框架本机可以是纯 CPU 笔记本；
* sidecar 的 ``GET /health`` 区分两种"活着"：服务可达（health_check=True）
  与引擎可用（``engine == "indextts"``，capabilities.available=True）。
  无 GPU 的 sidecar 返回 ``engine="none"``，此时本后端不可用但**不报错**，
  由路由层降级到 Edge-TTS；
* 一切连接失败都转成 :class:`BackendUnavailableError`（降级信号），
  绝不让 ``httpx`` 异常穿透到上层。

端点契约（与 ``vocalis/voice/sidecar/server.py`` 一一对应）::

    GET  /health              -> {"status","engine","gpu","voices"}
    GET  /voices              -> [{name, language, kind, ...}]
    POST /voices/register     -> {name, language, consent, audio_base64}
    POST /synthesize          -> {audio(base64), sample_rate, duration_s, format}
    POST /synthesize/stream   -> 分块 PCM（X-Sample-Rate 响应头）
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path

import httpx

from vocalis.voice.backends.base import (
    KIND_CLONED,
    AudioChunk,
    AudioResult,
    BackendCapabilities,
    BackendUnavailableError,
    SynthesisOptions,
    TTSBackend,
    TTSBackendError,
    VoiceInfo,
    require_consent,
)

logger = logging.getLogger("vocalis.voice.backends.indextts")

#: IndexTTS-2.5 支持的语言（zh/en 为主，2.5 扩展 ja/es/ar）。
_INDEX_LANGUAGES: tuple[str, ...] = ("zh", "en", "ja", "es", "ar")


class IndexTTSClientBackend(TTSBackend):
    """经 HTTP 访问 IndexTTS sidecar 的克隆合成后端。

    参数：
        base_url: sidecar 地址（默认本机 8765 端口）。
        timeout_s: 合成请求超时（克隆合成较慢，默认 30 s）。
        client:   可注入的 ``httpx.AsyncClient``（测试用 MockTransport）；
                  缺省懒创建并持有，随 :meth:`aclose` 关闭。
    """

    name = "indextts"

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8765",
        timeout_s: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._injected_client = client
        self._own_client: httpx.AsyncClient | None = None
        # 服务可达（health 探测结果）与引擎可用（engine=="indextts"）是两回事：
        # 无 GPU 的 sidecar 服务可达但引擎不可用 -> available=False -> 路由降级。
        self._healthy = False
        self._engine_ready = False

    # -- 基础设施 -------------------------------------------------------
    def _client(self) -> httpx.AsyncClient:
        if self._injected_client is not None:
            return self._injected_client
        if self._own_client is None:
            self._own_client = httpx.AsyncClient(
                base_url=self._base_url, timeout=self._timeout_s
            )
        return self._own_client

    async def aclose(self) -> None:
        """关闭自持的 HTTP 客户端（注入的客户端由调用方管理）。"""
        if self._own_client is not None:
            await self._own_client.aclose()
            self._own_client = None

    def _mark_down(self, reason: str) -> None:
        """连接失败：标记不可用（供路由降级），只记日志不抛 httpx 异常。"""
        self._healthy = False
        self._engine_ready = False
        logger.warning("IndexTTS sidecar 不可用: %s", reason)

    def _mark_engine(self, engine: str) -> None:
        self._healthy = True
        self._engine_ready = engine == "indextts"
        if not self._engine_ready:
            logger.info("IndexTTS sidecar 引擎未就绪（engine=%s），克隆合成降级", engine)

    def _url(self, path: str) -> str:
        """绝对 URL：注入的测试 client 没有 base_url，相对路径会直接 ValueError。"""
        return f"{self._base_url}{path}"

    # -- TTSBackend 接口 -------------------------------------------------
    @property
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            supports_cloning=True,
            supports_streaming=True,
            languages=_INDEX_LANGUAGES,
            sample_rate=0,  # 由 sidecar/引擎决定（随响应头返回）
            requires_gpu=True,
            available=self._engine_ready,
        )

    async def health_check(self) -> bool:
        """探测 sidecar：服务存活返回 True（引擎未就绪也算活着），失败 False。"""
        try:
            resp = await self._client().get(
                self._url("/health"), timeout=min(self._timeout_s, 5.0)
            )
            if resp.status_code == 200:
                payload = resp.json()
                if payload.get("status") == "ok":
                    self._mark_engine(str(payload.get("engine", "none")))
                    return True
            self._mark_down(f"/health 返回 {resp.status_code}")
        except Exception as e:
            self._mark_down(f"/health 请求失败: {e}")
        return False

    async def synthesize(
        self, text: str, voice: str, opts: SynthesisOptions | None = None
    ) -> AudioResult:
        opts = opts or SynthesisOptions()
        payload = self._payload(text, voice, opts)
        try:
            resp = await self._client().post(self._url("/synthesize"), json=payload)
        except httpx.HTTPError as e:
            self._mark_down(f"/synthesize 请求失败: {e}")
            raise BackendUnavailableError(f"IndexTTS sidecar 不可达: {e}") from e

        if resp.status_code == 503:
            self._engine_ready = False
            detail = self._detail(resp)
            raise BackendUnavailableError(f"IndexTTS 引擎不可用: {detail}")
        if resp.status_code != 200:
            raise TTSBackendError(
                f"IndexTTS sidecar 合成失败（HTTP {resp.status_code}）: {self._detail(resp)}"
            )

        try:
            data = resp.json()
            audio = base64.b64decode(data["audio"])
            sample_rate = int(data.get("sample_rate", 0))
            duration_s = float(data["duration_s"]) if data.get("duration_s") else None
            fmt = str(data.get("format", "wav"))
        except (KeyError, TypeError, ValueError, binascii.Error) as e:
            # JSON 解析/字段缺失/类型转换失败统一进 TTS 错误体系，不裸抛
            raise TTSBackendError(f"IndexTTS sidecar 响应格式非法: {e}") from e
        return AudioResult(
            data=audio,
            sample_rate=sample_rate,
            duration_s=duration_s,
            format=fmt,
            backend=self.name,
            voice=voice,
        )

    async def stream(
        self, text: str, voice: str, opts: SynthesisOptions | None = None
    ) -> AsyncIterator[AudioChunk]:
        """分块流式合成：sidecar 的段级流式响应转成 AsyncIterator。"""
        opts = opts or SynthesisOptions()
        payload = self._payload(text, voice, opts)
        try:
            async with self._client().stream(
                "POST", self._url("/synthesize/stream"), json=payload
            ) as resp:
                if resp.status_code == 503:
                    detail = (await resp.aread()).decode("utf-8", "replace")
                    self._engine_ready = False
                    raise BackendUnavailableError(
                        f"IndexTTS 引擎不可用: {self._detail_text(detail)}"
                    )
                if resp.status_code != 200:
                    detail = (await resp.aread()).decode("utf-8", "replace")
                    raise TTSBackendError(
                        f"IndexTTS sidecar 流式合成失败"
                        f"（HTTP {resp.status_code}）: {self._detail_text(detail)}"
                    )
                raw_rate = resp.headers.get("X-Sample-Rate", "")
                try:
                    sample_rate: int | None = int(raw_rate) if raw_rate else None
                except ValueError:
                    sample_rate = None
                seq = 0
                try:
                    async for data in resp.aiter_bytes():
                        yield AudioChunk(seq=seq, data=data, sample_rate=sample_rate)
                        seq += 1
                except httpx.HTTPError as e:
                    self._mark_down(f"/synthesize/stream 中断: {e}")
                    raise BackendUnavailableError(
                        f"IndexTTS sidecar 流式响应中断: {e}"
                    ) from e
        except (BackendUnavailableError, TTSBackendError):
            raise
        except httpx.HTTPError as e:
            self._mark_down(f"/synthesize/stream 请求失败: {e}")
            raise BackendUnavailableError(f"IndexTTS sidecar 不可达: {e}") from e

    async def list_voices(self) -> list[VoiceInfo]:
        try:
            resp = await self._client().get(self._url("/voices"))
        except httpx.HTTPError as e:
            self._mark_down(f"/voices 请求失败: {e}")
            raise BackendUnavailableError(f"IndexTTS sidecar 不可达: {e}") from e
        if resp.status_code != 200:
            raise TTSBackendError(
                f"IndexTTS sidecar /voices 失败（HTTP {resp.status_code}）"
            )
        try:
            payload = resp.json()
            if not isinstance(payload, list):
                raise ValueError(f"应为 JSON 数组，实际为 {type(payload).__name__}")
            return [
                VoiceInfo(
                    name=str(v.get("name", "")),
                    language=str(v.get("language", "")),
                    kind=str(v.get("kind", KIND_CLONED)),
                    gender=v.get("gender"),
                )
                for v in payload
            ]
        except (TypeError, AttributeError, ValueError) as e:
            # JSON 解析/字段转换失败统一进 TTS 错误体系，不裸抛
            raise TTSBackendError(f"IndexTTS sidecar /voices 响应格式非法: {e}") from e

    async def register_voice(
        self, ref_path: str | Path, name: str, language: str, consent: str
    ) -> VoiceInfo:
        """注册克隆音色：读参考音频 -> base64 上行（与官方 vLLM recipe 一致）。"""
        require_consent(consent)  # 客户端侧先拦截，不合规的请求不发出去
        try:
            audio = Path(ref_path).read_bytes()
        except OSError as e:
            raise TTSBackendError(f"参考音频不可读（{ref_path}）: {e}") from e

        try:
            resp = await self._client().post(
                self._url("/voices/register"),
                json={
                    "name": name,
                    "language": language,
                    "consent": consent,
                    "audio_base64": base64.b64encode(audio).decode("ascii"),
                },
            )
        except httpx.HTTPError as e:
            self._mark_down(f"/voices/register 请求失败: {e}")
            raise BackendUnavailableError(f"IndexTTS sidecar 不可达: {e}") from e

        if resp.status_code == 400:
            # 调用方错误（consent 缺失/名字非法等）：明确抛给上层，不是降级信号
            raise TTSBackendError(
                f"克隆音色注册被拒绝（HTTP 400）: {self._detail(resp)}"
            )
        if resp.status_code == 503:
            self._engine_ready = False
            raise BackendUnavailableError(
                f"IndexTTS sidecar 无法处理注册: {self._detail(resp)}"
            )
        if resp.status_code != 200:
            raise TTSBackendError(
                f"克隆音色注册失败（HTTP {resp.status_code}）: {self._detail(resp)}"
            )

        try:
            data = resp.json()
            if not isinstance(data, dict):
                raise ValueError(f"应为 JSON 对象，实际为 {type(data).__name__}")
            return VoiceInfo(
                name=str(data.get("name", name)),
                language=str(data.get("language", language)),
                kind=str(data.get("kind", KIND_CLONED)),
                gender=data.get("gender"),
            )
        except (TypeError, AttributeError, ValueError) as e:
            raise TTSBackendError(
                f"IndexTTS sidecar /voices/register 响应格式非法: {e}"
            ) from e

    # -- 内部 -------------------------------------------------------------
    @staticmethod
    def _payload(text: str, voice: str, opts: SynthesisOptions) -> dict:
        return {
            "text": text,
            "voice": voice,
            "speed": opts.speed,
            "emotion": opts.emotion,
            "duration_factor": opts.duration_factor,
        }

    @staticmethod
    def _detail(resp: httpx.Response) -> str:
        try:
            message = resp.json().get("detail", "")
        except Exception:
            message = ""
        return str(message) or resp.text[:200]

    @staticmethod
    def _detail_text(body: str) -> str:
        """从流式响应的错误 body 里提取 detail 字段（尽力而为）。"""
        try:
            return str(json.loads(body).get("detail", "")) or body[:200]
        except Exception:
            return body[:200]
