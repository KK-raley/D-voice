"""P0-4 doctor checks + P0-3 stress recorder (no hardware required)."""

from __future__ import annotations

import asyncio
import json

import pytest

import vocalis.doctor as doctor_mod
from vocalis.config import VocalisConfig
from vocalis.doctor import CheckResult, run_checks
from vocalis.monitor.stress import StressRecorder, summarize


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("VOCALIS_HOME", str(tmp_path / "home"))
    return VocalisConfig()


@pytest.fixture
def hermetic_gate(monkeypatch):
    """Avoid loading real speaker-encoder weights in tests (heavy / network)."""
    monkeypatch.setattr(
        doctor_mod,
        "check_voice_gate",
        lambda config: CheckResult("voiceprint", True, "stub backend · 2 profiles"),
    )


# ----------------------------------------------------------------------
# doctor
# ----------------------------------------------------------------------
async def test_doctor_never_raises_and_covers_p0_areas(config, hermetic_gate):
    results = await run_checks(config)
    names = {r.name for r in results}
    assert {"python", "config", "permissions", "microphone", "voiceprint",
            "asr-cache", "brain", "network-boundary"} <= names
    for r in results:
        assert r.ok in (True, False, None)
        assert isinstance(r.detail, str) and r.detail


async def test_doctor_brain_disabled_is_warning_not_failure(config):
    config.brain.enabled = False
    results = await run_checks(config)
    brain = next(r for r in results if r.name == "brain")
    assert brain.ok is None  # disabled -> warning row with fix hint


async def test_doctor_report_json_serializable(config, hermetic_gate):
    results = await run_checks(config)
    payload = json.dumps([r.to_dict() for r in results], ensure_ascii=False)
    assert "name" in payload


# ----------------------------------------------------------------------
# stress recorder
# ----------------------------------------------------------------------
async def test_stress_recorder_writes_jsonl_without_audio(config, monkeypatch, tmp_path):
    monkeypatch.setenv("VOCALIS_HOME", str(tmp_path / "home"))
    recorder = StressRecorder(interval_s=0.05)
    await recorder.start()
    await asyncio.sleep(0.2)
    await recorder.stop()
    from vocalis.monitor.stress import metrics_path

    path = metrics_path()
    assert path.is_file()
    lines = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert lines
    for sample in lines:
        assert "ts" in sample and "uptime_s" in sample and "threads" in sample
        assert "audio" not in sample  # no raw audio ever lands in metrics


async def test_stress_brain_probe_recorded(config, monkeypatch, tmp_path):
    monkeypatch.setenv("VOCALIS_HOME", str(tmp_path / "home"))

    async def brain_ok():
        return True

    recorder = StressRecorder(interval_s=0.05, brain_ok=brain_ok)
    await recorder.start()
    await asyncio.sleep(0.15)
    await recorder.stop()
    assert recorder.last is not None
    assert recorder.last.get("brain_ok") is True


def test_stress_summarize(tmp_path):
    path = tmp_path / "metrics.jsonl"
    rows = [
        {"ts": 1.0, "rss_mb": 100, "cpu_percent": 2, "threads": 5, "brain_ok": True},
        {"ts": 2.0, "rss_mb": 140, "cpu_percent": 4, "threads": 6, "brain_ok": False},
        {"ts": 3.0, "rss_mb": 180, "cpu_percent": 6, "threads": 7, "brain_ok": True},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    report = summarize(path)
    assert report["samples"] == 3
    assert report["duration_s"] == 2.0
    assert report["rss_mb"] == {"min": 100.0, "max": 180.0, "avg": 140.0}
    assert report["threads_max"] == 7
    assert report["brain_uptime_percent"] == 66.7


def test_stress_summarize_empty(tmp_path):
    report = summarize(tmp_path / "missing.jsonl")
    assert report["samples"] == 0
