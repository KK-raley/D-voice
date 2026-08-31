"""HTTP/lifecycle regressions with no live microphone, network or voice models."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import numpy as np
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import vocalis.server.app as server
from vocalis.config import VocalisConfig
from vocalis.server.events import EventBus


@pytest.fixture
def state(monkeypatch):
    state = object.__new__(server.AppState)
    state.config = VocalisConfig()
    state.microphone_task = None
    state.microphone_error = None
    state.microphone_stop = asyncio.Event()
    state.voice_lock = asyncio.Lock()
    state.event_bus = EventBus(history_size=20)
    state.standby = SimpleNamespace(
        gate=SimpleNamespace(profile_count=lambda: 1),
        process_audio=AsyncMock(return_value={"kind": "command", "state": "active",
                                              "text": "hello", "reply": "done"}),
        snapshot=lambda: {"state": "standby", "user": None},
        sleep=Mock(return_value={"state": "standby", "user": None}),
    )
    state.screen_watcher = SimpleNamespace(stop=AsyncMock())
    state.tts = SimpleNamespace(speak=AsyncMock(), synthesize=AsyncMock())
    state.commander = SimpleNamespace(execute=AsyncMock())
    monkeypatch.setattr(server, "state", state)
    monkeypatch.setattr(server, "_REQUIRED_TOKEN", "")
    return state


@pytest.fixture
def client(state):
    client = TestClient(server.app)
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def decoder(monkeypatch):
    import vocalis.voice.asr as asr

    decode = Mock(return_value=(np.ones(16000, dtype=np.float32) * 0.1, 16000))
    monkeypatch.setattr(asr, "decode_audio", decode)
    return decode


def test_upload_does_not_dispatch_a_second_time(client, state, decoder):
    response = client.post("/api/listen", content=b"encoded audio")
    assert response.status_code == 200
    assert response.json()["reply"] == "done"
    state.standby.process_audio.assert_awaited_once()
    state.commander.execute.assert_not_awaited()
    state.tts.speak.assert_not_awaited()


def test_continuous_microphone_blocks_browser_upload_before_decode(client, state, decoder):
    state.microphone_task = SimpleNamespace(done=lambda: False)
    assert client.post("/api/listen", content=b"encoded").status_code == 409
    decoder.assert_not_called()
    state.standby.process_audio.assert_not_awaited()


def test_busy_session_rejects_upload(client, state, decoder):
    state.voice_lock = SimpleNamespace(locked=lambda: True)
    assert client.post("/api/listen", content=b"encoded").status_code == 409
    state.standby.process_audio.assert_not_awaited()


def test_empty_upload_does_not_run_decoder(client, decoder):
    assert client.post("/api/listen", content=b"").status_code == 400
    decoder.assert_not_called()


@pytest.mark.parametrize("failure,code", [
    (ValueError("audio exceeds 15 seconds"), 400),
    (RuntimeError("PyAV is required"), 503),
])
def test_decode_failures_have_actionable_http_status(client, state, decoder, failure, code):
    decoder.side_effect = failure
    assert client.post("/api/listen", content=b"encoded").status_code == code
    state.standby.process_audio.assert_not_awaited()


def test_microphone_without_enrollment_fails_closed(client, state):
    state.standby.gate.profile_count = lambda: 0
    response = client.post("/api/standby/microphone", json={"enabled": True})
    assert response.status_code == 503
    assert "enroll" in response.json()["detail"]
    assert state.microphone_task is None


def test_sleep_button_stops_screen_capture_and_forgets_voice_owner(client, state):
    response = client.post("/api/standby/sleep")
    assert response.status_code == 200
    assert state.microphone_stop.is_set()
    state.screen_watcher.stop.assert_awaited_once()
    assert state.standby.sleep.call_count >= 1


@pytest.mark.parametrize("payload,media_type", [
    (b"RIFF\x00\x00\x00\x00WAVEfmt ", "audio/wav"),
    (b"ID3encoded-mp3", "audio/mpeg"),
])
def test_tts_audio_format_matches_response_header(client, state, payload, media_type):
    state.tts.synthesize.return_value = SimpleNamespace(ok=True, audio_bytes=payload)
    response = client.post("/api/speak", json={"text": "你好"})
    assert response.status_code == 200
    assert response.headers["content-type"] == media_type
    assert response.content == payload


def test_tts_requires_configured_token_before_synthesis(client, state, monkeypatch):
    monkeypatch.setattr(server, "_REQUIRED_TOKEN", "secret")
    assert client.post("/api/speak", json={"text": "你好"}).status_code == 401
    state.tts.synthesize.assert_not_awaited()


@pytest.mark.asyncio
async def test_microphone_device_failure_sleeps_and_surfaces_error(state, monkeypatch):
    import vocalis.voice.standby as voice

    monkeypatch.setattr(voice, "run_microphone", AsyncMock(side_effect=RuntimeError("device missing")))
    await state.listen_microphone()
    assert state.microphone_error == "device missing"
    state.standby.sleep.assert_called_with(reason="microphone_stopped")
    assert state.event_bus.history[-1].data["reason"] == "microphone_error"
    state.tts.speak.assert_not_awaited()


@pytest.mark.asyncio
async def test_stop_cancels_and_joins_capture_before_clearing_handle(state):
    entered, closed = asyncio.Event(), asyncio.Event()

    async def capture():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    task = asyncio.create_task(capture())
    state.microphone_task = task
    await entered.wait()
    await state.stop_microphone()
    assert task.done()
    assert closed.is_set()
    assert state.microphone_task is None
    state.standby.sleep.assert_called_with(reason="microphone_off")


@pytest.mark.asyncio
async def test_microphone_started_during_decode_cannot_mix_input_sources(state, monkeypatch):
    import vocalis.voice.asr as asr

    entered, release = threading.Event(), threading.Event()

    def slow_decode(*args):
        entered.set()
        if not release.wait(timeout=2):
            raise RuntimeError("test decode release timed out")
        return np.full(16000, 0.1, dtype=np.float32), 16000

    async def stream():
        yield b"encoded microphone clip"

    monkeypatch.setattr(asr, "decode_audio", slow_decode)
    upload = asyncio.create_task(server.listen(SimpleNamespace(stream=stream)))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        state.microphone_task = SimpleNamespace(done=lambda: False)
        release.set()
        with pytest.raises(HTTPException) as failure:
            await upload
        assert failure.value.status_code == 409
        state.standby.process_audio.assert_not_awaited()
    finally:
        release.set()
        if not upload.done():
            upload.cancel()
        await asyncio.gather(upload, return_exceptions=True)
