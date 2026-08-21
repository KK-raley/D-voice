"""Global configuration for Vocalis.

All user-modifiable settings live in ``~/.vocalis/config.toml`` (created on
first run). Secrets are always read from environment variables, never stored.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


def _home_dir() -> Path:
    base = Path(os.environ.get("VOCALIS_HOME", Path.home() / ".vocalis"))
    base.mkdir(parents=True, exist_ok=True)
    return base


@dataclass
class VoiceGateConfig:
    """Speaker-verification tuning.

    threshold: cosine similarity required to accept a voice. 0.80 is a good
    balance for Resemblyzer d-vectors; raise it for tighter security.
    """

    threshold: float = 0.80
    min_enroll_utterances: int = 3
    sample_rate: int = 16000


@dataclass
class TTSConfig:
    """Text-to-speech defaults and the active voice profile."""

    engine: str = "edge"
    default_profile: str = "aria"
    profiles: dict[str, dict[str, Any]] = field(
        default_factory=lambda: {
            "aria": {
                "voice": "zh-CN-XiaoxiaoNeural",
                "rate": "+8%",
                "pitch": "+2Hz",
                "volume": "+0%",
            },
            "orion": {
                "voice": "en-US-GuyNeural",
                "rate": "+0%",
                "pitch": "-2Hz",
                "volume": "+0%",
            },
            "whisper-calm": {
                "voice": "zh-CN-YunxiNeural",
                "rate": "-10%",
                "pitch": "+0Hz",
                "volume": "+10%",
            },
        }
    )


@dataclass
class ASRConfig:
    model_size: str = "small"
    device: str = "cpu"
    compute_type: str = "int8"
    language: str | None = None  # None = auto-detect


@dataclass
class BrainConfig:
    """Local small-model (Jarvis) configuration via Ollama."""

    enabled: bool = True
    host: str = "http://localhost:11434"
    model: str = "qwen2.5:3b-instruct"
    temperature: float = 0.6
    max_tokens: int = 512
    # Rule-based fallback when Ollama is unreachable.
    fallback_to_rules: bool = True


@dataclass
class MonitorConfig:
    poll_interval_s: float = 2.0
    watchdog_timeout_s: float = 300.0
    notify_on: list[str] = field(
        default_factory=lambda: ["completed", "failed", "stalled"]
    )


@dataclass
class VocalisConfig:
    voice_gate: VoiceGateConfig = field(default_factory=VoiceGateConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    asr: ASRConfig = field(default_factory=ASRConfig)
    brain: BrainConfig = field(default_factory=BrainConfig)
    monitor: MonitorConfig = field(default_factory=MonitorConfig)
    log_level: str = "INFO"

    # ------------------------------------------------------------------
    # Persistence (TOML, no external deps beyond stdlib on py3.11+;
    # tomllib fallback handled in load/save below)
    # ------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "VocalisConfig":
        cfg = cls()
        for section, values in raw.items():
            if not hasattr(cfg, section):
                continue
            current = getattr(cfg, section)
            if hasattr(current, "__dataclass_fields__") and isinstance(values, dict):
                for k, v in values.items():
                    if hasattr(current, k):
                        setattr(current, k, v)
            else:
                setattr(cfg, section, values)
        return cfg

    def save(self, path: Path | None = None) -> Path:
        path = path or _home_dir() / "config.toml"
        try:
            import tomllib  # noqa: F401  (reader)

            writer: Any = None
        except ImportError:  # pragma: no cover - py3.10
            writer = None
        try:
            if writer is not None:  # pragma: no cover
                text = writer.dumps(self.to_dict())  # type: ignore[union-attr]
            else:
                import tomli_w

                text = tomli_w.dumps(self.to_dict())
        except Exception:
            # Last-resort: simple repr-based persistence keeps the project
            # runnable even without TOML writer libraries.
            import json

            text = json.dumps(self.to_dict(), indent=2, ensure_ascii=False)
            path = path.with_suffix(".json")
        path.write_text(text, encoding="utf-8")
        return path

    @classmethod
    def load(cls) -> "VocalisConfig":
        home = _home_dir()
        for name in ("config.toml", "config.json"):
            p = home / name
            if not p.exists():
                continue
            raw: dict[str, Any]
            if p.suffix == ".toml":
                try:
                    import tomllib

                    raw = tomllib.loads(p.read_text(encoding="utf-8"))
                except ImportError:  # pragma: no cover
                    import tomli

                    raw = tomli.loads(p.read_text(encoding="utf-8"))
            else:
                import json

                raw = json.loads(p.read_text(encoding="utf-8"))
            return cls.from_dict(raw)
        cfg = cls()
        cfg.save()
        return cfg


def home_dir() -> Path:
    return _home_dir()


def profiles_dir() -> Path:
    p = _home_dir() / "profiles"
    p.mkdir(parents=True, exist_ok=True)
    return p


def audio_cache_dir() -> Path:
    p = _home_dir() / "audio_cache"
    p.mkdir(parents=True, exist_ok=True)
    return p
