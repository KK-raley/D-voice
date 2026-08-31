"""Deterministic authorization tests; no microphone, model download or network."""
from __future__ import annotations

import asyncio
import sys
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import numpy as np
import pytest

from vocalis.config import VocalisConfig
from vocalis.voice.asr import Transcription
from vocalis.voice.standby import StandbySession, run_microphone
from vocalis.voice.wakeword import WakeWordDetector, match_phrase


def audio(seconds=1.0, amplitude=0.1):
    return np.full(int(16000 * seconds), amplitude, dtype=np.float32)


def decision(user="alice"):
    return SimpleNamespace(accepted=user is not None, user=user, similarity=0.95)


def transcript(text="hey d-voice", words=None):
    if words is None:
        words = [{"word": text, "start": 0.1, "end": 0.9}]
    return Transcription(text=text, language="en",
                         segments=[{"start": 0, "end": 1, "text": text, "words": words}])


@pytest.fixture
def rig():
    config = VocalisConfig()
    clock = Mock(return_value=100.0)
    gate = Mock()
    gate.verify.return_value = decision()
    transcriber = Mock()
    transcriber.transcribe.return_value = transcript()
    commander = SimpleNamespace(execute=AsyncMock(return_value={"reply": "done"}))
    session = StandbySession(config, gate, transcriber, commander, clock=clock)
    return SimpleNamespace(session=session, gate=gate, asr=transcriber,
                           commander=commander, clock=clock, config=config)


async def test_silence_costs_no_asr_gate_or_dispatch(rig):
    for _ in range(20):
        assert (await rig.session.process_audio(audio(amplitude=0)))["kind"] == "ignored"
    rig.gate.verify.assert_not_called()
    rig.asr.transcribe.assert_not_called()
    rig.commander.execute.assert_not_awaited()


async def test_unknown_voice_never_transcribed_or_dispatched(rig):
    rig.gate.verify.return_value = decision(None)
    result = await rig.session.process_audio(audio())
    assert result["kind"] == "rejected" and result["state"] == "standby"
    rig.asr.transcribe.assert_not_called()
    rig.commander.execute.assert_not_awaited()


async def test_enrolled_but_no_wake_does_not_expose_transcript_or_call_model(rig):
    rig.asr.transcribe.return_value = transcript("a private background conversation")
    result = await rig.session.process_audio(audio())
    assert result["text"] == "" and result["reason"] == "no_wake_phrase"
    rig.commander.execute.assert_not_awaited()


async def test_wake_handshake_drops_inline_command_and_then_accepts_verified_turn(rig):
    rig.asr.transcribe.return_value = transcript("hey d-voice delete my files")
    assert (await rig.session.process_audio(audio()))["kind"] == "wake"
    rig.commander.execute.assert_not_awaited()
    rig.asr.transcribe.return_value = transcript("what time is it")
    result = await rig.session.process_audio(audio())
    assert result["kind"] == "command" and result["user"] == "alice"
    rig.commander.execute.assert_awaited_once_with(
        "what time is it", user="alice", voiceprint="accepted"
    )


async def test_different_enrolled_speaker_cannot_take_over(rig):
    await rig.session.process_audio(audio())
    rig.gate.verify.return_value = decision("bob")
    assert (await rig.session.process_audio(audio()))["kind"] == "rejected"
    rig.commander.execute.assert_not_awaited()


async def test_mixed_speakers_rejected_even_if_whole_clip_matches(rig):
    rig.gate.verify.side_effect = [decision(), decision(), decision(None)]
    assert (await rig.session.process_audio(audio(2.0)))["reason"] == "mixed_or_different_speaker"
    rig.commander.execute.assert_not_awaited()


async def test_wake_phrase_itself_must_match_same_speaker(rig):
    rig.gate.verify.side_effect = [decision(), decision(None)]
    assert (await rig.session.process_audio(audio()))["reason"] == "wake_speaker_mismatch"
    assert rig.session.snapshot()["state"] == "standby"


@pytest.mark.parametrize("words", [[], [{"word": "hey d-voice", "start": 0, "end": 99}],
                                   [{"word": "hey d-voice", "start": float("nan"), "end": 1}]])
async def test_missing_or_invalid_alignment_never_wakes(rig, words):
    rig.asr.transcribe.return_value = transcript(words=words)
    assert (await rig.session.process_audio(audio()))["reason"] == "wake_phrase_not_aligned"


async def test_short_phrase_cannot_borrow_owner_speech_to_pass(rig):
    rig.asr.transcribe.return_value = transcript("owner talking hey d-voice", words=[
        {"word": "owner talking", "start": 0, "end": 0.6},
        {"word": "hey d-voice", "start": 0.65, "end": 0.8},
    ])
    assert (await rig.session.process_audio(audio()))["reason"] == "wake_phrase_not_aligned"


async def test_timeout_resets_identity_and_ignores_unwoken_command(rig):
    await rig.session.process_audio(audio())
    rig.clock.return_value = 131
    assert rig.session.snapshot()["state"] == "standby"
    rig.asr.transcribe.return_value = transcript("tell me something")
    assert (await rig.session.process_audio(audio()))["kind"] == "ignored"
    rig.commander.execute.assert_not_awaited()


async def test_sleep_is_exact_local_command_no_llm(rig):
    await rig.session.process_audio(audio())
    rig.asr.transcribe.return_value = transcript("休眠。")
    assert (await rig.session.process_audio(audio()))["kind"] == "sleep"
    rig.commander.execute.assert_not_awaited()


async def test_discussing_sleep_is_not_a_sleep_command(rig):
    await rig.session.process_audio(audio())
    rig.asr.transcribe.return_value = transcript("不要休眠")
    assert (await rig.session.process_audio(audio()))["kind"] == "command"


async def test_gate_failure_closes_session(rig):
    await rig.session.process_audio(audio())
    rig.gate.verify.side_effect = RuntimeError("sensitive internal details")
    result = await rig.session.process_audio(audio())
    assert result["state"] == "standby" and result["kind"] == "error"
    assert "sensitive" not in str(result)
    assert "dependencies" in result["error"]
    rig.commander.execute.assert_not_awaited()


async def test_manual_stop_during_asr_does_not_resurrect_session(rig):
    rig.asr.transcribe.side_effect = lambda *a, **k: (rig.session.sleep(), transcript())[1]
    result = await rig.session.process_audio(audio())
    assert result["state"] == "standby" and result["kind"] == "ignored"
    rig.commander.execute.assert_not_awaited()


async def test_cancellation_returns_to_standby(rig):
    await rig.session.process_audio(audio())
    rig.commander.execute.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await rig.session.process_audio(audio())
    assert rig.session.snapshot()["state"] == "standby"


@pytest.mark.parametrize("bad_audio", [np.array([]), audio(16), np.ones((16000, 2)),
                                        audio(amplitude=float("nan")), audio(amplitude=32767)])
async def test_invalid_audio_rejected_without_model_calls(rig, bad_audio):
    assert (await rig.session.process_audio(bad_audio))["kind"] == "rejected"
    rig.gate.verify.assert_not_called()


def test_latin_wake_word_does_not_match_inside_unrelated_words():
    assert not match_phrase("supercomputer", ["computer"])
    assert not match_phrase("hey d voices", ["hey d voice"])
    assert match_phrase("你好d-voice", ["d voice"])


async def test_disabled_wake_fails_closed(rig):
    rig.config.wake_word.enabled = False
    assert (await rig.session.process_audio(audio()))["reason"] == "wake_disabled"
    rig.gate.verify.assert_not_called()
    assert not WakeWordDetector(rig.config.wake_word).process_text("hey d-voice").detected


async def test_gate_and_asr_run_off_event_loop(rig):
    main_thread = threading.get_ident()
    worker_ids = []

    def verify(*_args, **_kwargs):
        worker_ids.append(threading.get_ident())
        return decision()

    def transcribe(*_args, **_kwargs):
        worker_ids.append(threading.get_ident())
        return transcript()

    rig.gate.verify.side_effect = verify
    rig.asr.transcribe.side_effect = transcribe
    assert (await rig.session.process_audio(audio()))["kind"] == "wake"
    assert len(worker_ids) >= 3
    assert all(identifier != main_thread for identifier in worker_ids)


async def test_slow_command_stays_active_until_processing_finishes(rig):
    await rig.session.process_audio(audio())
    entered, finish = asyncio.Event(), asyncio.Event()

    async def slow_command(_text, **_kwargs):
        entered.set()
        await finish.wait()
        return {"reply": "slow answer"}

    rig.commander.execute.side_effect = slow_command
    task = asyncio.create_task(rig.session.process_audio(audio()))
    await asyncio.wait_for(entered.wait(), 2)
    try:
        rig.clock.return_value = 300.0  # Qwen takes much longer than idle timeout
        snapshot = rig.session.snapshot()
        assert snapshot["state"] == "active"
        assert snapshot["processing"] is True
        assert snapshot["user"] == "alice"
        assert snapshot["remaining_s"] == rig.config.standby.idle_timeout_s
    finally:
        finish.set()
    result = await task
    assert result["kind"] == "command" and result["processing"] is False
    assert result["remaining_s"] == rig.config.standby.idle_timeout_s
    rig.clock.return_value = 329.0
    assert rig.session.snapshot()["state"] == "active"
    rig.clock.return_value = 330.0
    assert rig.session.snapshot()["state"] == "standby"


async def test_manual_sleep_during_command_discards_late_reply(rig):
    await rig.session.process_audio(audio())
    entered, finish = asyncio.Event(), asyncio.Event()

    async def slow_command(_text, **_kwargs):
        entered.set()
        await finish.wait()
        return {"reply": "must not be spoken after sleep"}

    rig.commander.execute.side_effect = slow_command
    task = asyncio.create_task(rig.session.process_audio(audio()))
    await asyncio.wait_for(entered.wait(), 2)
    try:
        snapshot = rig.session.sleep()
        assert snapshot["state"] == "standby"
        assert snapshot["user"] is None
        assert snapshot["processing"] is True  # already-authorized work can still finish
    finally:
        finish.set()
    result = await task
    assert result["state"] == "standby" and result["processing"] is False
    assert result["kind"] == "ignored"
    assert result["text"] == result["reply"] == ""


async def test_failed_command_clears_processing_and_session(rig):
    await rig.session.process_audio(audio())
    rig.commander.execute.side_effect = RuntimeError("model stopped")
    result = await rig.session.process_audio(audio())
    assert result["state"] == "standby" and result["processing"] is False
    assert result["kind"] == "error"


async def test_microphone_long_turn_discards_tail_and_requires_new_wake(rig, monkeypatch):
    """Exercise actual VAD/endpointing with deterministic fake audio hardware."""
    rig.config.standby.max_utterance_s = 2
    await rig.session.process_audio(audio())
    rig.asr.transcribe.return_value = transcript("run dangerous tail fragment")
    started = threading.Event()
    stream_instances = []

    class InputStream:
        def __init__(self, **kwargs):
            self.callback = kwargs["callback"]
            self.active = False
            self.closed = False
            stream_instances.append(self)

        def start(self):
            self.active = True
            started.set()

        def close(self):
            self.active = False
            self.closed = True

    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(InputStream=InputStream))
    results = []
    stop = asyncio.Event()
    listener = asyncio.create_task(run_microphone(rig.session, results.append, stop))
    assert await asyncio.to_thread(started.wait, 2)
    frame = (0.2 * np.sin(np.arange(480) * 2 * np.pi * 220 / 16000)).astype(np.float32)
    try:
        # Four continuous seconds exceed the cap. The two-second tail MUST
        # not become a fresh command, even after the capture chunker resets.
        for part in [frame] * 140 + [np.zeros(480, np.float32)] * 45:
            stream_instances[0].callback(part[:, None], 480, None, None)
            await asyncio.sleep(0.002)
        await asyncio.sleep(0.05)
    finally:
        stop.set()
        await asyncio.wait_for(listener, 2)
    assert any(result["reason"] == "utterance_too_long" for result in results)
    rig.commander.execute.assert_not_awaited()
    assert rig.session.snapshot()["state"] == "standby"
    assert stream_instances[0].closed
