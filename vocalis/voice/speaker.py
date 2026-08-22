"""Speaker embedding backends.

Two interchangeable backends power VoiceGate:

* ``resemblyzer``   - Resemblyzer d-vectors (256-dim), light & zero-config.
* ``eres2net-large`` - ERes2Net-large from 3D-Speaker/ModelScope (192-dim,
  LDA-normalized), the current open-source SOTA for speaker verification.
  Needs the ``voiceprint`` extra (``pip install vocalis-voice-agent[voiceprint]``).

Backends produce embeddings with *different geometry* (dimension and score
distributions), so enrolled voiceprints are namespaced per backend and
thresholds default per backend (see :data:`BACKEND_DEFAULTS`).

The encoders are loaded lazily so importing :mod:`vocalis.voice` stays cheap
even when heavy ML dependencies are not installed (e.g. in CI).
"""

from __future__ import annotations

import logging
import os
import tempfile
from functools import lru_cache

import numpy as np

from vocalis.voice.audio import resample

logger = logging.getLogger("vocalis.voice.speaker")

ERES2NET_MODEL_ID = "iic/speech_eres2net_sv_zh-cn_16k-common"

#: Per-backend defaults. ERes2Net-large embeddings are LDA-normalized, so
#: genuine/impostor cosine scores live on a different scale than Resemblyzer.
#: Tune `voice_gate.threshold` in config.toml after a real enrollment session.
BACKEND_DEFAULTS: dict[str, dict[str, float]] = {
    "resemblyzer": {"threshold": 0.80, "enroll_consistency": 0.75},
    "eres2net-large": {"threshold": 0.55, "enroll_consistency": 0.70},
}


class SpeakerEncoderError(RuntimeError):
    pass


# ----------------------------------------------------------------------
# Backend: Resemblyzer (d-vector)
# ----------------------------------------------------------------------
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


def _embed_resemblyzer(audio: np.ndarray, sample_rate: int) -> np.ndarray:
    audio = resample(audio, sample_rate, 16000)
    encoder = get_encoder()
    return np.asarray(encoder.embed_utterance(audio), dtype=np.float32)


# ----------------------------------------------------------------------
# Backend: ERes2Net-large (3D-Speaker via ModelScope)
# ----------------------------------------------------------------------
@lru_cache(maxsize=1)
def get_eres2net():
    """Load the ERes2Net-large speaker-verification pipeline (downloads once)."""
    try:
        from modelscope.pipelines import pipeline
        from modelscope.utils.constant import Tasks
    except ImportError as e:  # pragma: no cover
        raise SpeakerEncoderError(
            "modelscope is not installed. Install the SOTA voiceprint extra: "
            "pip install 'vocalis-voice-agent[voiceprint]' "
            "(or switch voice_gate.backend back to 'resemblyzer')"
        ) from e
    return pipeline(task=Tasks.speaker_verification, model=ERES2NET_MODEL_ID)


def _embed_eres2net(audio: np.ndarray, sample_rate: int) -> np.ndarray:
    """ERes2Net-large embedding via the ModelScope SV pipeline.

    The pipeline consumes wav files, so the utterance is round-tripped
    through a temp file (int16 PCM @16k) - a few ms of overhead.
    """
    from scipy.io import wavfile

    audio = resample(audio, sample_rate, 16000)
    pcm = np.clip(audio, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype(np.int16)

    fd, path = tempfile.mkstemp(suffix=".wav", prefix="vocalis_sv_")
    try:
        wavfile.write(path, 16000, pcm)
        out = get_eres2net()([path])
    finally:
        os.close(fd)
        try:
            os.unlink(path)
        except OSError:  # pragma: no cover
            pass

    embs = out.get("embs") if isinstance(out, dict) else None
    if embs is None:
        raise SpeakerEncoderError(f"unexpected ERes2Net pipeline output: {type(out)}")
    emb = np.asarray(embs[0], dtype=np.float32).squeeze()
    norm = float(np.linalg.norm(emb))
    if norm == 0.0:
        raise SpeakerEncoderError("ERes2Net returned a zero embedding")
    return emb / norm


_EMBEDDERS = {
    "resemblyzer": _embed_resemblyzer,
    "eres2net-large": _embed_eres2net,
}


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------
def resolve_backend(preferred: str | None) -> str:
    """Return an embeddable backend name.

    Falls back to the *other* backend (with a loud warning) when the
    preferred one's dependencies are missing; raises if neither works.
    """
    preferred = preferred or "eres2net-large"
    if preferred not in _EMBEDDERS:
        raise SpeakerEncoderError(
            f"unknown voice_gate.backend {preferred!r} - "
            f"expected one of {sorted(_EMBEDDERS)}"
        )
    order = [preferred] + [b for b in _EMBEDDERS if b != preferred]
    for name in order:
        probe = {"resemblyzer": get_encoder, "eres2net-large": get_eres2net}[name]
        try:
            probe()
            return name
        except SpeakerEncoderError as e:
            if name == preferred:
                logger.warning("preferred backend %r unavailable: %s", name, e)
    raise SpeakerEncoderError(
        "no speaker backend available - install one of: "
        "vocalis-voice-agent[voice] (resemblyzer) or "
        "vocalis-voice-agent[voiceprint] (ERes2Net-large)"
    )


def embed_utterance(
    audio: np.ndarray, sample_rate: int = 16000, backend: str = "eres2net-large"
) -> np.ndarray:
    """Return an L2-normalized speaker embedding for one utterance.

    Accepts float32/float64 PCM in [-1, 1]. Any input rate is resampled to
    16 kHz - passing the *actual* capture rate is the caller's responsibility.
    """
    audio = np.asarray(audio, dtype=np.float32).squeeze()
    if audio.ndim != 1:
        raise ValueError(f"expected mono waveform, got shape {audio.shape}")
    if audio.size < sample_rate // 4:
        raise ValueError("utterance too short (<0.25s) for reliable embedding")
    try:
        embedder = _EMBEDDERS[backend]
    except KeyError:
        raise SpeakerEncoderError(
            f"unknown backend {backend!r} - expected one of {sorted(_EMBEDDERS)}"
        ) from None
    return embedder(audio, sample_rate)


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))
