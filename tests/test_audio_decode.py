"""Real decoder acceptance tests: bounded, mono and normalized microphone PCM."""

from __future__ import annotations

import io
import wave

import numpy as np
import pytest

from vocalis.voice.asr import decode_audio


def _wav(samples: np.ndarray, rate: int, channels: int = 1) -> bytes:
    target = io.BytesIO()
    with wave.open(target, "wb") as recording:
        recording.setnchannels(channels)
        recording.setsampwidth(2)
        recording.setframerate(rate)
        recording.writeframes(samples.astype("<i2").tobytes())
    return target.getvalue()


@pytest.fixture(autouse=True)
def decoder_available():
    pytest.importorskip("av", reason="PyAV is an optional voice dependency")


@pytest.mark.parametrize("sample_rate", [16000, 44100, 48000])
def test_integer_wav_becomes_normalized_16khz_mono(sample_rate):
    time = np.arange(sample_rate // 2) / sample_rate
    source = np.round(np.sin(2 * np.pi * 440 * time) * 16384).astype(np.int16)
    audio, rate = decode_audio(_wav(source, sample_rate))
    assert rate == 16000
    assert audio.dtype == np.float32
    assert audio.ndim == 1
    assert abs(len(audio) - 8000) <= 2
    assert 0.45 < np.max(np.abs(audio)) < 0.55
    assert np.isfinite(audio).all()


def test_stereo_wav_is_downmixed_and_preserves_duration():
    source = np.tile(np.array([8192, 8192], dtype=np.int16), (16000, 1))
    audio, rate = decode_audio(_wav(source, 16000, channels=2))
    assert audio.ndim == 1
    assert len(audio) == rate == 16000
    assert 0.2 < float(np.mean(audio)) < 0.4


def test_decoded_duration_is_limited_even_for_small_encoded_payload():
    source = np.zeros(16000, dtype=np.int16)
    with pytest.raises(ValueError, match="exceeds"):
        decode_audio(_wav(source, 16000), max_duration_s=0.5)


def test_empty_audio_frames_are_rejected():
    with pytest.raises(ValueError, match="no audio frames"):
        decode_audio(_wav(np.array([], dtype=np.int16), 16000))


def test_corrupt_audio_is_rejected():
    import av

    with pytest.raises(av.error.InvalidDataError):
        decode_audio(b"This is not an audio container")
