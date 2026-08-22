"""Track B voice-output feature tests: voice picker, presets, per-agent voices.

Fully offline: edge_tts is faked (or blocked) through ``sys.modules`` so no
network call ever happens, even when the real package is installed.
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest

from vocalis.config import VocalisConfig
from vocalis.voice.tts import (
    FALLBACK_VOICES,
    TTSService,
    filter_voices,
    list_voices,
)


# ---------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------
def _install_fake_edge_tts(monkeypatch, voices=None, error=None) -> None:
    """Insert a fake edge_tts module into sys.modules.

    Works whether or not the real package is installed, because ``import
    edge_tts`` resolves through sys.modules first.
    """
    module = types.ModuleType("edge_tts")

    async def list_voices():
        if error is not None:
            raise error
        return voices

    module.list_voices = list_voices
    monkeypatch.setitem(sys.modules, "edge_tts", module)


def _block_edge_tts(monkeypatch) -> None:
    """A None entry makes ``import edge_tts`` raise ImportError."""
    monkeypatch.setitem(sys.modules, "edge_tts", None)


def _service(tmp_path: Path, monkeypatch) -> TTSService:
    monkeypatch.setenv("VOCALIS_HOME", str(tmp_path))
    return TTSService(VocalisConfig())


# ---------------------------------------------------------------------
# B1: list_voices / filter_voices
# ---------------------------------------------------------------------
def test_list_voices_normalizes_and_sorts(monkeypatch):
    raw = [
        {
            "ShortName": "zh-TW-HsiaoChenNeural",
            "Gender": "Female",
            "Locale": "zh-TW",
            "FriendlyName": "HsiaoChen",
        },
        {"ShortName": "en-US-GuyNeural", "Gender": "Male", "Locale": "en-US"},
        {"ShortName": "zh-CN-XiaoxiaoNeural", "Gender": "Female", "Locale": "zh-CN"},
    ]
    _install_fake_edge_tts(monkeypatch, voices=raw)

    voices = asyncio.run(list_voices())

    assert [v["ShortName"] for v in voices] == [
        "en-US-GuyNeural",
        "zh-CN-XiaoxiaoNeural",
        "zh-TW-HsiaoChenNeural",
    ]
    # only the three documented keys survive normalization
    assert all(set(v) == {"ShortName", "Gender", "Locale"} for v in voices)


def test_list_voices_fallback_when_import_fails(monkeypatch):
    _block_edge_tts(monkeypatch)

    voices = asyncio.run(list_voices())

    assert [v["ShortName"] for v in voices] == [
        v["ShortName"] for v in FALLBACK_VOICES
    ]
    assert "zh-CN-XiaoxiaoNeural" in [v["ShortName"] for v in voices]
    assert "en-US-AriaNeural" in [v["ShortName"] for v in voices]
    # fallback is served sorted by Locale too
    assert voices == sorted(voices, key=lambda v: v["Locale"])


def test_list_voices_fallback_when_backend_errors(monkeypatch):
    _install_fake_edge_tts(monkeypatch, error=RuntimeError("network down"))

    voices = asyncio.run(list_voices())

    assert [v["ShortName"] for v in voices] == [
        v["ShortName"] for v in FALLBACK_VOICES
    ]


def test_filter_voices_locale_prefix_match():
    voices = [
        {"ShortName": "a", "Gender": "Female", "Locale": "zh-CN"},
        {"ShortName": "b", "Gender": "Male", "Locale": "zh-TW"},
        {"ShortName": "c", "Gender": "Female", "Locale": "en-US"},
    ]
    got = filter_voices(voices, locale="zh")
    assert [v["ShortName"] for v in got] == ["a", "b"]


def test_filter_voices_locale_full_prefix():
    voices = [
        {"ShortName": "a", "Gender": "Female", "Locale": "zh-CN"},
        {"ShortName": "b", "Gender": "Male", "Locale": "zh-TW"},
    ]
    got = filter_voices(voices, locale="zh-CN")
    assert [v["ShortName"] for v in got] == ["a"]


def test_filter_voices_gender():
    voices = [
        {"ShortName": "a", "Gender": "Female", "Locale": "zh-CN"},
        {"ShortName": "b", "Gender": "Male", "Locale": "zh-TW"},
    ]
    got = filter_voices(voices, gender="Female")
    assert [v["ShortName"] for v in got] == ["a"]


def test_filter_voices_locale_and_gender_combined():
    voices = [
        {"ShortName": "a", "Gender": "Female", "Locale": "zh-CN"},
        {"ShortName": "b", "Gender": "Male", "Locale": "zh-TW"},
        {"ShortName": "c", "Gender": "Female", "Locale": "en-US"},
    ]
    got = filter_voices(voices, locale="zh", gender="Female")
    assert [v["ShortName"] for v in got] == ["a"]


def test_filter_voices_no_filters_returns_all():
    voices = [
        {"ShortName": "a", "Gender": "Female", "Locale": "zh-CN"},
        {"ShortName": "b", "Gender": "Male", "Locale": "en-US"},
    ]
    assert filter_voices(voices) == voices
    assert filter_voices(voices, locale=None, gender=None) == voices


def test_filter_voices_no_match_returns_empty():
    voices = [{"ShortName": "a", "Gender": "Female", "Locale": "zh-CN"}]
    assert filter_voices(voices, locale="ja") == []
    assert filter_voices(voices, gender="Male") == []


# ---------------------------------------------------------------------
# B2: apply_preset
# ---------------------------------------------------------------------
def test_apply_preset_switches_default_and_content(tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch)

    profile = svc.apply_preset("focus")

    assert profile.name == "focus"
    assert svc.config.tts.default_profile == "focus"
    stored = svc.get_profile("focus")
    assert stored.voice == "zh-CN-YunxiNeural"
    assert stored.rate == "+15%"
    assert stored.pitch == "+0Hz"
    assert stored.volume == "+0%"


def test_apply_preset_persists_to_disk(tmp_path, monkeypatch):
    monkeypatch.setenv("VOCALIS_HOME", str(tmp_path))
    svc = TTSService(VocalisConfig())

    svc.apply_preset("evening")

    reloaded = VocalisConfig.load()
    assert reloaded.tts.default_profile == "evening"
    assert reloaded.tts.profiles["evening"]["voice"] == "zh-CN-XiaoxiaoNeural"
    assert reloaded.tts.profiles["evening"]["rate"] == "-10%"
    assert reloaded.tts.profiles["evening"]["pitch"] == "-2Hz"
    assert reloaded.tts.profiles["evening"]["volume"] == "-5%"


def test_apply_preset_unknown_raises_with_available_names(tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch)

    with pytest.raises(KeyError) as exc:
        svc.apply_preset("nope")

    msg = str(exc.value)
    assert "nope" in msg
    for name in ("focus", "evening", "presentation"):
        assert name in msg


# ---------------------------------------------------------------------
# B4: profile_for_agent
# ---------------------------------------------------------------------
def test_profile_for_agent_mapped(tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch)
    assert svc.profile_for_agent("claude-code") == "orion"
    assert svc.profile_for_agent("echo") == "aria"


def test_profile_for_agent_falls_back_to_default(tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch)
    svc.apply_preset("presentation")

    assert svc.profile_for_agent("someone-else") == "presentation"
    assert svc.profile_for_agent("someone-else") == svc.config.tts.default_profile
