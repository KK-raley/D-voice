"""Wake-word detector tests.

Fully offline: no microphone, no network, no openwakeword/torch required.
openwakeword availability is forced off deterministically via sys.modules.
"""

from __future__ import annotations

import sys

import numpy as np

from vocalis.config import VocalisConfig, WakeWordConfig
from vocalis.voice.wakeword import WakeHit, WakeWordDetector, match_phrase

PHRASES = ["hey d-voice", "d voice", "hey d voice", "你好 d-voice"]


def _no_openwakeword(monkeypatch) -> None:
    """Make `import openwakeword` fail deterministically, installed or not."""
    monkeypatch.setitem(sys.modules, "openwakeword", None)


# ----------------------------------------------------------------------
# match_phrase
# ----------------------------------------------------------------------
def test_match_phrase_case_insensitive():
    assert match_phrase("Hey D-VOICE!", PHRASES)
    assert match_phrase("HEY D-VOICE", PHRASES)
    assert match_phrase("hey D vOiCe", PHRASES)


def test_match_phrase_ignores_punctuation_and_extra_spaces():
    assert match_phrase("hey, d-voice...", PHRASES)
    assert match_phrase("hey  d   voice", PHRASES)
    assert match_phrase("(hey d-voice)", PHRASES)
    assert match_phrase("你好，d-voice！", PHRASES)


def test_match_phrase_substring_in_longer_utterance():
    assert match_phrase("um, hey d-voice, run the tests", PHRASES)
    assert match_phrase("please say d voice again", PHRASES)
    assert match_phrase("嗯你好 d-voice 帮我看看状态", PHRASES)


def test_match_phrase_tolerates_missing_word_spacing():
    # ASR often drops the spacing around the Latin part of mixed text.
    assert match_phrase("你好d-voice", PHRASES)
    assert match_phrase("hey dvoice", PHRASES)


def test_match_phrase_fullwidth_input():
    # NFKC folding: full-width Latin typed with a CJK IME still matches.
    assert match_phrase("Ｈｅｙ Ｄ-ＶＯＩＣＥ", PHRASES)


def test_match_phrase_alternate_forms():
    assert match_phrase("hey d voice", PHRASES)
    assert match_phrase("d voice", PHRASES)
    assert match_phrase("你好 d-voice", PHRASES)


def test_match_phrase_negative():
    assert not match_phrase("hello world", PHRASES)
    assert not match_phrase("what's the status", PHRASES)
    assert not match_phrase("voice assistant", PHRASES)
    assert not match_phrase("hey device", PHRASES)
    assert not match_phrase("hey devise", PHRASES)


def test_match_phrase_degenerate_inputs():
    assert not match_phrase("", PHRASES)
    assert not match_phrase("!!!", PHRASES)
    assert not match_phrase("hey d-voice", [])
    assert not match_phrase("hey d-voice", ["", "  "])


def test_match_phrase_custom_list():
    assert match_phrase("computer, do the thing", ["computer"])
    assert not match_phrase("hey jarvis", ["computer"])
    # Pure-Chinese phrase: direct substring after normalization.
    assert match_phrase("请说你好声纹", ["你好声纹"])
    assert match_phrase("你好，声纹！", ["你好声纹"])
    assert not match_phrase("你好", ["你好声纹"])


# ----------------------------------------------------------------------
# process_text (asr backend)
# ----------------------------------------------------------------------
def test_process_text_hit():
    det = WakeWordDetector(WakeWordConfig())
    hit = det.process_text("Hey D-VOICE!", now=100.0)
    assert hit.detected
    assert hit.phrase == "hey d-voice"
    assert hit.backend == "asr"
    assert hit.score == 1.0


def test_process_text_miss():
    det = WakeWordDetector()
    hit = det.process_text("tell me a joke", now=10.0)
    assert isinstance(hit, WakeHit)
    assert not hit.detected
    assert hit.phrase is None
    assert hit.backend == "asr"
    assert hit.score == 0.0


def test_process_text_empty_text_is_miss():
    det = WakeWordDetector()
    assert not det.process_text("", now=1.0).detected


def test_process_text_cooldown_swallows_rapid_repeats():
    det = WakeWordDetector(WakeWordConfig(cooldown_s=2.0))
    first = det.process_text("hey d-voice", now=100.0)
    assert first.detected

    second = det.process_text("hey d-voice", now=100.5)  # within 2s: ignored
    assert not second.detected
    assert second.phrase is None

    third = det.process_text("hey d-voice", now=103.0)  # past cooldown
    assert third.detected
    assert third.phrase == "hey d-voice"


def test_process_text_cooldown_is_global_across_phrases():
    det = WakeWordDetector(WakeWordConfig(cooldown_s=2.0))
    assert det.process_text("hey d-voice", now=0.0).detected
    # Different phrase, still within the cooldown window -> swallowed.
    assert not det.process_text("你好 d-voice", now=0.5).detected


# ----------------------------------------------------------------------
# Backend probing / resolution
# ----------------------------------------------------------------------
def test_default_detector_is_asr(monkeypatch):
    _no_openwakeword(monkeypatch)
    det = WakeWordDetector()
    assert det.config.backend == "asr"
    assert det.available() is True
    assert det.resolve_backend() == "asr"


def test_resolve_backend_falls_back_to_asr_without_openwakeword(monkeypatch):
    _no_openwakeword(monkeypatch)
    det = WakeWordDetector(WakeWordConfig(backend="openwakeword"))
    assert det.resolve_backend() == "asr"


def test_available_false_when_openwakeword_missing(monkeypatch):
    _no_openwakeword(monkeypatch)
    det = WakeWordDetector(WakeWordConfig(backend="openwakeword"))
    assert det.available() is False


def test_resolve_backend_none_for_unknown_backend():
    det = WakeWordDetector(WakeWordConfig(backend="telepathy"))
    assert det.resolve_backend() == "none"
    assert det.available() is False


def test_process_audio_without_openwakeword_returns_none_backend(monkeypatch):
    _no_openwakeword(monkeypatch)
    det = WakeWordDetector(WakeWordConfig(backend="openwakeword"))
    silence = np.zeros(16000, dtype=np.float32)
    hit = det.process_audio(silence, sample_rate=16000)
    assert not hit.detected
    assert hit.phrase is None
    assert hit.backend == "none"
    assert hit.score == 0.0


# ----------------------------------------------------------------------
# Config integration
# ----------------------------------------------------------------------
def test_config_wakes_up_as_asr_backend():
    cfg = VocalisConfig()
    assert cfg.wake_word.backend == "asr"
    assert cfg.wake_word.enabled is True
    assert cfg.wake_word.cooldown_s == 2.0
    assert "hey d-voice" in cfg.wake_word.phrases
    assert "你好 d-voice" in cfg.wake_word.phrases
