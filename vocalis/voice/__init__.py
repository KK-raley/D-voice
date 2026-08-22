"""Voice pipeline: speaker verification, ASR, and personalized TTS."""

from vocalis.voice.wakeword import WakeHit, WakeWordDetector, match_phrase

__all__ = ["WakeHit", "WakeWordDetector", "match_phrase"]
