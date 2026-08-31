"""IndexTTS sidecar 服务测试（FastAPI TestClient + mock 引擎，完全离线）。

覆盖：能力降级（engine="none" 时 health 与合成 503 分离）、consent 缺失
400、注册音色磁盘持久化（跨重启）、合成/流式响应契约、环境探测
（torch/indextts 缺失时的降级路径）与 CLI 启动参数；以及安全面——
token 鉴权（401/200）、请求体前置限制（413）、text 超长（422）、
被篡改注册表的白名单校验（M3）、infer 并发串行化（M1）。
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import threading
import time
import wave
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vocalis.voice.backends.base import SynthesisOptions
from vocalis.voice.sidecar.engine import (
    EngineAudio,
    IndexTTSEngine,
    probe_gpu,
    probe_indextts,
    probe_state,
)
from vocalis.voice.sidecar.registry import VoiceRegistry
from vocalis.voice.sidecar.server import SidecarState, create_app


# ---------------------------------------------------------------------
# mock 引擎：确定性输出（1 秒 WAV / 两个 PCM 块）
# ---------------------------------------------------------------------
class MockEngine:
    """假合成引擎：记录调用，返回可预测的 WAV/PCM。"""

    sample_rate = 22050

    def __init__(self) -> None:
        self.synth_calls: list[tuple[str, str, SynthesisOptions]] = []
        self.stream_calls: list[tuple[str, str, SynthesisOptions]] = []

    @staticmethod
    def _wav_bytes(n_frames: int) -> bytes:
        buf = BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(22050)
            wf.writeframes(b"\x00\x01" * n_frames)
        return buf.getvalue()

    async def synthesize(self, text: str, ref_audio: str, opts: SynthesisOptions):
        self.synth_calls.append((text, ref_audio, opts))
        return EngineAudio(
            wav=self._wav_bytes(22050), sample_rate=22050, duration_s=1.0
        )

    async def stream(self, text: str, ref_audio: str, opts: SynthesisOptions):
        self.stream_calls.append((text, ref_audio, opts))
        yield b"\x01\x00" * 100
        yield b"\x02\x00" * 100


def _app(
    tmp_path: Path,
    engine=None,
    gpu: bool = True,
    *,
    token: str | None = None,
    max_body_bytes: int | None = None,
) -> FastAPI:
    state = SidecarState(engine=engine, gpu=gpu)
    kwargs: dict = {"token": token}
    if max_body_bytes is not None:
        kwargs["max_body_bytes"] = max_body_bytes
    return create_app(state, registry_path=tmp_path / "sidecar", **kwargs)


def _register(
    client: TestClient,
    name: str = "li",
    consent: str = "本人同意克隆",
    headers: dict | None = None,
):
    return client.post(
        "/voices/register",
        json={
            "name": name,
            "language": "zh",
            "consent": consent,
            "audio_base64": base64.b64encode(b"ref-bytes").decode(),
        },
        headers=headers,
    )


# ---------------------------------------------------------------------
# /health：服务存活与引擎可用分离（关键降级设计）
# ---------------------------------------------------------------------
def test_health_reports_engine_none_without_gpu(tmp_path):
    client = TestClient(_app(tmp_path, engine=None, gpu=False))

    resp = client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body == {"status": "ok", "engine": "none", "gpu": False, "voices": 0}


def test_health_reports_engine_when_ready(tmp_path):
    client = TestClient(_app(tmp_path, engine=MockEngine(), gpu=True))

    body = client.get("/health").json()

    assert body["status"] == "ok"
    assert body["engine"] == "indextts"
    assert body["gpu"] is True


def test_health_counts_registered_voices(tmp_path):
    client = TestClient(_app(tmp_path, engine=MockEngine()))

    _register(client)
    body = client.get("/health").json()

    assert body["voices"] == 1


# ---------------------------------------------------------------------
# 合成端点：无引擎 503，有引擎正常
# ---------------------------------------------------------------------
def test_synthesize_503_when_engine_none(tmp_path):
    client = TestClient(_app(tmp_path, engine=None, gpu=False))

    resp = client.post("/synthesize", json={"text": "你好", "voice": "li"})

    assert resp.status_code == 503
    assert "不可用" in resp.json()["detail"]


def test_stream_503_when_engine_none(tmp_path):
    client = TestClient(_app(tmp_path, engine=None, gpu=False))

    resp = client.post("/synthesize/stream", json={"text": "你好", "voice": "li"})

    assert resp.status_code == 503


def test_synthesize_returns_base64_wav(tmp_path):
    engine = MockEngine()
    client = TestClient(_app(tmp_path, engine=engine))
    _register(client)

    resp = client.post(
        "/synthesize",
        json={"text": "你好世界", "voice": "li", "speed": 1.2, "emotion": "happy"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert base64.b64decode(body["audio"]) == engine._wav_bytes(22050)
    assert body["sample_rate"] == 22050
    assert body["duration_s"] == 1.0
    assert body["format"] == "wav"
    # 引擎收到 (text, 参考音频路径, opts)；opts 携带请求里的控制参数
    text, ref_audio, opts = engine.synth_calls[0]
    assert text == "你好世界"
    assert Path(ref_audio).exists()
    assert Path(ref_audio).read_bytes() == b"ref-bytes"
    assert opts.speed == 1.2
    assert opts.emotion == "happy"
    assert opts.duration_factor is None


def test_synthesize_unknown_voice_404(tmp_path):
    client = TestClient(_app(tmp_path, engine=MockEngine()))

    resp = client.post("/synthesize", json={"text": "你好", "voice": "ghost"})

    assert resp.status_code == 404
    assert "ghost" in resp.json()["detail"]


def test_stream_returns_pcm_chunks_with_header(tmp_path):
    engine = MockEngine()
    client = TestClient(_app(tmp_path, engine=engine))
    _register(client)

    resp = client.post(
        "/synthesize/stream", json={"text": "你好", "voice": "li", "duration_factor": 2.0}
    )

    assert resp.status_code == 200
    assert resp.headers["X-Sample-Rate"] == "22050"
    assert resp.content == b"\x01\x00" * 100 + b"\x02\x00" * 100
    # 流式路径同样传递请求参数
    _, _, opts = engine.stream_calls[0]
    assert opts.duration_factor == 2.0


def test_stream_unknown_voice_404(tmp_path):
    client = TestClient(_app(tmp_path, engine=MockEngine()))

    resp = client.post("/synthesize/stream", json={"text": "你好", "voice": "ghost"})

    assert resp.status_code == 404


# ---------------------------------------------------------------------
# /voices/register：consent 合规 + 持久化
# ---------------------------------------------------------------------
def test_register_requires_consent(tmp_path):
    client = TestClient(_app(tmp_path, engine=MockEngine()))

    resp = _register(client, consent="")

    assert resp.status_code == 400
    assert "consent" in resp.json()["detail"]


def test_register_rejects_unsafe_names(tmp_path):
    """音色名会进入文件路径：路径穿越必须被拒绝。"""
    client = TestClient(_app(tmp_path, engine=MockEngine()))

    resp = _register(client, name="../../evil")

    assert resp.status_code == 400


def test_register_invalid_base64_400(tmp_path):
    client = TestClient(_app(tmp_path, engine=MockEngine()))

    resp = client.post(
        "/voices/register",
        json={
            "name": "li",
            "language": "zh",
            "consent": "本人同意克隆",
            "audio_base64": "@@not-base64@@",
        },
    )

    assert resp.status_code == 400


def test_register_missing_audio_422(tmp_path):
    client = TestClient(_app(tmp_path, engine=MockEngine()))

    resp = client.post(
        "/voices/register",
        json={"name": "li", "language": "zh", "consent": "同意"},
    )

    assert resp.status_code == 422


def test_register_persists_and_lists(tmp_path):
    client = TestClient(_app(tmp_path, engine=MockEngine()))

    resp = _register(client)

    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "li"
    assert body["language"] == "zh"
    assert body["kind"] == "cloned"

    voices = client.get("/voices").json()
    assert [v["name"] for v in voices] == ["li"]
    assert voices[0]["kind"] == "cloned"
    # 注册表与参考音频都落盘
    assert (tmp_path / "sidecar" / "voices.json").exists()
    refs = list((tmp_path / "sidecar" / "refs").iterdir())
    assert len(refs) == 1
    assert refs[0].read_bytes() == b"ref-bytes"


def test_register_persistence_across_restart(tmp_path):
    """注册表持久化：重启 sidecar（新 app 实例，同数据目录）音色仍在。"""
    _register(TestClient(_app(tmp_path, engine=MockEngine())))

    client2 = TestClient(_app(tmp_path, engine=MockEngine()))
    voices = client2.get("/voices").json()

    assert [v["name"] for v in voices] == ["li"]


def test_register_same_name_overwrites(tmp_path):
    client = TestClient(_app(tmp_path, engine=MockEngine()))

    _register(client, name="li")
    _register(client, name="li")

    voices = client.get("/voices").json()
    assert len(voices) == 1


# ---------------------------------------------------------------------
# VoiceRegistry：单元级
# ---------------------------------------------------------------------
def test_registry_add_and_get(tmp_path):
    registry = VoiceRegistry(tmp_path / "reg")

    entry = registry.add("li", "zh", "本人同意", b"audio-bytes")

    assert entry["name"] == "li"
    assert entry["kind"] == "cloned"
    assert registry.get("li") == entry
    assert registry.ref_path("li").read_bytes() == b"audio-bytes"


def test_registry_rejects_unsafe_name(tmp_path):
    registry = VoiceRegistry(tmp_path / "reg")

    with pytest.raises(ValueError):
        registry.add("a/b", "zh", "同意", b"x")


def test_registry_overwrite_same_name(tmp_path):
    registry = VoiceRegistry(tmp_path / "reg")

    registry.add("li", "zh", "同意", b"old")
    registry.add("li", "en", "同意", b"new")

    assert len(registry.list()) == 1
    assert registry.ref_path("li").read_bytes() == b"new"
    assert registry.get("li")["language"] == "en"


def test_registry_survives_reload(tmp_path):
    root = tmp_path / "reg"
    VoiceRegistry(root).add("li", "zh", "同意", b"audio")

    reloaded = VoiceRegistry(root)

    assert [e["name"] for e in reloaded.list()] == ["li"]
    assert reloaded.ref_path("li").read_bytes() == b"audio"


# ---------------------------------------------------------------------
# 环境探测：无 torch / 无 indextts 的降级路径
# ---------------------------------------------------------------------
def test_probe_gpu_false_without_torch(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", None)

    assert probe_gpu() is False


def test_probe_indextts_false_without_package(monkeypatch):
    monkeypatch.setitem(sys.modules, "indextts", None)

    assert probe_indextts() is False


def test_probe_state_degrades_without_gpu(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", None)
    monkeypatch.setitem(sys.modules, "indextts", None)

    state = probe_state()

    assert state.engine is None
    assert state.gpu is False


# ---------------------------------------------------------------------
# CLI 入口：python -m vocalis.voice.sidecar
# ---------------------------------------------------------------------
def test_main_runs_uvicorn_with_defaults(monkeypatch, tmp_path):
    from vocalis.voice.sidecar import __main__ as sidecar_main

    # 默认数据目录在 ~/.vocalis 下：测试重定向到 tmp_path，不污染真实主目录
    monkeypatch.setenv("VOCALIS_HOME", str(tmp_path))
    captured: dict = {}

    def fake_run(app, **kwargs):
        captured["app"] = app
        captured.update(kwargs)

    monkeypatch.setattr(sidecar_main.uvicorn, "run", fake_run)

    sidecar_main.main([])

    assert isinstance(captured["app"], FastAPI)
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8765


def test_main_accepts_host_port_model_dir(monkeypatch, tmp_path):
    from vocalis.voice.sidecar import __main__ as sidecar_main

    monkeypatch.setenv("VOCALIS_HOME", str(tmp_path))
    captured: dict = {}

    def fake_run(app, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(sidecar_main.uvicorn, "run", fake_run)

    sidecar_main.main(["--host", "0.0.0.0", "--port", "9000", "--model-dir", "ckpts"])

    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 9000


# ---------------------------------------------------------------------
# M1：IndexTTSEngine 并发合成时 infer 串行化（推理锁）
# ---------------------------------------------------------------------
class _FakeModel:
    """假 IndexTTS2 模型：写最小 WAV + 记录 infer 并发重入情况。"""

    def __init__(self) -> None:
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def infer(self, *, spk_audio_prompt: str, text: str, output_path: str, **_: object) -> None:
        with self.lock:
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        with wave.open(output_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(22050)
            wf.writeframes(b"\x00\x01" * 22050)  # 1 秒
        time.sleep(0.05)  # 放大并发窗口：无锁时两个 to_thread 几乎必重叠
        with self.lock:
            self.active -= 1


async def test_engine_serializes_concurrent_infer(tmp_path):
    """两个并发 synthesize：infer 各执行一次且全程无重入（_infer_lock 生效）。"""
    engine = IndexTTSEngine(model_dir=tmp_path / "ckpts")
    model = _FakeModel()
    engine._model = model  # 注入假模型，绕过真实懒加载（无需 indextts 环境）
    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"ref-bytes")

    results = await asyncio.gather(
        engine.synthesize("第一条", str(ref), SynthesisOptions()),
        engine.synthesize("第二条", str(ref), SynthesisOptions()),
    )

    assert model.calls == 2
    assert model.max_active == 1  # 串行化铁证：infer 从未并发重入
    assert all(isinstance(r, EngineAudio) and r.sample_rate == 22050 for r in results)


# ---------------------------------------------------------------------
# M2：token 鉴权（401/200）+ 请求体前置限制（413）+ text 超长（422）
# ---------------------------------------------------------------------
_TOKEN = "unit-test-token"


def test_token_missing_header_401(tmp_path):
    client = TestClient(_app(tmp_path, engine=MockEngine(), token=_TOKEN))

    resp = client.get("/health")

    assert resp.status_code == 401
    assert "token" in resp.json()["detail"]


def test_token_wrong_value_401(tmp_path):
    client = TestClient(_app(tmp_path, engine=MockEngine(), token=_TOKEN))

    resp = client.get("/health", headers={"Authorization": "Bearer wrong-token"})

    assert resp.status_code == 401


def test_token_correct_bearer_200(tmp_path):
    client = TestClient(_app(tmp_path, engine=MockEngine(), token=_TOKEN))

    resp = client.get("/health", headers={"Authorization": f"Bearer {_TOKEN}"})

    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_token_via_x_sidecar_token_header_200(tmp_path):
    client = TestClient(_app(tmp_path, engine=MockEngine(), token=_TOKEN))

    resp = client.get("/health", headers={"X-Sidecar-Token": _TOKEN})

    assert resp.status_code == 200


def test_health_also_requires_token(tmp_path):
    """/health 不豁免：与业务端点同一套鉴权（简单一致）。"""
    client = TestClient(_app(tmp_path, engine=MockEngine(), token=_TOKEN))

    assert client.get("/health").status_code == 401
    assert client.post(
        "/synthesize", json={"text": "你好", "voice": "li"}
    ).status_code == 401
    assert client.get("/voices").status_code == 401


def test_token_authenticates_synthesize_end_to_end(tmp_path):
    """带对 token 的注册 + 合成全链路畅通。"""
    client = TestClient(_app(tmp_path, engine=MockEngine(), token=_TOKEN))
    headers = {"Authorization": f"Bearer {_TOKEN}"}

    reg = client.post(
        "/voices/register",
        json={
            "name": "li",
            "language": "zh",
            "consent": "本人同意",
            "audio_base64": base64.b64encode(b"ref").decode(),
        },
        headers=headers,
    )
    synth = client.post(
        "/synthesize", json={"text": "你好", "voice": "li"}, headers=headers
    )

    assert reg.status_code == 200
    assert synth.status_code == 200


def test_no_token_configured_means_no_auth(tmp_path):
    """未设置 token（默认本机回环场景）：请求无需鉴权头照常工作。"""
    client = TestClient(_app(tmp_path, engine=MockEngine()))

    resp = client.get("/health")

    assert resp.status_code == 200


def test_body_over_content_length_limit_413(tmp_path):
    """Content-Length 超限的请求在 middleware 层直接 413（不进处理器）。"""
    client = TestClient(
        _app(tmp_path, engine=MockEngine(), token=_TOKEN, max_body_bytes=1024)
    )

    resp = client.post(
        "/synthesize",
        json={"text": "a" * 1500, "voice": "li"},  # body ~1.5 KB > 1 KB 上限
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )

    assert resp.status_code == 413
    assert "请求体过大" in resp.json()["detail"]


def test_body_within_limit_passes(tmp_path):
    """上限内的请求不受影响（前置限制不误伤正常请求）。"""
    client = TestClient(
        _app(tmp_path, engine=MockEngine(), token=_TOKEN, max_body_bytes=64 * 1024)
    )
    _register(client, headers={"Authorization": f"Bearer {_TOKEN}"})

    resp = client.post(
        "/synthesize",
        json={"text": "你好", "voice": "li"},
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )

    assert resp.status_code == 200


def test_text_over_max_length_422(tmp_path):
    """text 超 2000 字符：pydantic 校验 422（body 大小在默认 25 MB 限内）。"""
    client = TestClient(_app(tmp_path, engine=MockEngine(), token=_TOKEN))

    resp = client.post(
        "/synthesize",
        json={"text": "长" * 2001, "voice": "li"},
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )

    assert resp.status_code == 422


# ---------------------------------------------------------------------
# M3：被篡改注册表的白名单校验（加载期丢弃 + ref_path 双保险）
# ---------------------------------------------------------------------
def _tampered_registry(tmp_path: Path) -> Path:
    """写一个含四类条目的 voices.json：三类非法（应被丢弃）+ 一个合法。"""
    root = tmp_path / "tampered"
    root.mkdir(parents=True)
    entries = {
        # 1) name 带 ../（路径穿越）
        "evil": {
            "name": "../../etc/passwd",
            "ref_file": "../../etc/passwd.wav",
            "language": "zh",
            "consent": "x",
            "created_at": "2026-01-01T00:00:00+00:00",
        },
        # 2) ref_file 与音色名不匹配（可指向任意文件）
        "badref": {
            "name": "badref",
            "ref_file": "someone-else.wav",
            "language": "zh",
            "consent": "x",
            "created_at": "2026-01-01T00:00:00+00:00",
        },
        # 3) 缺必备字段（consent/created_at 没有）
        "nofield": {
            "name": "nofield",
            "ref_file": "nofield.wav",
            "language": "zh",
        },
        # 4) 完全合法
        "li": {
            "name": "li",
            "ref_file": "li.wav",
            "language": "zh",
            "consent": "本人同意",
            "created_at": "2026-01-01T00:00:00+00:00",
        },
    }
    (root / "voices.json").write_text(json.dumps(entries), encoding="utf-8")
    return root


def test_registry_load_drops_tampered_entries(tmp_path):
    registry = VoiceRegistry(_tampered_registry(tmp_path))

    assert [e["name"] for e in registry.list()] == ["li"]
    assert registry.get("evil") is None
    assert registry.get("badref") is None
    assert registry.get("nofield") is None


def test_registry_ref_path_escapes_refs_dir_raises(tmp_path):
    """双保险：即使条目混进 _entries，ref_path 的 resolve 检查也会拦下。"""
    registry = VoiceRegistry(_tampered_registry(tmp_path))
    # 直接注入恶意条目模拟"绕过加载期校验"的运行期状态
    registry._entries["sneaky"] = {
        "name": "sneaky",
        "ref_file": "../secrets.wav",
        "language": "zh",
        "consent": "x",
        "created_at": "t",
    }

    with pytest.raises(ValueError, match="逃逸"):
        registry.ref_path("sneaky")


def test_synthesize_with_tampered_registry_returns_404_not_500(tmp_path):
    """被篡改条目在加载期丢弃：对它合成得到 404（未注册），绝不能 500。"""
    root = _tampered_registry(tmp_path)
    app = create_app(
        SidecarState(engine=MockEngine(), gpu=True), registry_path=root
    )
    client = TestClient(app)

    for evil_name in ("evil", "badref", "nofield"):
        resp = client.post(
            "/synthesize", json={"text": "你好", "voice": evil_name}
        )
        assert resp.status_code == 404, f"{evil_name} 应被视为未注册而非 500"
