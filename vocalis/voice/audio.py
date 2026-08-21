"""Microphone capture utilities (sounddevice) with graceful no-device fallback."""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

TARGET_RATE = 16000


def _require_sounddevice():
    try:
        import sounddevice as sd
    except ImportError as e:
        raise RuntimeError(
            "sounddevice is required for microphone capture: "
            "pip install 'vocalis-voice-agent[voice]'"
        ) from e
    return sd


def resample(audio: np.ndarray, src_rate: int, dst_rate: int = TARGET_RATE) -> np.ndarray:
    """Linear-interpolation resampling (adequate for embeddings and whisper)."""
    if src_rate == dst_rate:
        return audio
    duration = audio.size / src_rate
    target_len = int(duration * dst_rate)
    x_old = np.linspace(0.0, duration, num=audio.size, endpoint=False)
    x_new = np.linspace(0.0, duration, num=target_len, endpoint=False)
    return np.interp(x_new, x_old, audio).astype(np.float32)


def record(
    seconds: float = 4.0,
    sample_rate: int = 16000,
    channels: int = 1,
) -> np.ndarray:
    """Record mono float32 PCM in [-1, 1] from the default input device."""
    sd = _require_sounddevice()
    audio = sd.rec(
        int(seconds * sample_rate),
        samplerate=sample_rate,
        channels=channels,
        dtype="float32",
    )
    sd.wait()
    audio = np.asarray(audio)
    if audio.ndim > 1:
        return audio.mean(axis=1)  # downmix to mono
    return audio


def record_until_silence(
    sample_rate: int = 16000,
    silence_s: float = 1.2,
    max_s: float = 20.0,
    energy_floor: float = 0.01,
) -> np.ndarray:
    """Record until `silence_s` of silence or `max_s` elapsed (VAD-lite)."""
    sd = _require_sounddevice()

    frame_ms = 30
    frame_len = int(sample_rate * frame_ms / 1000)
    silent_frames_needed = int(silence_s * 1000 / frame_ms)
    collected: list[np.ndarray] = []
    silent_run = 0
    total = 0

    with sd.InputStream(samplerate=sample_rate, channels=1, dtype="float32") as stream:
        while total < max_s * sample_rate:
            frame, _ = stream.read(frame_len)
            frame = frame.squeeze(axis=1)
            collected.append(frame)
            total += frame_len
            rms = float(np.sqrt(np.mean(np.square(frame))))
            silent_run = silent_run + 1 if rms < energy_floor else 0
            if silent_run >= silent_frames_needed and total > sample_rate:
                break

    audio = np.concatenate(collected)
    # Trim trailing silence.
    rms_windows = np.sqrt(np.convolve(audio**2, np.ones(1600) / 1600, mode="same"))
    voiced = np.nonzero(rms_windows > energy_floor)[0]
    if voiced.size:
        audio = audio[: voiced[-1] + 1600]
    return audio


def save_wav(path: str | Path, audio: np.ndarray, sample_rate: int = 16000) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm16 = np.clip(audio, -1.0, 1.0)
    pcm16 = (pcm16 * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm16.tobytes())
    return path


def load_wav(path: str | Path) -> tuple[np.ndarray, int]:
    """Load a PCM-16 WAV; returns (mono float32 waveform, sample_rate)."""
    with wave.open(str(path), "rb") as wf:
        sample_rate = wf.getframerate()
        n_channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        if sampwidth != 2:
            raise ValueError(
                f"{path}: only 16-bit PCM WAV is supported (got {sampwidth * 8}-bit); "
                "convert with e.g. ffmpeg -i in.wav -ac 1 -ar 16000 out.wav"
            )
        raw = wf.readframes(wf.getnframes())
    data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32767.0
    if n_channels > 1:
        data = data.reshape(-1, n_channels).mean(axis=1)
    return data, sample_rate
