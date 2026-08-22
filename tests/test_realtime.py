"""Realtime voice-interaction tests (chunking / VAD / turn detection / barge-in).

Fully offline: no microphone, no network, no torch. Test signals are
synthesized numpy frames (silence + sine "speech") and the turn detector is
driven purely by injected timestamps.
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from vocalis.voice.realtime import (
    BargeInController,
    EnergyVAD,
    RealtimeEvent,
    RealtimeSession,
    TurnDetector,
    VadEvent,
)

SR = 16000
FRAME_MS = 30
FRAME = int(SR * FRAME_MS / 1000)  # 480 samples
FRAME_S = FRAME_MS / 1000.0


# ----------------------------------------------------------------------
# Synthetic signal helpers
# ----------------------------------------------------------------------
def speech_frame(amp: float = 0.3, freq: float = 220.0) -> np.ndarray:
    t = np.arange(FRAME, dtype=np.float32) / SR
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def silence_frame() -> np.ndarray:
    return np.zeros(FRAME, dtype=np.float32)


def frames(*durations_s: float, amp: float = 0.3) -> list[np.ndarray]:
    """Build a frame list alternating speech/silence from durations.

    ``frames(1.0, 3.0, 1.0)`` = 1 s silence, 3 s speech, 1 s silence.
    Odd positions (0-based) are speech.
    """
    out: list[np.ndarray] = []
    for i, d in enumerate(durations_s):
        n = int(round(d / FRAME_S))
        f = speech_frame(amp=amp) if i % 2 == 1 else silence_frame()
        out.extend([f.copy() for _ in range(n)])
    return out


def feed_all(vad: EnergyVAD, fr: list[np.ndarray]) -> list[VadEvent]:
    return [e for e in (vad.feed(f) for f in fr) if e is not None]


# ----------------------------------------------------------------------
# EnergyVAD
# ----------------------------------------------------------------------
def test_vad_silence_emits_nothing():
    vad = EnergyVAD()
    assert feed_all(vad, frames(2.0)) == []


def test_vad_speech_start_and_stop():
    vad = EnergyVAD()
    events = feed_all(vad, frames(0.5, 1.0, 0.5))
    kinds = [e.kind for e in events]
    assert kinds[0] == "speech_start"
    assert "speech_stop" in kinds
    start = events[0]
    stop = next(e for e in events if e.kind == "speech_stop")
    assert start.t == pytest.approx(0.5, abs=0.05)   # first voiced frame
    assert stop.t == pytest.approx(1.5, abs=0.05)    # last voiced frame end


def test_vad_hangover_bridges_short_pauses():
    vad = EnergyVAD()
    # 0.3 s speech, 0.15 s pause (< hangover 0.3 s), 0.3 s speech, long silence
    events = feed_all(vad, frames(0.3, 0.3, 0.15, 0.3, 0.6))
    starts = [e for e in events if e.kind == "speech_start"]
    stops = [e for e in events if e.kind == "speech_stop"]
    assert len(starts) == 1 and len(stops) == 1
    # Speech spans [0.3, 1.05) with the 0.15 s pause bridged inside.
    assert stops[0].t == pytest.approx(1.05, abs=0.06)


def test_vad_fixed_threshold_fires_on_hum_but_adaptive_does_not():
    # Background hum louder than the fixed floor (0.01): RMS ~0.035.
    hum = [speech_frame(amp=0.05) for _ in range(int(1.0 / FRAME_S))]

    fixed = EnergyVAD(adaptive=False)
    assert any(e.kind == "speech_start" for e in feed_all(fixed, hum))

    adaptive = EnergyVAD(adaptive=True)
    assert feed_all(adaptive, hum) == []  # noise floor learned, no false start

    # ...but a loud voice on top of the hum still triggers.
    loud = [speech_frame(amp=0.5) for _ in range(int(0.3 / FRAME_S))]
    kinds = [e.kind for e in feed_all(adaptive, loud)]
    assert kinds[0] == "speech_start"


# ----------------------------------------------------------------------
# TurnDetector (pure state machine, time injected)
# ----------------------------------------------------------------------
def _start(t: float) -> VadEvent:
    return VadEvent(kind="speech_start", t=t)


def _speech(t: float) -> VadEvent:
    return VadEvent(kind="speech", t=t)


def _stop(t: float) -> VadEvent:
    return VadEvent(kind="speech_stop", t=t)


def test_turn_short_pause_keeps_listening():
    td = TurnDetector(min_pause_s=0.8)
    assert td.update(_start(0.0), 0.0) == "listening"
    assert td.update(_speech(1.0), 1.0) == "listening"
    assert td.update(_stop(1.0), 1.0) == "listening"
    assert td.update(None, 1.5) == "listening"  # 0.5 s pause < min


def test_turn_pause_over_min_ends_turn():
    td = TurnDetector(min_pause_s=0.8)
    td.update(_start(0.0), 0.0)
    td.update(_speech(1.0), 1.0)
    td.update(_stop(1.0), 1.0)
    assert td.update(None, 2.0) == "turn_end"  # 1.0 s pause >= min
    assert td.update(None, 3.0) == "listening"  # reset afterwards


def test_turn_timeout_forces_end():
    td = TurnDetector(min_pause_s=0.8, max_turn_s=2.0)
    td.update(_start(0.0), 0.0)
    for now in np.arange(0.03, 2.0, 0.3):
        td.update(_speech(float(now)), float(now))
    assert td.update(_speech(2.1), 2.1) == "turn_timeout"


def test_turn_resume_within_pause_continues_same_turn():
    td = TurnDetector(min_pause_s=0.8)
    td.update(_start(0.0), 0.0)
    td.update(_speech(2.0), 2.0)
    td.update(_stop(2.0), 2.0)
    assert td.update(None, 2.5) == "listening"  # mid-pause
    # Speaker resumes 0.6 s in (before min_pause): same turn must continue.
    assert td.update(_speech(2.6), 2.6) == "listening"
    td.update(_stop(3.0), 3.0)
    assert td.update(None, 4.0) == "turn_end"  # one continuous turn


def test_turn_max_pause_caps_wait():
    td = TurnDetector(min_pause_s=2.0, max_pause_s=1.0)
    td.update(_start(0.0), 0.0)
    td.update(_stop(1.0), 1.0)
    assert td.update(None, 2.2) == "turn_end"  # capped by max_pause_s


def test_turn_new_start_opens_new_turn():
    td = TurnDetector(min_pause_s=0.8)
    td.update(_start(0.0), 0.0)
    td.update(_stop(1.0), 1.0)
    assert td.update(None, 2.0) == "turn_end"
    assert td.update(_start(3.0), 3.0) == "listening"
    td.update(_stop(3.5), 3.5)
    assert td.update(None, 4.5) == "turn_end"


# ----------------------------------------------------------------------
# BargeInController
# ----------------------------------------------------------------------
def test_bargein_single_burst_does_not_interrupt():
    bi = BargeInController(confirm_frames=2)
    assert bi.observe(True, 0.03) is False   # one cough frame...
    assert bi.observe(False, 0.06) is False  # ...then silence
    assert bi.interrupted_at is None


def test_bargein_consecutive_speech_interrupts():
    bi = BargeInController(confirm_frames=2)
    assert bi.observe(True, 0.03) is False
    hit = bi.observe(True, 0.06)
    assert hit is True
    assert bi.interrupted_at == pytest.approx(0.06, abs=1e-6)


def test_bargein_event_api_requires_two_consecutive_events():
    bi = BargeInController(confirm_frames=2)
    assert bi.should_interrupt(_start(0.03)) is False
    assert bi.should_interrupt(_speech(0.06)) is True
    assert bi.interrupted_at == pytest.approx(0.06, abs=1e-6)


def test_bargein_reset_rearms():
    bi = BargeInController(confirm_frames=2)
    bi.observe(True, 0.03)
    bi.observe(True, 0.06)
    bi.reset()
    assert bi.interrupted_at is None
    assert bi.observe(True, 0.09) is False  # streak cleared


# ----------------------------------------------------------------------
# RealtimeSession (end-to-end synthetic streams)
# ----------------------------------------------------------------------
def test_session_two_utterances_with_boundaries():
    session = RealtimeSession()  # min_pause 0.8 s default
    events: list[RealtimeEvent] = []
    for f in frames(1.0, 3.0, 1.0, 2.0, 2.0):
        events.extend(session.feed_frame(f))

    ends = [e for e in events if e.kind == "utterance_end"]
    assert len(ends) == 2
    e1, e2 = ends
    assert e1.start == pytest.approx(1.0, abs=0.1)
    assert e1.end == pytest.approx(4.0, abs=0.1)
    assert e1.audio is not None
    assert e1.audio.size / SR == pytest.approx(3.0, abs=0.15)
    assert e2.start == pytest.approx(5.0, abs=0.1)
    assert e2.end == pytest.approx(7.0, abs=0.1)
    assert e2.audio.size / SR == pytest.approx(2.0, abs=0.15)


def test_session_short_pause_merges_into_single_turn():
    session = RealtimeSession()
    events: list[RealtimeEvent] = []
    # speech 2 s, pause 0.5 s (< 0.8), speech 1 s, then long silence
    for f in frames(0.2, 2.0, 0.5, 1.0, 2.0):
        events.extend(session.feed_frame(f))

    ends = [e for e in events if e.kind == "utterance_end"]
    assert len(ends) == 1
    u = ends[0]
    assert u.start == pytest.approx(0.2, abs=0.1)
    assert u.end == pytest.approx(3.7, abs=0.15)   # 0.2 + 2 + 0.5 + 1
    assert u.audio.size / SR == pytest.approx(3.5, abs=0.2)


def test_session_timeout_flushes_truncated_turn():
    session = RealtimeSession(turn=TurnDetector(min_pause_s=0.8, max_turn_s=1.0))
    events: list[RealtimeEvent] = []
    for f in frames(0.2, 3.0, 1.0):
        events.extend(session.feed_frame(f))

    # 3 s of continuous speech with a 1 s turn cap -> periodic forced cuts.
    truncated = [e for e in events if e.kind == "turn_complete"]
    assert len(truncated) == 3
    for ev in truncated:
        assert ev.audio is not None
        assert ev.audio.size / SR >= 0.9  # each chunk ~1 s
    # Chunks tile the speech span without dropping the tail.
    assert truncated[-1].end == pytest.approx(3.21, abs=0.1)


def test_session_barge_in_during_playback():
    session = RealtimeSession()
    session.bot_speaking = True
    events: list[RealtimeEvent] = []
    for f in frames(0.3, 0.3, 0.3):  # silence then 0.3 s of speech
        events.extend(session.feed_frame(f))

    barges = [e for e in events if e.kind == "barge_in"]
    assert len(barges) == 1
    assert barges[0].audio is None
    assert session.bot_speaking is False  # auto-disarmed after firing


def test_session_no_barge_in_when_bot_silent():
    session = RealtimeSession()
    events: list[RealtimeEvent] = []
    for f in frames(0.3, 0.3, 0.3):
        events.extend(session.feed_frame(f))
    assert [e for e in events if e.kind == "barge_in"] == []


def test_session_max_buffer_forces_flush():
    session = RealtimeSession(max_buffer_s=1.0)
    events: list[RealtimeEvent] = []
    for f in frames(2.0, 2.0):  # 2 s of continuous speech
        events.extend(session.feed_frame(f))

    flushed = [e for e in events if e.kind == "turn_complete"]
    assert len(flushed) == 1
    assert flushed[0].audio.size / SR == pytest.approx(1.0, abs=0.1)


def test_session_silence_only_produces_nothing():
    session = RealtimeSession()
    events: list[RealtimeEvent] = []
    for f in frames(5.0):
        events.extend(session.feed_frame(f))
    assert events == []


# ----------------------------------------------------------------------
# InterruptiblePlayer (playback kernel monkeypatched - no audio hardware)
# ----------------------------------------------------------------------
def test_player_stop_when_idle_is_safe():
    from vocalis.voice.tts import InterruptiblePlayer

    player = InterruptiblePlayer()
    player.stop()  # must not raise
    assert player.playing is False


def test_player_play_then_stop(monkeypatch, tmp_path):
    import vocalis.voice.tts as tts_mod

    def slow_kernel(path, stop: threading.Event) -> None:
        deadline = time.monotonic() + 10.0
        while not stop.is_set() and time.monotonic() < deadline:
            time.sleep(0.02)

    monkeypatch.setattr(tts_mod, "_blocking_play", slow_kernel)
    player = tts_mod.InterruptiblePlayer()
    wav = tmp_path / "reply.mp3"
    wav.write_bytes(b"fake")
    player.play(wav)
    time.sleep(0.15)
    assert player.playing is True
    player.stop()
    assert player.wait(timeout=2.0) is True
    assert player.playing is False


def test_player_stop_before_play_completes_kernel(monkeypatch, tmp_path):
    import vocalis.voice.tts as tts_mod

    called = {"n": 0}

    def kernel(path, stop: threading.Event) -> None:
        called["n"] += 1
        for _ in range(50):
            if stop.is_set():
                return
            time.sleep(0.01)

    monkeypatch.setattr(tts_mod, "_blocking_play", kernel)
    player = tts_mod.InterruptiblePlayer()
    player.play(tmp_path / "a.mp3")
    player.stop()
    player.wait(timeout=3.0)
    assert called["n"] == 1  # kernel ran and was cut short without crashing
