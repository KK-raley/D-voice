"""Speaker embedding built on Resemblyzer d-vectors.

The encoder is loaded lazily so that importing :mod:`vocalis.voice` stays
cheap even when heavy ML dependencies are not installed (e.g. in CI).
"""

from __future__ import annotations

from functools import lru_cache

import numpy as np


class SpeakerEncoderError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def get_encoder():
    """Load and cache the Resemblyzer ``VoiceEncoder`` (downloads weights once)."""
    try:
        from resemblyzer import VoiceEncoder
    except ImportError as e:  # pragma: no cover
        raise SpeakerEncoderError(
            "resemblyzer is not installed. Install voice extras with: "
            "pip install 'vocalis-voice-agent[voice]'"
        ) from e
    return VoiceEncoder()


def embed_utterance(audio: np.ndarray, sample_rate: int = 16000) -> np.ndarray:
    """Return an L2-normalized 256-dim speaker embedding for one utterance.

    Accepts float32/float64 PCM in [-1, 1]. Non-16 kHz input is resampled
    with linear interpolation (adequate for d-vector extraction).
    """
    audio = np.asarray(audio, dtype=np.float32).squeeze()
    if audio.ndim != 1:
        raise ValueError(f"expected mono waveform, got shape {audio.shape}")
    if audio.size < sample_rate // 4:
        raise ValueError("utterance too short (<0.25s) for reliable embedding")
    if sample_rate != 16000:
        duration = audio.size / sample_rate
        target_len = int(duration * 16000)
        x_old = np.linspace(0.0, duration, num=audio.size, endpoint=False)
        x_new = np.linspace(0.0, duration, num=target_len, endpoint=False)
        audio = np.interp(x_new, x_old, audio).astype(np.float32)

    encoder = get_encoder()
    embedding = encoder.embed_utterance(audio)
    return np.asarray(embedding, dtype=np.float32)


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))
