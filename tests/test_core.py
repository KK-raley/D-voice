"""Unit tests that run without heavy ML deps installed."""

from __future__ import annotations

import numpy as np
import pytest


def _fake_embedding(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.normal(size=256).astype(np.float32)
    return v / np.linalg.norm(v)


class TestEventBus:
    def test_publish_subscribe_roundtrip(self):
        import asyncio

        from vocalis.server.events import EventBus, EventType

        bus = EventBus()
        received = []

        async def scenario():
            q = bus.subscribe("task.*")
            await bus.publish(EventType.TASK_STARTED, agent="echo", instruction="x")
            received.append(await asyncio.wait_for(q.get(), timeout=1.0))

        asyncio.run(scenario())
        assert received[0].type is EventType.TASK_STARTED
        assert received[0].data["agent"] == "echo"

    def test_pattern_wildcard(self):
        import asyncio

        from vocalis.server.events import EventBus, EventType

        bus = EventBus()
        received = []

        async def scenario():
            q = bus.subscribe("*")
            await bus.publish(EventType.SYSTEM, message="hi")
            received.append(await asyncio.wait_for(q.get(), timeout=1.0))

        asyncio.run(scenario())
        assert received[0].data["message"] == "hi"

    def test_history_is_bounded(self):
        import asyncio

        from vocalis.server.events import EventBus, EventType

        bus = EventBus(history_size=8)

        async def pump():
            for _ in range(20):
                await bus.publish(EventType.SYSTEM)

        asyncio.run(pump())
        assert len(bus.history) == 8


class TestVoiceProfile:
    def test_apply_delta(self):
        from vocalis.voice.tts import VoiceProfile

        p = VoiceProfile(rate="+0%", pitch="+0Hz", volume="+0%")
        p.apply_delta(rate=15, pitch=-4, volume=20)
        assert p.rate == "+15%"
        assert p.pitch == "-4Hz"
        assert p.volume == "+20%"

    def test_delta_clamps(self):
        from vocalis.voice.tts import VoiceProfile

        p = VoiceProfile()
        p.apply_delta(rate=500)
        assert p.rate == "+100%"


class TestVoiceGateMocked:
    def test_verify_accepts_owner(self, monkeypatch, tmp_path):
        pytest.importorskip("numpy")
        monkeypatch.setattr("vocalis.config.profiles_dir", lambda: tmp_path)
        import json

        from vocalis.voice.gate import VoiceGate

        # Write profiles directly (bypass resemblyzer dependency).
        alice = _fake_embedding(1)
        bob = _fake_embedding(2)
        for name, emb in (("alice", alice), ("bob", bob)):
            (tmp_path / f"{name}.voiceprofile.json").write_text(
                json.dumps({"user": name, "embedding": emb.tolist()}),
                encoding="utf-8",
            )

        gate = VoiceGate.__new__(VoiceGate)  # skip config load side-effects
        from vocalis.config import VocalisConfig

        gate.config = VocalisConfig()
        gate.backend = "resemblyzer"
        gate.threshold = 0.8
        gate.enroll_consistency = 0.75
        gate.profiles = {"alice": alice, "bob": bob}

        decision = gate.verify_embedding(alice + np.float32(0.01))
        assert decision.accepted and decision.user == "alice"
        assert decision.similarity > 0.9

        impostor = _fake_embedding(999)
        decision = gate.verify_embedding(impostor)
        assert not decision.accepted and decision.user is None

    def test_profiles_are_namespaced_per_backend(self, monkeypatch, tmp_path):
        pytest.importorskip("numpy")
        monkeypatch.setattr("vocalis.voice.gate.profiles_dir", lambda: tmp_path)
        monkeypatch.setattr("vocalis.config.profiles_dir", lambda: tmp_path)
        import json

        from vocalis.voice.gate import VoiceGate

        alice = _fake_embedding(1)
        # legacy (resemblyzer) profile and an eres2net-large profile coexist
        (tmp_path / "alice.voiceprofile.json").write_text(
            json.dumps({"user": "alice", "embedding": alice.tolist()}),
            encoding="utf-8",
        )
        (tmp_path / "alice.eres2net-large.voiceprofile.json").write_text(
            json.dumps(
                {
                    "user": "alice",
                    "backend": "eres2net-large",
                    "embedding": _fake_embedding(2).tolist(),
                }
            ),
            encoding="utf-8",
        )

        gate = VoiceGate.__new__(VoiceGate)
        gate.config = None
        gate.backend = "eres2net-large"
        gate.threshold = 0.55
        gate.enroll_consistency = 0.70
        gate.profiles = {}
        gate.load_profiles()
        assert list(gate.profiles) == ["alice"]
        probe = _fake_embedding(2)
        assert gate.verify_embedding(probe).accepted

        gate.backend = "resemblyzer"
        gate.threshold = 0.8
        gate.profiles = {}
        gate.load_profiles()
        assert list(gate.profiles) == ["alice"]
        assert gate.verify_embedding(alice).accepted


class TestSpeakerBackends:
    def test_backend_defaults_exist(self):
        from vocalis.voice.speaker import BACKEND_DEFAULTS

        assert set(BACKEND_DEFAULTS) >= {"resemblyzer", "eres2net-large"}
        for defaults in BACKEND_DEFAULTS.values():
            assert 0.0 < defaults["threshold"] < 1.0

    def test_embed_unknown_backend_raises(self):
        import numpy as np

        from vocalis.voice.speaker import SpeakerEncoderError, embed_utterance

        with pytest.raises(SpeakerEncoderError):
            embed_utterance(np.zeros(16000, dtype=np.float32), backend="nope")

    def test_resolve_backend_rejects_unknown(self):
        from vocalis.voice.speaker import SpeakerEncoderError, resolve_backend

        with pytest.raises(SpeakerEncoderError):
            resolve_backend("nope")


class TestBrainOpenAICompat:
    def test_chat_via_openai_compatible(self, monkeypatch):
        import asyncio

        from vocalis.config import VocalisConfig
        from vocalis.dvoice.assistant import DVoiceBrain

        cfg = VocalisConfig()
        cfg.brain.backend = "openai-compatible"
        cfg.brain.base_url = "http://localhost:9999/v1"
        brain = DVoiceBrain(config=cfg)

        class FakeResp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"choices": [{"message": {"content": "All good."}}]}

        class FakeClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

            async def post(self, url, json=None, headers=None):
                FakeClient.last_url = url
                FakeClient.last_json = json
                return FakeResp()

        monkeypatch.setattr("vocalis.dvoice.assistant.httpx.AsyncClient", FakeClient)
        reply = asyncio.run(brain.chat("status?"))
        assert reply == "All good."
        assert FakeClient.last_url == "http://127.0.0.1:9999/v1/chat/completions"
        assert FakeClient.last_json["model"] == cfg.brain.model

    def test_base_url_required_for_compat(self):
        from vocalis.config import VocalisConfig
        from vocalis.dvoice.assistant import DVoiceBrain

        cfg = VocalisConfig()
        cfg.brain.backend = "openai-compatible"
        cfg.brain.base_url = None
        brain = DVoiceBrain(config=cfg)
        import asyncio

        # unreachable base_url -> degrade to rules, not crash
        reply = asyncio.run(brain.chat("status"))
        assert "task" in reply.lower() or "idle" in reply.lower()


class TestCliAgent:
    def test_build_args_placeholder(self):
        from vocalis.agents.cli_agent import build_args

        assert build_args(["codex", "exec", "{instruction}"], "fix the bug") == [
            "codex",
            "exec",
            "fix the bug",
        ]

    def test_build_args_appends_without_placeholder(self):
        from vocalis.agents.cli_agent import build_args

        assert build_args(["opencode", "run"], "ship it") == [
            "opencode",
            "run",
            "ship it",
        ]

    def test_empty_command_rejected(self):
        import pytest

        from vocalis.agents.cli_agent import build_args

        with pytest.raises(ValueError):
            build_args([], "x")


class TestCliAgentsConfig:
    def test_roundtrip(self):
        from vocalis.config import CliAgentConfig, VocalisConfig

        cfg = VocalisConfig()
        cfg.cli_agents = [
            CliAgentConfig(name="codex", command=["codex", "exec", "{instruction}"]),
            CliAgentConfig(name="opencode", command=["opencode", "run"]),
        ]
        raw = cfg.to_dict()
        assert raw["cli_agents"][0]["name"] == "codex"

        restored = VocalisConfig.from_dict(raw)
        assert [a.name for a in restored.cli_agents] == ["codex", "opencode"]
        assert restored.cli_agents[1].command == ["opencode", "run"]


class TestCommander:
    def test_status_query_detection(self):
        from vocalis.agents.echo import EchoAgent
        from vocalis.agents.registry import AgentRegistry
        from vocalis.dvoice.commander import Commander

        registry = AgentRegistry()
        registry.register(EchoAgent())
        commander = Commander(registry)
        assert commander.plan("当前状态怎么样").is_status_query
        assert commander.plan("what's the progress").is_status_query
        assert not commander.plan("run the tests").is_status_query

    def test_question_vs_order(self):
        from vocalis.agents.echo import EchoAgent
        from vocalis.agents.registry import AgentRegistry
        from vocalis.dvoice.commander import Commander

        registry = AgentRegistry()
        registry.register(EchoAgent())
        commander = Commander(registry)
        assert commander.plan("what is quantum tunneling?").question is not None
        assert commander.plan("帮我重构测试文件").assignments

    def test_fanout_parse(self):
        from vocalis.agents.echo import EchoAgent
        from vocalis.agents.registry import AgentRegistry
        from vocalis.dvoice.commander import Commander

        registry = AgentRegistry()
        registry.register(EchoAgent())
        commander = Commander(registry)
        plan = commander.plan("让echo 做第一件事；然后 echo 做第二件事")
        assert len(plan.assignments) == 2
        assert plan.assignments[0][0] == "echo"


class TestServerListen:
    """POST /api/listen 安全语音入口（离线桩解码、声纹和转写）。

    lifespan 会构造真实 AppState，所以在 TestClient 启动"之后"替换
    state.transcriber / 打桩 decode_audio，而不是替换 state 本身。
    """

    @staticmethod
    def _stub_transcribe(text="你好，今天天气怎么样", fail=False):
        import vocalis.voice.asr as asr_module
        from vocalis.voice.asr import Transcription

        calls = []

        class Stub:
            def transcribe(self, audio, sample_rate=16000):
                if fail:
                    raise RuntimeError("faster-whisper is required for ASR")
                calls.append(sample_rate)
                return Transcription(text=text, language="zh", segments=[])

        monkey_target = Stub
        return asr_module, Transcription, calls, monkey_target

    def test_listen_rejects_empty_body(self):
        from fastapi.testclient import TestClient

        import vocalis.server.app as app_module

        with TestClient(app_module.app) as client:
            r = client.post("/api/listen", content=b"")
        assert r.status_code == 400

    def test_listen_undecodable_audio_is_400(self, monkeypatch):
        from fastapi.testclient import TestClient

        import vocalis.server.app as app_module
        import vocalis.voice.asr as asr_module

        def bad_decode(data, max_duration_s):
            raise ValueError("bad")

        monkeypatch.setattr(asr_module, "decode_audio", bad_decode)
        with TestClient(app_module.app) as client:
            r = client.post("/api/listen", content=b"garbage")
        assert r.status_code == 400
        assert "cannot decode" in r.json()["detail"]

    def test_listen_transcribes_and_caches_transcriber(self, monkeypatch):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        import numpy as np
        from fastapi.testclient import TestClient

        import vocalis.server.app as app_module
        import vocalis.voice.asr as asr_module
        import vocalis.voice.gate as gate_module
        from vocalis.voice.asr import Transcription

        calls = []

        class Stub:
            def transcribe(self, audio, sample_rate=16000):
                calls.append(sample_rate)
                text = "你好 d-voice" if len(calls) == 1 else "你好，今天天气怎么样"
                return Transcription(text=text, language="zh", segments=[{
                    "words": [{"word": text, "start": 0.1, "end": 0.9}],
                }])

        monkeypatch.setattr(
            asr_module, "decode_audio",
            lambda data, max_duration_s: (np.full(44100, 0.1, dtype=np.float32), 44100),
        )
        monkeypatch.setattr(asr_module, "Transcriber", lambda cfg: Stub())
        monkeypatch.setattr(gate_module, "VoiceGate", lambda cfg: SimpleNamespace(
            verify=lambda audio, sample_rate: SimpleNamespace(
                accepted=True, user="owner", similarity=0.95),
        ))
        with TestClient(app_module.app) as client:
            st = app_module.state
            st.transcriber = None
            st.commander.execute = AsyncMock(return_value={"reply": "天气晴朗"})
            r = client.post("/api/listen", content=b"fake-webm")
            assert r.status_code == 200
            assert r.json()["kind"] == "wake"
            assert r.json()["text"] == ""  # waking only authenticates; no command yet
            st.commander.execute.assert_not_awaited()
            assert calls == [44100]  # 解码采样率被透传
            cached = st.transcriber
            assert cached is not None  # 已缓存
            r2 = client.post("/api/listen", content=b"fake-webm")
            assert r2.status_code == 200
            assert r2.json()["text"] == "你好，今天天气怎么样"
            assert r2.json()["kind"] == "command"
            st.commander.execute.assert_awaited_once_with(
                "你好，今天天气怎么样", user="owner", voiceprint="accepted"
            )
            assert st.transcriber is cached  # 复用同一实例

    def test_listen_missing_voice_stack_is_503(self, monkeypatch):
        from fastapi.testclient import TestClient

        import vocalis.server.app as app_module
        import vocalis.voice.asr as asr_module

        def no_av(data, max_duration_s):
            raise RuntimeError("PyAV is required")

        monkeypatch.setattr(asr_module, "decode_audio", no_av)
        with TestClient(app_module.app) as client:
            r = client.post("/api/listen", content=b"fake")
        assert r.status_code == 503

    def test_listen_transcriber_failure_fails_closed(self, monkeypatch):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        import numpy as np
        from fastapi.testclient import TestClient

        import vocalis.server.app as app_module
        import vocalis.voice.asr as asr_module
        import vocalis.voice.gate as gate_module

        class Broken:
            def transcribe(self, audio, sample_rate=16000):
                raise RuntimeError("faster-whisper is required for ASR")

        monkeypatch.setattr(
            asr_module, "decode_audio",
            lambda data, max_duration_s: (np.full(16000, 0.1, dtype=np.float32), 16000),
        )
        monkeypatch.setattr(asr_module, "Transcriber", lambda cfg: Broken())
        monkeypatch.setattr(gate_module, "VoiceGate", lambda cfg: SimpleNamespace(
            verify=lambda audio, sample_rate: SimpleNamespace(
                accepted=True, user="owner", similarity=0.95),
        ))
        with TestClient(app_module.app) as client:
            app_module.state.commander.execute = AsyncMock()
            r = client.post("/api/listen", content=b"fake")
            app_module.state.commander.execute.assert_not_awaited()
        assert r.status_code == 200
        assert r.json()["kind"] == "error"
        assert r.json()["state"] == "standby"
        assert r.json()["reason"] == "local_processing_failed"
        assert r.json()["text"] == ""


class TestBrainProbe:
    """available() 对远程 API 401/403 必须报离线（防止假'在线'）。"""

    @staticmethod
    def _brain_with_status(status: int):
        import asyncio

        from vocalis.config import VocalisConfig
        from vocalis.dvoice.assistant import DVoiceBrain

        cfg = VocalisConfig()
        cfg.brain.backend = "openai-compatible"
        cfg.brain.base_url = "http://localhost:9999/v1"
        brain = DVoiceBrain(config=cfg)

        class FakeResp:
            def __init__(self, code):
                self.status_code = code

        class FakeClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

            async def get(self, url, headers=None):
                return FakeResp(status)

        import vocalis.dvoice.assistant as assistant_module

        orig = assistant_module.httpx.AsyncClient
        assistant_module.httpx.AsyncClient = FakeClient
        try:
            return asyncio.run(brain.available())
        finally:
            assistant_module.httpx.AsyncClient = orig

    def test_401_is_offline(self):
        assert self._brain_with_status(401) is False

    def test_403_is_offline(self):
        assert self._brain_with_status(403) is False

    def test_200_is_online(self):
        assert self._brain_with_status(200) is True


class TestCommanderVoiceRouting:
    """语音无标点的意图路由：问句/中性语句 -> 大脑，祈使句 -> 派发。"""

    @staticmethod
    def _commander():
        from vocalis.agents.echo import EchoAgent
        from vocalis.agents.registry import AgentRegistry
        from vocalis.dvoice.commander import Commander

        registry = AgentRegistry()
        registry.register(EchoAgent())
        return Commander(registry)

    def test_asr_question_without_punctuation_goes_to_brain(self):
        """回归：无标点、问话词在句中的提问曾被误派发为 echo 任务。"""
        plan = self._commander().plan("你的名字叫什么你能不能看到我目前的桌面你又没有监视的功能")
        assert plan.question is not None
        assert not plan.assignments

    def test_neutral_sentence_goes_to_brain(self):
        plan = self._commander().plan("写一首关于秋天的诗")
        assert plan.question is not None

    def test_imperative_still_dispatches(self):
        plan = self._commander().plan("帮我重构测试文件")
        assert plan.assignments
        assert plan.question is None

    def test_english_imperative_still_dispatches(self):
        plan = self._commander().plan("run the release summary")
        assert plan.assignments


class TestServerVision:
    """/api/vision/* 视觉端点 + 命令管线视觉意图路由（完全离线桩）。"""

    @staticmethod
    def _stub_observation():
        from vocalis.vision.screen import ScreenObservation

        return ScreenObservation(
            title="ZCode",
            text="测试文本第一行\n测试文本第二行",
            lines=["测试文本第一行", "测试文本第二行"],
            engine="stub",
        )

    @staticmethod
    def _install(monkeypatch):
        import vocalis.server.app as app_module
        import vocalis.vision.screen as screen_module

        async def fake_observe(**kw):
            return TestServerVision._stub_observation()

        monkeypatch.setattr(screen_module, "observe_screen", fake_observe)
        return app_module

    def test_vision_look_answers_with_screen_context(self, monkeypatch):
        from fastapi.testclient import TestClient

        app_module = self._install(monkeypatch)
        with TestClient(app_module.app) as client:
            r = client.post("/api/vision/look", json={"question": "屏幕上有什么？"})
            assert r.status_code == 200
            body = r.json()
            assert body["ok"] is True
            assert body["title"] == "ZCode"
            assert "测试文本第一行" in body["text"]
            assert body["reply"]  # 规则兜底也应有回复

    def test_vision_look_without_question_returns_digest_only(self, monkeypatch):
        from fastapi.testclient import TestClient

        app_module = self._install(monkeypatch)
        with TestClient(app_module.app) as client:
            r = client.post("/api/vision/look", json={})
            assert r.status_code == 200
            body = r.json()
            assert body["reply"] == ""
            assert "测试文本" in body["text"]

    def test_command_vision_intent_routes_to_vision(self, monkeypatch):
        from fastapi.testclient import TestClient

        app_module = self._install(monkeypatch)
        with TestClient(app_module.app) as client:
            r = client.post("/api/command", json={"text": "你能不能看到我目前的桌面", "speak": False})
            assert r.status_code == 200
            assert r.json()["kind"] == "vision"

    def test_command_imperative_with_keyword_still_dispatches(self, monkeypatch):
        from fastapi.testclient import TestClient

        app_module = self._install(monkeypatch)
        with TestClient(app_module.app) as client:
            r = client.post("/api/command", json={"text": "让 echo 监视测试", "speak": False})
            assert r.status_code == 200
            assert r.json()["kind"] != "vision"

    def test_screen_watch_toggle(self, monkeypatch):
        from fastapi.testclient import TestClient

        app_module = self._install(monkeypatch)
        with TestClient(app_module.app) as client:
            r = client.post("/api/vision/watch", json={"enabled": True, "interval_s": 0.05})
            assert r.status_code == 200
            assert r.json()["running"] is True
            r2 = client.post("/api/vision/watch", json={"enabled": False})
            assert r2.status_code == 200
            assert r2.json()["running"] is False
