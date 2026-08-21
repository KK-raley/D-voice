"""Offline ASR via faster-whisper."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import numpy as np

from vocalis.config import ASRConfig
from vocalis.voice.audio import resample


@dataclass
class Transcription:
    text: str
    language: str
    segments: list[dict]


@lru_cache(maxsize=4)
def _load_model(model_size: str, device: str, compute_type: str):
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "faster-whisper is required for ASR: "
            "pip install 'vocalis-voice-agent[voice]'"
        ) from e
    return WhisperModel(model_size, device=device, compute_type=compute_type)


class Transcriber:
    def __init__(self, config: ASRConfig | None = None) -> None:
        self.config = config or ASRConfig()

    def transcribe(self, audio: np.ndarray, sample_rate: int = 16000) -> Transcription:
        # faster-whisper requires 16 kHz float32 input.
        audio16 = resample(np.asarray(audio, dtype=np.float32), sample_rate, 16000)
        model = _load_model(
            self.config.model_size, self.config.device, self.config.compute_type
        )
        segments_iter, info = model.transcribe(
            audio16,
            language=self.config.language,
            beam_size=5,
            vad_filter=True,
        )
        segments: list[dict] = []
        for seg in segments_iter:
            segments.append(
                {"start": seg.start, "end": seg.end, "text": seg.text.strip()}
            )
        text = "".join(s["text"] for s in segments).strip()
        return Transcription(text=text, language=info.language, segments=segments)
