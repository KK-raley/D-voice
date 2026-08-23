"""可插拔 TTS 后端框架测试：能力协商 / Edge 包装 / IndexTTS HTTP 客户端 / 路由回退。

完全离线：
* ``edge_tts`` 经 ``sys.modules`` 注入假模块（无论真包是否安装都成立）；
* IndexTTS 客户端用 ``httpx.MockTransport`` 模拟 sidecar 的 HTTP 响应；
* 路由回退用假后端（FakeBackend）验证"IndexTTS 挂 -> Edge-TTS 兜底"。
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import time
import types
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from vocalis.config import VocalisConfig
from vocalis.voice.backends.base import (
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

# ---------------------------------------------------------------------
# helpers: fake edge_tts
# ---------------------------------------------------------------------
_RAW_VOICES = [
    {"ShortName": "zh-CN-XiaoxiaoNeural", "Gender": "Female", "Locale": "zh-CN"},
    {"ShortName": "en-US-GuyNeural", "Gender": "Male", "Locale": "en-US"},
]


def _install_fake_edge_tts(
    monkeypatch, *, voices=None, audio_chunks=None, error=None
) -> list[dict]:
    """注入假 edge_tts 模块；返回 Communicate 构造参数记录列表。

    无论真实 edge_tts 是否安装都成立：``import edge_tts`` 优先解析
    ``sys.modules``（与 test_voice_features.py 同一套 mock 手法）。
    """
    calls: list[dict] = []
    chunks = list(audio_chunks) if audio_chunks is not None else [b"mp3-1", b"mp3-2"]

    class Communicate:
        def __init__(self, **kwargs) -> None:
            calls.append(kwargs)

        async def stream(self):
            if error is not None:
                raise error
            yield {"type": "WordBoundary", "offset": 0}  # 非 audio 帧必须被跳过
            for chunk in chunks:
                yield {"type": "audio", "data": chunk}

    module = types.ModuleType("edge_tts")

    async def list_voices():
        if error is not None:
            raise error
        return voices if voices is not None else _RAW_VOICES

    module.list_voices = list_voices
    module.Communicate = Communicate
    monkeypatch.setitem(sys.modules, "edge_tts", module)
    return calls


def _block_edge_tts(monkeypatch) -> None:
    """sys.modules 里放 None 使 ``import edge_tts`` 抛 ImportError。"""
    monkeypatch.setitem(sys.modules, "edge_tts", None)


async def _collect(aiter) -> list:
    return [item async for item in aiter]


# ---------------------------------------------------------------------
# EdgeTTSBackend：能力声明
# ---------------------------------------------------------------------
def test_edge_capabilities_declares_no_cloning(monkeypatch):
    _install_fake_edge_tts(monkeypatch)

    caps = EdgeTTSBackend().capabilities

    assert caps.supports_cloning is False
    assert caps.supports_streaming is True
    assert caps.requires_gpu is False
    assert caps.available is True
    assert caps.sample_rate == 24000


async def test_edge_capabilities_unavailable_without_edge_tts(monkeypatch):
    _block_edge_tts(monkeypatch)

    backend = EdgeTTSBackend()

    assert backend.capabilities.available is False
    assert await backend.health_check() is False


async def test_edge_health_check_true_when_importable(monkeypatch):
    _install_fake_edge_tts(monkeypatch)

    assert await EdgeTTSBackend().health_check() is True


# ---------------------------------------------------------------------
# EdgeTTSBackend：合成 / 流式
# ---------------------------------------------------------------------
async def test_edge_synthesize_returns_mp3_result(monkeypatch, tmp_path):
    calls = _install_fake_edge_tts(monkeypatch)
    backend = EdgeTTSBackend()
    opts = SynthesisOptions(rate="+15%", pitch="+2Hz", volume="-5%")

    result = await backend.synthesize("你好世界", "zh-CN-XiaoxiaoNeural", opts)

    assert isinstance(result, AudioResult)
    assert result.data == b"mp3-1mp3-2"
    assert result.format == "mp3"
    assert result.sample_rate == 24000
    assert result.backend == "edge"
    assert result.voice == "zh-CN-XiaoxiaoNeural"
    # opts 的 rate/pitch/volume 必须透传给 edge-tts
    assert calls == [
        {
            "text": "你好世界",
            "voice": "zh-CN-XiaoxiaoNeural",
            "rate": "+15%",
            "pitch": "+2Hz",
            "volume": "-5%",
        }
    ]


async def test_edge_synthesize_defaults_when_opts_omitted(monkeypatch):
    calls = _install_fake_edge_tts(monkeypatch)

    await EdgeTTSBackend().synthesize("hi", "en-US-GuyNeural")

    assert calls[0]["rate"] == "+0%"
    assert calls[0]["pitch"] == "+0Hz"
    assert calls[0]["volume"] == "+0%"


async def test_edge_synthesize_empty_audio_raises(monkeypatch):
    _install_fake_edge_tts(monkeypatch, audio_chunks=[])

    with pytest.raises(TTSBackendError, match="no audio"):
        await EdgeTTSBackend().synthesize("hi", "en-US-GuyNeural")


async def test_edge_stream_yields_sequenced_chunks(monkeypatch):
    _install_fake_edge_tts(monkeypatch)

    chunks = await _collect(
        EdgeTTSBackend().stream("你好", "zh-CN-XiaoxiaoNeural")
    )

    assert [c.data for c in chunks] == [b"mp3-1", b"mp3-2"]
    assert [c.seq for c in chunks] == [0, 1]
    assert all(isinstance(c, AudioChunk) for c in chunks)


async def test_edge_synthesize_engine_error_wrapped(monkeypatch):
    _install_fake_edge_tts(monkeypatch, error=RuntimeError("network down"))

    with pytest.raises(TTSBackendError):
        await EdgeTTSBackend().synthesize("hi", "en-US-GuyNeural")


# ---------------------------------------------------------------------
# EdgeTTSBackend：音色枚举 / 注册
# ---------------------------------------------------------------------
async def test_edge_list_voices_maps_preset_infos(monkeypatch):
    _install_fake_edge_tts(monkeypatch)

    voices = await EdgeTTSBackend().list_voices()

    assert voices == [
        VoiceInfo(
            name="en-US-GuyNeural", language="en-US", kind="preset", gender="Male"
        ),
        VoiceInfo(
            name="zh-CN-XiaoxiaoNeural",
            language="zh-CN",
            kind="preset",
            gender="Female",
        ),
    ]


async def test_edge_register_voice_preset_is_validated_noop(monkeypatch):
    """preset 音色注册 = 校验空操作：返回已有 VoiceInfo，不写任何状态。"""
    _install_fake_edge_tts(monkeypatch)
    backend = EdgeTTSBackend()

    info = await backend.register_voice(
        Path("does-not-matter.wav"), "zh-CN-XiaoxiaoNeural", "zh", "本人同意使用该参考音频"
    )

    assert info.name == "zh-CN-XiaoxiaoNeural"
    assert info.kind == "preset"


async def test_edge_register_voice_rejects_unknown_names(monkeypatch, tmp_path):
    _install_fake_edge_tts(monkeypatch)

    with pytest.raises(TTSBackendError, match="克隆"):
        await EdgeTTSBackend().register_voice(
            tmp_path / "ref.wav", "my-clone", "zh", "本人同意"
        )


async def test_edge_register_voice_requires_consent(monkeypatch):
    _install_fake_edge_tts(monkeypatch)

    with pytest.raises(ValueError, match="consent"):
        await EdgeTTSBackend().register_voice(
            Path("ref.wav"), "zh-CN-XiaoxiaoNeural", "zh", "  "
        )


# ---------------------------------------------------------------------
# IndexTTSClientBackend：httpx.MockTransport 模拟 sidecar
# ---------------------------------------------------------------------
_HEALTH_READY = {"status": "ok", "engine": "indextts", "gpu": True, "voices": 1}
_HEALTH_NONE = {"status": "ok", "engine": "none", "gpu": False, "voices": 0}


def _backend(handler) -> IndexTTSClientBackend:
    """注入 MockTransport 的 IndexTTS 客户端后端（不发真实网络请求）。"""
    return IndexTTSClientBackend(
        base_url="http://sidecar.test",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


async def test_client_health_ok_when_engine_ready():
    backend = _backend(
        lambda request: httpx.Response(200, json=_HEALTH_READY)
    )

    assert await backend.health_check() is True
    caps = backend.capabilities
    assert caps.available is True
    assert caps.supports_cloning is True
    assert caps.requires_gpu is True
    assert caps.supports_streaming is True


async def test_client_health_service_alive_but_engine_none():
    """关键降级语义：服务活着（health=True）但引擎不可用（available=False）。"""
    backend = _backend(lambda request: httpx.Response(200, json=_HEALTH_NONE))

    assert await backend.health_check() is True
    assert backend.capabilities.available is False


async def test_client_health_down_sidecar_never_raises():
    """sidecar 完全不可达：health_check 返回 False，绝不抛异常到上层。"""
    def handler(request):
        raise httpx.ConnectError("connection refused")

    backend = _backend(handler)

    assert await backend.health_check() is False
    assert backend.capabilities.available is False


async def test_client_synthesize_parses_response():
    wav = b"RIFF....WAV"
    requests_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        return httpx.Response(
            200,
            json={
                "audio": base64.b64encode(wav).decode(),
                "sample_rate": 22050,
                "duration_s": 1.5,
                "format": "wav",
            },
        )

    backend = _backend(handler)

    result = await backend.synthesize("你好", "li", SynthesisOptions(speed=1.2))

    assert isinstance(result, AudioResult)
    assert result.data == wav
    assert result.sample_rate == 22050
    assert result.duration_s == 1.5
    assert result.format == "wav"
    assert result.backend == "indextts"
    assert result.voice == "li"
    # 请求体必须是 JSON（text/voice/speed/emotion/duration_factor）
    body = json.loads(requests_seen[0].content)
    assert body == {
        "text": "你好",
        "voice": "li",
        "speed": 1.2,
        "emotion": None,
        "duration_factor": None,
    }


async def test_client_synthesize_503_raises_unavailable():
    def handler(request):
        return httpx.Response(503, json={"detail": "engine unavailable"})

    backend = _backend(handler)

    with pytest.raises(BackendUnavailableError):
        await backend.synthesize("你好", "li")
    assert backend.capabilities.available is False


async def test_client_synthesize_connection_error_raises_unavailable():
    def handler(request):
        raise httpx.ConnectError("refused")

    backend = _backend(handler)

    with pytest.raises(BackendUnavailableError):
        await backend.synthesize("你好", "li")
    assert backend.capabilities.available is False


async def test_client_stream_yields_chunks_with_sample_rate():
    async def pcm_stream():
        yield b"ab"
        yield b"cd"

    def handler(request):
        return httpx.Response(
            200,
            headers={"X-Sample-Rate": "22050", "X-Format": "pcm16"},
            content=pcm_stream(),
        )

    backend = _backend(handler)

    chunks = await _collect(backend.stream("你好", "li"))

    assert [c.data for c in chunks] == [b"ab", b"cd"]
    assert [c.seq for c in chunks] == [0, 1]
    assert all(c.sample_rate == 22050 for c in chunks)


async def test_client_stream_503_raises_unavailable():
    def handler(request):
        return httpx.Response(503, json={"detail": "engine unavailable"})

    backend = _backend(handler)

    with pytest.raises(BackendUnavailableError):
        async for _ in backend.stream("你好", "li"):
            pass


async def test_client_list_voices():
    def handler(request):
        assert request.url.path == "/voices"
        return httpx.Response(
            200,
            json=[
                {"name": "li", "language": "zh", "kind": "cloned"},
                {"name": "ann", "language": "en", "kind": "cloned", "gender": "Female"},
            ],
        )

    backend = _backend(handler)

    voices = await backend.list_voices()

    assert voices == [
        VoiceInfo(name="li", language="zh", kind="cloned"),
        VoiceInfo(name="ann", language="en", kind="cloned", gender="Female"),
    ]


async def test_client_register_voice_posts_consent_and_audio(tmp_path):
    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"ref-audio-bytes")
    seen: list[dict] = []

    def handler(request):
        assert request.url.path == "/voices/register"
        seen.append(json.loads(request.content))
        return httpx.Response(
            200, json={"name": "li", "language": "zh", "kind": "cloned"}
        )

    backend = _backend(handler)

    info = await backend.register_voice(ref, "li", "zh", "本人同意克隆")

    assert info == VoiceInfo(name="li", language="zh", kind="cloned")
    assert seen == [
        {
            "name": "li",
            "language": "zh",
            "consent": "本人同意克隆",
            "audio_base64": base64.b64encode(b"ref-audio-bytes").decode(),
        }
    ]


async def test_client_register_voice_missing_consent_maps_400(tmp_path):
    """sidecar 的 400（consent 缺失）是调用方错误：抛 TTSBackendError 而非降级信号。"""
    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"ref")

    def handler(request):
        return httpx.Response(400, json={"detail": "consent 必填"})

    backend = _backend(handler)

    with pytest.raises(TTSBackendError) as excinfo:
        await backend.register_voice(ref, "li", "zh", "本人同意，但 sidecar 拒绝")
    assert not isinstance(excinfo.value, BackendUnavailableError)


async def test_client_register_voice_connection_error_unavailable(tmp_path):
    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"ref")

    def handler(request):
        raise httpx.ConnectError("refused")

    backend = _backend(handler)

    with pytest.raises(BackendUnavailableError):
        await backend.register_voice(ref, "li", "zh", "同意")


async def test_client_register_voice_requires_consent_client_side():
    """consent 在客户端侧也强制校验——不合规的请求根本不该发出去。"""
    backend = _backend(lambda request: httpx.Response(200, json={}))

    with pytest.raises(ValueError, match="consent"):
        await backend.register_voice(Path("ref.wav"), "li", "zh", "")


# ---------------------------------------------------------------------
# TTSRouter：能力路由 + 自动降级
# ---------------------------------------------------------------------
class FakeBackend(TTSBackend):
    """可编程假后端：记录调用，可注入失败/能力变化。"""

    def __init__(
        self,
        name: str,
        *,
        cloning: bool = False,
        available: bool = True,
        voices: list[VoiceInfo] | None = None,
        probe_result: bool | None = None,
    ) -> None:
        self.name = name
        self.cloning = cloning
        self.available = available
        self.voices = list(voices or [])
        # health_check 的探测副作用：None=不变，True/False=探测后翻转 available
        self.probe_result: bool | None = probe_result
        self.health_probes = 0
        self.list_calls = 0
        self.synth_calls: list[tuple[str, str]] = []
        self.stream_calls: list[tuple[str, str]] = []
        self.register_calls: list[tuple[str, str, str]] = []
        self.fail_synth = False
        self.fail_stream_before_first = False
        self.fail_stream_after_first = False
        self.fail_list = False

    @property
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            supports_cloning=self.cloning,
            supports_streaming=True,
            available=self.available,
        )

    async def synthesize(self, text, voice, opts=None) -> AudioResult:
        self.synth_calls.append((text, voice))
        if self.fail_synth:
            raise BackendUnavailableError(f"{self.name} down")
        return AudioResult(
            data=f"<{self.name}:{voice}>".encode(),
            sample_rate=24000,
            duration_s=None,
            format="mp3",
            backend=self.name,
            voice=voice,
        )

    async def stream(self, text, voice, opts=None) -> AsyncIterator[AudioChunk]:
        self.stream_calls.append((text, voice))
        if self.fail_stream_before_first:
            raise BackendUnavailableError(f"{self.name} stream down")
        yield AudioChunk(seq=0, data=f"<{self.name}:0>".encode(), sample_rate=24000)
        if self.fail_stream_after_first:
            raise BackendUnavailableError(f"{self.name} stream died mid-way")

    async def list_voices(self) -> list[VoiceInfo]:
        self.list_calls += 1
        if self.fail_list:
            raise BackendUnavailableError(f"{self.name} list down")
        return list(self.voices)

    async def register_voice(self, ref_path, name, language, consent) -> VoiceInfo:
        self.register_calls.append((name, language, consent))
        info = VoiceInfo(name=name, language=language, kind="cloned")
        self.voices.append(info)
        return info

    async def health_check(self) -> bool:
        self.health_probes += 1
        if self.probe_result is not None:
            self.available = self.probe_result
        return self.available


def _edge_like() -> FakeBackend:
    return FakeBackend(
        "edge", voices=[VoiceInfo("zh-CN-XiaoxiaoNeural", "zh-CN", "preset")]
    )


def _index_like(voice: str = "li", **kwargs) -> FakeBackend:
    return FakeBackend(
        "indextts", cloning=True, voices=[VoiceInfo(voice, "zh", "cloned")], **kwargs
    )


async def test_router_routes_cloned_voice_to_cloning_backend():
    edge, index = _edge_like(), _index_like()
    router = TTSRouter([edge, index])

    result = await router.synthesize("你好", "li")

    assert index.synth_calls == [("你好", "li")]
    assert edge.synth_calls == []
    assert result.backend == "indextts"
    assert result.voice == "li"


async def test_router_routes_preset_voice_to_edge():
    """preset 音色（含 Neural 命名模式）永远走 Edge，即使克隆后端可用。"""
    edge, index = _edge_like(), _index_like()
    router = TTSRouter([edge, index])

    await router.synthesize("你好", "zh-CN-XiaoxiaoNeural")
    await router.synthesize("hello", "en-US-GuyNeural")  # 模式匹配（不在 edge 列表里）

    assert edge.synth_calls == [
        ("你好", "zh-CN-XiaoxiaoNeural"),
        ("hello", "en-US-GuyNeural"),
    ]
    assert index.synth_calls == []


async def test_router_falls_back_when_cloning_unavailable():
    """IndexTTS 不可用（如 CPU 机器上 sidecar 未起）-> 克隆音色映射到 preset 兜底。"""
    edge, index = _edge_like(), _index_like(available=False, probe_result=False)
    router = TTSRouter([edge, index])

    result = await router.synthesize("你好", "li")

    assert index.synth_calls == []
    assert edge.synth_calls == [("你好", "zh-CN-XiaoxiaoNeural")]
    assert result.backend == "edge"


async def test_router_falls_back_when_cloning_dies_midflight():
    """路由选中 IndexTTS 后它在合成中挂掉 -> 自动降级 Edge 重试。"""
    edge, index = _edge_like(), _index_like()
    index.fail_synth = True
    router = TTSRouter([edge, index])

    result = await router.synthesize("你好", "li")

    assert index.synth_calls == [("你好", "li")]
    assert edge.synth_calls == [("你好", "zh-CN-XiaoxiaoNeural")]
    assert result.backend == "edge"


async def test_router_fallback_uses_preset_map():
    edge, index = _edge_like(), _index_like()
    index.fail_synth = True
    router = TTSRouter([edge, index], preset_map={"li": "en-US-GuyNeural"})

    await router.synthesize("你好", "li")

    assert edge.synth_calls == [("你好", "en-US-GuyNeural")]


async def test_router_unknown_voice_maps_to_default():
    edge, index = _edge_like(), _index_like(available=False, probe_result=False)
    router = TTSRouter([edge, index], default_voice="zh-CN-YunxiNeural")

    await router.synthesize("你好", "mystery-voice")

    assert edge.synth_calls == [("你好", "zh-CN-YunxiNeural")]


async def test_router_stream_falls_back_before_first_chunk():
    edge, index = _edge_like(), _index_like()
    index.fail_stream_before_first = True
    router = TTSRouter([edge, index])

    chunks = await _collect(router.stream("你好", "li"))

    assert [c.data for c in chunks] == [b"<edge:0>"]


async def test_router_stream_error_after_first_chunk_propagates():
    """首块已产出后后端挂掉：无法中途切换格式，错误如实上抛。"""
    edge, index = _edge_like(), _index_like()
    index.fail_stream_after_first = True
    router = TTSRouter([edge, index])

    received: list[bytes] = []
    with pytest.raises(BackendUnavailableError):
        async for chunk in router.stream("你好", "li"):
            received.append(chunk.data)

    assert received == [b"<indextts:0>"]
    assert edge.stream_calls == []


async def test_router_list_voices_merges_and_swallows_unavailable():
    edge, index = _edge_like(), _index_like()
    index.fail_list = True
    router = TTSRouter([edge, index])

    voices = await router.list_voices()

    assert voices == edge.voices  # index 挂了只贡献空集，绝不抛异常


async def test_router_list_voices_merges_both_backends():
    edge, index = _edge_like(), _index_like()
    router = TTSRouter([edge, index])

    voices = await router.list_voices()

    assert {v.name for v in voices} == {"zh-CN-XiaoxiaoNeural", "li"}


async def test_router_register_voice_delegates_and_primes_cache():
    edge, index = _edge_like(), _index_like()
    router = TTSRouter([edge, index])

    info = await router.register_voice(Path("ref.wav"), "new-voice", "zh", "本人同意")

    assert info.kind == "cloned"
    assert index.register_calls == [("new-voice", "zh", "本人同意")]
    # 注册后缓存已更新：直接路由到克隆后端，无需重新枚举
    await router.synthesize("你好", "new-voice")
    assert index.synth_calls == [("你好", "new-voice")]


async def test_router_register_voice_fails_without_cloning_backend():
    router = TTSRouter([_edge_like()])

    with pytest.raises(TTSBackendError, match="克隆"):
        await router.register_voice(Path("ref.wav"), "li", "zh", "本人同意")


async def test_router_probes_cloning_backend_on_first_use():
    """真实 IndexTTS 客户端初始 available=False——路由必须主动探测一次。"""
    edge, index = _edge_like(), _index_like(available=False, probe_result=True)
    router = TTSRouter([edge, index])

    result = await router.synthesize("你好", "li")

    assert index.health_probes == 1
    assert index.synth_calls == [("你好", "li")]
    assert result.backend == "indextts"


async def test_router_probe_skipped_within_ttl():
    edge, index = _edge_like(), _index_like()
    router = TTSRouter([edge, index])

    await router.synthesize("你好", "li")
    await router.synthesize("你好 again", "li")

    assert index.health_probes == 1  # TTL 内不重复探测
    assert index.synth_calls == [("你好", "li"), ("你好 again", "li")]


async def test_router_reprobes_after_midflight_failure():
    """合成中挂掉后：下一次调用重新探测（sidecar 恢复即自动回归）。"""
    edge, index = _edge_like(), _index_like()
    index.fail_synth = True
    router = TTSRouter([edge, index])

    await router.synthesize("你好", "li")   # 失败 -> 降级 edge
    index.fail_synth = False                # sidecar "恢复"
    await router.synthesize("你好", "li")   # 重新探测 -> 回归 index

    assert index.health_probes == 2
    assert index.synth_calls == [("你好", "li"), ("你好", "li")]
    assert len(edge.synth_calls) == 1


async def test_router_no_backends_raises_clear_error():
    router = TTSRouter([])

    with pytest.raises(TTSBackendError, match="后端"):
        await router.synthesize("你好", "li")


# ---------------------------------------------------------------------
# SidecarConfig + build_router（配置层）
# ---------------------------------------------------------------------
def test_sidecar_config_defaults_backward_compatible():
    """默认 enabled=False：不配 sidecar 时行为与旧版完全一致（仅 Edge）。"""
    config = VocalisConfig()

    assert config.sidecar.enabled is False
    assert config.sidecar.base_url == "http://127.0.0.1:8765"
    assert config.sidecar.timeout_s == 30.0
    assert config.sidecar.fallback_voice == "zh-CN-XiaoxiaoNeural"


def test_sidecar_config_from_dict_applies_values():
    raw = {
        "sidecar": {
            "enabled": True,
            "base_url": "http://gpu-box:8765",
            "timeout_s": 60.0,
        }
    }

    config = VocalisConfig.from_dict(raw)

    assert config.sidecar.enabled is True
    assert config.sidecar.base_url == "http://gpu-box:8765"
    assert config.sidecar.timeout_s == 60.0


def test_sidecar_config_omitted_section_keeps_defaults():
    config = VocalisConfig.from_dict({"tts": {"default_profile": "orion"}})

    assert config.sidecar.enabled is False
    assert config.tts.default_profile == "orion"


def test_build_router_edge_only_by_default():
    router = build_router(VocalisConfig())

    assert router.cloning is None
    assert router.fallback.name == "edge"


def test_build_router_adds_indextts_when_enabled():
    config = VocalisConfig()
    config.sidecar.enabled = True
    config.sidecar.base_url = "http://gpu-box:8765"

    router = build_router(config)

    assert router.cloning is not None
    assert router.cloning.name == "indextts"
    assert router.fallback.name == "edge"


# ---------------------------------------------------------------------
# m1：TTL 探测 single-flight（TTL 过期瞬间的并发请求只发一次 health_check）
# ---------------------------------------------------------------------
async def test_router_probe_single_flight_on_ttl_expiry():
    """TTL 过期后 5 个并发请求：只触发 1 次新的 health_check。"""
    edge, index = _edge_like(), _index_like()
    router = TTSRouter([edge, index])

    await router.synthesize("你好", "li")  # 首次使用：探测 1 次
    assert index.health_probes == 1

    router._probe_ts = time.monotonic() - 10_000.0  # 强制 TTL 过期
    await asyncio.gather(*(router.synthesize(f"第{i}句", "li") for i in range(5)))

    assert index.health_probes == 2  # 5 个并发只补了 1 次探测
    assert len(index.synth_calls) == 6  # 全部请求都正常路由到克隆后端


# ---------------------------------------------------------------------
# m2：list_voices 响应解析统一转 TTSBackendError（不裸抛）
# ---------------------------------------------------------------------
async def test_client_list_voices_non_list_payload_raises_backend_error():
    backend = _backend(lambda request: httpx.Response(200, json={"not": "a list"}))

    with pytest.raises(TTSBackendError, match="格式非法") as excinfo:
        await backend.list_voices()

    assert not isinstance(excinfo.value, BackendUnavailableError)


async def test_client_list_voices_non_dict_entries_raises_backend_error():
    backend = _backend(lambda request: httpx.Response(200, json=["li", 42]))

    with pytest.raises(TTSBackendError, match="格式非法"):
        await backend.list_voices()


async def test_client_synthesize_non_dict_payload_raises_backend_error():
    backend = _backend(lambda request: httpx.Response(200, json=["not", "an", "object"]))

    with pytest.raises(TTSBackendError, match="格式非法"):
        await backend.synthesize("你好", "li")


# ---------------------------------------------------------------------
# m5：ConsentMissingError 双继承（TTSBackendError + ValueError）
# ---------------------------------------------------------------------
def test_require_consent_raises_dual_inheritance_error():
    assert issubclass(ConsentMissingError, TTSBackendError)
    assert issubclass(ConsentMissingError, ValueError)

    with pytest.raises(ValueError):  # 按历史习惯捕获 ValueError 也命中
        require_consent("")
    with pytest.raises(TTSBackendError):  # 按 TTS 错误体系捕获也命中
        require_consent("   ")


async def test_edge_register_missing_consent_catchable_both_ways(monkeypatch):
    _install_fake_edge_tts(monkeypatch)
    backend = EdgeTTSBackend()

    with pytest.raises(TTSBackendError):  # 不再是裸 ValueError
        await backend.register_voice(Path("ref.wav"), "zh-CN-XiaoxiaoNeural", "zh", "")


# ---------------------------------------------------------------------
# m7：preset_map 配置透传 + TTSRouter.aclose 生命周期
# ---------------------------------------------------------------------
def test_sidecar_config_preset_map_from_dict():
    config = VocalisConfig.from_dict(
        {"sidecar": {"preset_map": {"li": "en-US-GuyNeural"}}}
    )

    assert config.sidecar.preset_map == {"li": "en-US-GuyNeural"}


def test_sidecar_config_preset_map_default_empty():
    assert VocalisConfig().sidecar.preset_map == {}


def test_build_router_passes_preset_map():
    config = VocalisConfig()
    config.sidecar.preset_map = {"li": "zh-CN-YunxiNeural"}

    router = build_router(config)

    assert router.preset_map == {"li": "zh-CN-YunxiNeural"}


async def test_router_aclose_closes_backends_with_aclose():
    """aclose 调用实现了 aclose 协议的后端（httpx 连接池），无该协议的跳过。"""
    edge, index = _edge_like(), _index_like()
    closed: list[str] = []

    async def _index_aclose() -> None:
        closed.append(index.name)

    index.aclose = _index_aclose  # FakeBackend 动态挂上 aclose（模拟 sidecar 客户端）
    router = TTSRouter([edge, index])

    await router.aclose()

    assert closed == ["indextts"]  # edge 无 aclose：自动跳过不报错


async def test_router_aclose_swallows_backend_close_failure():
    edge, index = _edge_like(), _index_like()

    async def _boom() -> None:
        raise RuntimeError("close failed")

    index.aclose = _boom
    router = TTSRouter([edge, index])

    await router.aclose()  # 单个后端关闭失败不级联：清理路径绝不抛

