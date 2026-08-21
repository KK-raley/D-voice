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
        gate.profiles = {"alice": alice, "bob": bob}

        decision = gate.verify_embedding(alice + np.float32(0.01))
        assert decision.accepted and decision.user == "alice"
        assert decision.similarity > 0.9

        impostor = _fake_embedding(999)
        decision = gate.verify_embedding(impostor)
        assert not decision.accepted and decision.user is None


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
