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
        assert FakeClient.last_url == "http://localhost:9999/v1/chat/completions"
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
