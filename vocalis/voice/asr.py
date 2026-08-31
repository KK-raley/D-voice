"""Offline ASR via faster-whisper."""

from __future__ import annotations

import io
import os
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


def decode_audio(data: bytes, max_duration_s: float = 30.0) -> tuple[np.ndarray, int]:
    """Decode an encoded audio blob (wav/mp3/webm/ogg...) to mono float32 PCM.

    The browser MediaRecorder API typically produces ``audio/webm;codecs=opus``;
    PyAV (a faster-whisper dependency) decodes it directly, so no client-side
    conversion is needed. Returns (samples, sample_rate).
    """
    try:
        import av
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("PyAV is required to decode uploaded audio: pip install av") from e
    chunks: list[np.ndarray] = []
    rate = 16000
    samples = 0
    # Normalize packed/planar and integer/float formats through libav. Merely
    # casting int16 to float32 produces amplitudes up to 32768 and breaks VAD.
    resampler = av.AudioResampler(format="fltp", layout="mono", rate=rate)
    with av.open(io.BytesIO(data)) as container:
        for frame in container.decode(audio=0):
            for normalized in resampler.resample(frame):
                arr = normalized.to_ndarray().reshape(-1).astype(np.float32)
                samples += arr.size
                if samples > rate * max_duration_s:
                    raise ValueError(f"audio exceeds {max_duration_s:g} seconds")
                chunks.append(arr)
        for normalized in resampler.resample(None):
            arr = normalized.to_ndarray().reshape(-1).astype(np.float32)
            samples += arr.size
            if samples > rate * max_duration_s:
                raise ValueError(f"audio exceeds {max_duration_s:g} seconds")
            chunks.append(arr)
    if not chunks:
        raise ValueError("no audio frames found in upload")
    return np.concatenate(chunks), rate


@lru_cache(maxsize=4)
def _load_model(model_size: str, device: str, compute_type: str):
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "faster-whisper is required for ASR: "
            "pip install 'vocalis-voice-agent[voice]'"
        ) from e
    # 默认线程数偏保守；实测 4 核 8 线程机器上 8 线程快约 35%
    return WhisperModel(
        model_size,
        device=device,
        compute_type=compute_type,
        cpu_threads=min(os.cpu_count() or 4, 8),
    )


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
            beam_size=1,  # 语音命令场景延迟优先：beam 5 在 CPU 上慢 3-4 倍
            condition_on_previous_text=False,  # 短句独立转写，避免幻觉延续
            initial_prompt="以下是普通话的句子。",  # 偏置简体中文输出
            vad_filter=True,
            word_timestamps=True,  # bind wake words to their own speaker-verified audio
        )
        segments: list[dict] = []
        for seg in segments_iter:
            segments.append(
                {
                    "start": seg.start, "end": seg.end, "text": seg.text.strip(),
                    "words": [
                        {"start": word.start, "end": word.end, "word": word.word}
                        for word in (getattr(seg, "words", None) or [])
                    ],
                }
            )
        text = "".join(s["text"] for s in segments).strip()
        return Transcription(text=text, language=info.language, segments=segments)
