"""Wake-word ("hey D-VOICE") detection with two interchangeable backends.

Backend trade-off:

* ``openwakeword`` - dedicated streaming models (~10 MB RAM, sub-second
  latency, designed for always-on listening). Costs an extra dependency
  (``pip install 'vocalis-voice-agent[wakeword]'``) and only recognizes the
  phrases its small models were trained on.
* ``asr`` - keyword matching on regular faster-whisper transcription.
  Zero extra dependencies (reuses the ASR stack) and arbitrary phrases,
  including Chinese, at the price of much higher latency and CPU: a whole
  utterance must be transcribed before the match can happen.

The detector degrades gracefully: when the configured ``openwakeword``
backend cannot be imported it resolves to ``asr`` matching, and
``process_audio`` reports a plain miss (``backend="none"``) instead of
raising when the library disappears at call time.
"""

from __future__ import annotations

import logging
import time
import unicodedata
from dataclasses import dataclass
from typing import Any

import numpy as np

from vocalis.config import WakeWordConfig

logger = logging.getLogger("vocalis.voice.wakeword")

_TARGET_RATE = 16000  # both openwakeword and faster-whisper want 16 kHz


# ----------------------------------------------------------------------
# Text matching (asr backend)
# ----------------------------------------------------------------------
def _normalize(text: str) -> str:
    """Lowercase, strip punctuation and collapse whitespace.

    Unicode NFKC first so full-width Latin ("ＤＶＯＩＣＥ") matches ASCII.
    Punctuation becomes a space so "d-voice" and "d voice" normalize alike;
    CJK characters count as alphanumeric and pass through untouched.
    """
    text = unicodedata.normalize("NFKC", text).lower()
    cleaned = "".join(ch if ch.isalnum() else " " for ch in text)
    return " ".join(cleaned.split())


def _find_phrase(text: str, phrases: list[str]) -> str | None:
    """Return the first phrase occurring in *text*, or None (normalized)."""
    norm_text = _normalize(text)
    if not norm_text:
        return None
    despaced_text = norm_text.replace(" ", "")
    for phrase in phrases:
        norm_phrase = _normalize(phrase)
        if not norm_phrase:
            continue
        # Direct substring: Chinese phrases need nothing more. The fully
        # despaced comparison additionally tolerates ASR output such as
        # "你好d voice" or "hey dvoice" where word spacing is off.
        if norm_phrase in norm_text or (
            norm_phrase.replace(" ", "") in despaced_text
        ):
            return phrase
    return None


def match_phrase(text: str, phrases: list[str]) -> bool:
    """True if any phrase occurs in *text*.

    Case-insensitive, ignores punctuation and redundant whitespace, so
    "Hey D-VOICE!" matches "hey d-voice". Chinese phrases match as direct
    (normalized) substrings.
    """
    return _find_phrase(text, phrases) is not None


@dataclass
class WakeHit:
    """Result of one wake-word evaluation.

    ``detected`` is True iff ``phrase`` is not None. ``score`` is 1.0 for an
    exact text match or the openwakeword model confidence otherwise.
    """

    detected: bool
    phrase: str | None
    backend: str
    score: float


# ----------------------------------------------------------------------
# Detector
# ----------------------------------------------------------------------
class WakeWordDetector:
    """Always-on wake-word detector.

    Two paths share one cooldown (``config.cooldown_s``):

    * ``process_text`` - match ASR transcription output against phrases.
    * ``process_audio`` - stream 16 kHz chunks through openwakeword.
    """

    def __init__(self, config: WakeWordConfig | None = None) -> None:
        self.config = config or WakeWordConfig()
        self._last_hit: float | None = None  # time.monotonic() of last accepted hit
        self._oww_model: Any = None  # lazily built openwakeword.Model

    # -- backend probing -------------------------------------------------
    @staticmethod
    def _openwakeword_available() -> bool:
        try:
            import openwakeword  # noqa: F401  (heavy: onnxruntime/tflite)
        except ImportError:
            return False
        except Exception:  # broken install (e.g. missing tflite-runtime)
            logger.debug("openwakeword import failed", exc_info=True)
            return False
        return True

    def available(self) -> bool:
        """True if the *configured* backend's dependencies import cleanly."""
        backend = self.config.backend
        if backend == "openwakeword":
            return self._openwakeword_available()
        if backend == "asr":
            return True  # pure text matching, stdlib only
        logger.debug("unknown wake-word backend %r", backend)
        return False

    def resolve_backend(self) -> str:
        """Best usable backend: the configured one, else "asr", else "none"."""
        backend = self.config.backend
        if backend == "openwakeword":
            if self._openwakeword_available():
                return "openwakeword"
            logger.warning(
                "openwakeword configured but not importable; "
                "falling back to the asr text-matching backend "
                "(pip install 'vocalis-voice-agent[wakeword]')"
            )
            return "asr"
        if backend == "asr":
            return "asr"
        logger.warning("unknown wake-word backend %r; wake detection disabled", backend)
        return "none"

    # -- cooldown ---------------------------------------------------------
    def _accept(self, now: float) -> bool:
        """Cooldown gate; True if enough time passed since the last hit."""
        if self._last_hit is not None and (now - self._last_hit) < self.config.cooldown_s:
            return False
        self._last_hit = now
        return True

    # -- asr path -----------------------------------------------------------
    def process_text(self, text: str, now: float | None = None) -> WakeHit:
        """Match an ASR transcription (may be empty) against the wake phrases."""
        phrase = _find_phrase(text, self.config.phrases)
        if phrase is None:
            return WakeHit(detected=False, phrase=None, backend="asr", score=0.0)
        now = time.monotonic() if now is None else now
        if not self._accept(now):
            logger.debug("wake hit %r swallowed by cooldown", phrase)
            return WakeHit(detected=False, phrase=None, backend="asr", score=0.0)
        logger.info("wake word detected: %r (backend=asr)", phrase)
        return WakeHit(detected=True, phrase=phrase, backend="asr", score=1.0)

    # -- openwakeword path ----------------------------------------------------
    def process_audio(self, audio: np.ndarray, sample_rate: int) -> WakeHit:
        """Score an audio chunk with openwakeword (missing dep = plain miss)."""
        if not self._openwakeword_available():
            logger.warning(
                "process_audio called but openwakeword is not importable; "
                "returning miss (backend=none). "
                "Install with: pip install 'vocalis-voice-agent[wakeword]'"
            )
            return WakeHit(detected=False, phrase=None, backend="none", score=0.0)

        from vocalis.voice.audio import resample

        audio16 = resample(np.asarray(audio, dtype=np.float32), sample_rate, _TARGET_RATE)
        if self._oww_model is None:
            import openwakeword

            logger.info(
                "loading openwakeword model %r (may download on first run)",
                self.config.model,
            )
            self._oww_model = openwakeword.Model(wakeword_models=[self.config.model])

        score = self._score_for(self._oww_model.predict(audio16))
        if score < self.config.threshold:
            return WakeHit(detected=False, phrase=None, backend="openwakeword", score=score)
        if not self._accept(time.monotonic()):
            logger.debug("openwakeword hit swallowed by cooldown (score=%.3f)", score)
            return WakeHit(detected=False, phrase=None, backend="openwakeword", score=score)
        logger.info(
            "wake word detected: %r (backend=openwakeword, score=%.3f)",
            self.config.model,
            score,
        )
        return WakeHit(
            detected=True, phrase=self.config.model, backend="openwakeword", score=score
        )

    def _score_for(self, scores: Any) -> float:
        """Extract the configured model's score from a predict() result."""
        if isinstance(scores, dict):
            for name, value in scores.items():
                if self.config.model in str(name):
                    return float(value)
            if scores:
                return max(float(v) for v in scores.values())
            return 0.0
        try:
            return float(scores)
        except (TypeError, ValueError):
            logger.warning("unexpected openwakeword predict() result: %r", scores)
            return 0.0
