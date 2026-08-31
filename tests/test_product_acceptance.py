"""Independent product acceptance: authorization, privacy and HTTP entry points.

Speaker/ASR fakes isolate control-flow guarantees. These tests do not measure
real biometric accuracy, spoof resistance or microphone hardware quality.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest
from fastapi.testclient import TestClient

import vocalis.server.app as app_module
from vocalis.config import VocalisConfig
from vocalis.voice.standby import StandbySession


class Speaker:
    def __init__(self):
        self.user = "owner"
        self.calls = 0

    def verify(self, audio, sample_rate=16000):
        self.calls += 1
        return SimpleNamespace(accepted=self.user is not None, user=self.user,
                               similarity=0.95 if self.user else 0.1)


class Speech:
    def __init__(self):
        self.text = "hey d-voice"
        self.aligned = True
        self.calls = 0

    def transcribe(self, audio, sample_rate=16000):
        self.calls += 1
        words = [{"word": self.text, "start": 0.05, "end": 0.9}] if self.aligned else []
        return SimpleNamespace(text=self.text, segments=[{"words": words}])


@pytest.fixture
def product():
    config = VocalisConfig()
    gate, speech = Speaker(), Speech()
    commander = SimpleNamespace(execute=AsyncMock(return_value={"reply": "处理完成"}))
    now = [100.0]
    session = StandbySession(config, gate, speech, commander, clock=lambda: now[0])
    return SimpleNamespace(session=session, config=config, gate=gate, speech=speech,
                           commander=commander, now=now,
                           audio=np.full(16000, 0.1, dtype=np.float32))


@pytest.mark.asyncio
async def test_sleeping_conversation_never_reaches_commander_or_returns_transcript(product):
    product.speech.text = "这是房间里的私密谈话"
    for _ in range(8):
        response = await product.session.process_audio(product.audio)
        assert response["text"] == response["reply"] == ""
        assert response["state"] == "standby"
    product.commander.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_stranger_wake_is_rejected_before_asr(product):
    product.gate.user = None
    response = await product.session.process_audio(product.audio)
    assert response["kind"] == "rejected"
    assert response["user"] is None
    assert product.speech.calls == 0
    product.commander.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_wake_and_command_same_breath_only_opens_session(product):
    product.speech.text = "hey d-voice 立即删除我的文件"
    response = await product.session.process_audio(product.audio)
    assert response["kind"] == "wake"
    product.commander.execute.assert_not_awaited()
    product.speech.text = "今天有什么安排"
    response = await product.session.process_audio(product.audio)
    assert response["kind"] == "command"
    product.commander.execute.assert_awaited_once_with(
        "今天有什么安排", user="owner", voiceprint="accepted"
    )


@pytest.mark.asyncio
async def test_different_enrolled_person_cannot_use_owner_session(product):
    await product.session.process_audio(product.audio)
    product.gate.user = "another-enrolled-user"
    product.speech.text = "执行一个任务"
    response = await product.session.process_audio(product.audio)
    assert response["kind"] == "rejected"
    product.commander.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_idle_expiry_requires_a_fresh_wake(product):
    await product.session.process_audio(product.audio)
    product.now[0] += product.config.standby.idle_timeout_s + 1
    product.speech.text = "现在执行任务"
    response = await product.session.process_audio(product.audio)
    assert response["state"] == "standby"
    assert response["text"] == ""
    product.commander.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_absent_wake_alignment_fails_closed(product):
    product.speech.aligned = False
    response = await product.session.process_audio(product.audio)
    assert response["kind"] == "rejected"
    assert response["state"] == "standby"
    product.commander.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_wake_cannot_disable_authorization(product):
    product.config.wake_word.enabled = False
    response = await product.session.process_audio(product.audio)
    assert response["state"] == "standby"
    assert product.gate.calls == product.speech.calls == 0
    product.commander.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_asr_failure_closes_active_session(product, monkeypatch):
    await product.session.process_audio(product.audio)

    def broken(*args, **kwargs):
        raise RuntimeError("sensitive internal failure")

    monkeypatch.setattr(product.speech, "transcribe", broken)
    response = await product.session.process_audio(product.audio)
    assert response["kind"] == "error"
    assert response["state"] == "standby"
    assert "sensitive" not in str(response)
    product.commander.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_silence_and_nonfinite_audio_never_run_models(product):
    for audio in [np.zeros(16000), np.full(16000, np.nan), np.full(16000, np.inf)]:
        response = await product.session.process_audio(audio)
        assert response["kind"] in ("ignored", "rejected")
    assert product.gate.calls == product.speech.calls == 0
    product.commander.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_sleep_phrase_is_local_and_no_longer_authorizes_commands(product):
    await product.session.process_audio(product.audio)
    product.speech.text = "休眠"
    response = await product.session.process_audio(product.audio)
    assert response["kind"] == "sleep"
    product.speech.text = "继续执行"
    response = await product.session.process_audio(product.audio)
    assert response["state"] == "standby"
    product.commander.execute.assert_not_awaited()


@pytest.fixture
def http_client(monkeypatch, product):
    state = SimpleNamespace(microphone_task=None, voice_lock=asyncio.Lock(),
                            config=product.config,
                            voice_session=lambda: product.session)
    monkeypatch.setattr(app_module, "state", state)
    monkeypatch.setattr(app_module, "_REQUIRED_TOKEN", "acceptance-token")
    client = TestClient(app_module.app)
    try:
        yield client
    finally:
        client.close()


@pytest.mark.parametrize("path,body", [
    ("/api/speak", {"text": "hello"}),
    ("/api/command", {"text": "hello"}),
    ("/api/ask", {"text": "hello"}),
    ("/api/standby/microphone", {"enabled": True}),
    ("/api/standby/sleep", {}),
])
def test_mutating_voice_endpoints_require_token(http_client, path, body):
    assert http_client.post(path, json=body).status_code == 401


@pytest.mark.parametrize("path", ["/api/events/history", "/api/vision/state", "/api/standby"])
def test_private_transcripts_screen_history_and_identity_require_token(http_client, path):
    assert http_client.get(path).status_code == 401


def test_upload_limit_is_checked_before_decode_or_models(http_client, product):
    response = http_client.post("/api/listen", content=b"x" * (5 * 1024 * 1024 + 1),
                                headers={"X-Vocalis-Token": "acceptance-token"})
    assert response.status_code == 413
    assert product.gate.calls == product.speech.calls == 0


def test_browser_voice_uses_same_authorization_session(http_client, product, monkeypatch):
    import vocalis.voice.asr as asr

    monkeypatch.setattr(asr, "decode_audio", lambda *args: (product.audio, 16000))
    product.gate.user = None
    response = http_client.post("/api/listen", content=b"fake encoded clip",
                                headers={"X-Vocalis-Token": "acceptance-token"})
    assert response.status_code == 200
    assert response.json()["kind"] == "rejected"
    assert response.json()["text"] == ""
    product.commander.execute.assert_not_awaited()
