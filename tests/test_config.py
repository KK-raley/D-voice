"""Config persistence round-trip tests."""

from __future__ import annotations

from pathlib import Path

from vocalis.config import VocalisConfig


def test_defaults(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("VOCALIS_HOME", str(tmp_path))
    cfg = VocalisConfig()
    assert cfg.voice_gate.threshold == 0.80
    assert cfg.tts.default_profile == "aria"
    assert cfg.brain.model.startswith("qwen")


def test_roundtrip(tmp_path: Path, monkeypatch):
    import os

    monkeypatch.setenv("VOCALIS_HOME", str(tmp_path / "home"))
    cfg = VocalisConfig()
    cfg.voice_gate.threshold = 0.9
    cfg.brain.model = "llama3.2:3b"
    saved = cfg.save()
    assert saved.exists()

    loaded = VocalisConfig.load()
    assert loaded.voice_gate.threshold == 0.9
    assert loaded.brain.model == "llama3.2:3b"


def test_from_dict_ignores_unknown(tmp_path: Path):
    cfg = VocalisConfig.from_dict({"nonexistent": {"x": 1}, "log_level": "DEBUG"})
    assert cfg.log_level == "DEBUG"
