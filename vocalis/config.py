"""Global configuration for Vocalis.

All user-modifiable settings live in ``~/.vocalis/config.toml`` (created on
first run). Secrets are always read from environment variables, never stored.
The config directory is created with owner-only permissions (0700) because it
may contain voiceprints (biometric data).
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


def _home_dir() -> Path:
    base = Path(os.environ.get("VOCALIS_HOME", Path.home() / ".vocalis"))
    base.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(base, 0o700)  # biometric data may live here
    except OSError:
        pass  # e.g. some network filesystems
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
    """Local small-model (D-VOICE brain) configuration via Ollama."""

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
    # Persistence (TOML via tomli-w; tomllib on py3.11+, tomli otherwise)
    # ------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        def _strip_none(obj: Any) -> Any:
            """TOML has no null type: drop None fields (defaults apply on load)."""
            if isinstance(obj, dict):
                return {k: _strip_none(v) for k, v in obj.items() if v is not None}
            if isinstance(obj, list):
                return [_strip_none(v) for v in obj if v is not None]
            return obj

        return _strip_none(asdict(self))

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
        import tomli_w

        path = path or _home_dir() / "config.toml"
        path.write_text(tomli_w.dumps(self.to_dict()), encoding="utf-8")
        return path

    @classmethod
    def load(cls) -> "VocalisConfig":
        home = _home_dir()
        p = home / "config.toml"
        if p.exists():
            raw: dict[str, Any]
            try:
                import tomllib

                raw = tomllib.loads(p.read_text(encoding="utf-8"))
            except ImportError:  # pragma: no cover - py3.10
                import tomli

                raw = tomli.loads(p.read_text(encoding="utf-8"))
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
