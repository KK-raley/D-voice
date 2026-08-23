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

    backend: embedding model - "eres2net-large" (3D-Speaker SOTA, needs the
    ``voiceprint`` extra) or "resemblyzer" (light d-vectors, ``voice`` extra).
    threshold: cosine similarity required to accept a voice. ``None`` uses
    the backend's calibrated default (0.55 for ERes2Net-large, 0.80 for
    Resemblyzer); override after a real enrollment session if needed.
    """

    backend: str = "eres2net-large"
    threshold: float | None = None
    min_enroll_utterances: int = 3
    sample_rate: int = 16000


@dataclass
class WakeWordConfig:
    """Always-on wake-word detection ("hey D-VOICE").

    backend: "openwakeword" (small ONNX models, ~10 MB RAM, pip install
    openwakeword) or "asr" (keyword match on streaming transcription -
    higher latency but zero extra deps beyond faster-whisper).
    phrases: keyword forms matched (case-insensitive) by the asr backend.
    """

    enabled: bool = True
    backend: str = "asr"
    model: str = "hey_jarvis"  # openwakeword pretrained model name
    threshold: float = 0.5
    phrases: list[str] = field(
        default_factory=lambda: ["hey d-voice", "d voice", "hey d voice", "你好 d-voice"]
    )
    cooldown_s: float = 2.0


@dataclass
class TTSConfig:
    """Text-to-speech defaults and the active voice profile.

    presets: one-click scenario bundles; applying a preset switches
    default_profile to the preset's name (presets are normal profiles).
    agent_voices: agent name -> profile name; narration picks a distinct
    voice per agent so parallel tasks are distinguishable by ear.
    """

    engine: str = "edge"
    default_profile: str = "aria"
    presets: dict[str, dict[str, Any]] = field(
        default_factory=lambda: {
            "focus": {
                "voice": "zh-CN-YunxiNeural",
                "rate": "+15%",
                "pitch": "+0Hz",
                "volume": "+0%",
            },
            "evening": {
                "voice": "zh-CN-XiaoxiaoNeural",
                "rate": "-10%",
                "pitch": "-2Hz",
                "volume": "-5%",
            },
            "presentation": {
                "voice": "zh-CN-YunjianNeural",
                "rate": "+0%",
                "pitch": "+2Hz",
                "volume": "+15%",
            },
        }
    )
    agent_voices: dict[str, str] = field(
        default_factory=lambda: {
            "claude-code": "orion",
            "echo": "aria",
        }
    )
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
class SidecarConfig:
    """IndexTTS sidecar 客户端配置（主框架 -> 独立进程的克隆合成服务）。

    enabled: 总开关，默认 False——不开 sidecar 时 TTS 行为与旧版完全
    一致（仅 Edge-TTS），保证向后兼容；置 True 后
    :func:`vocalis.voice.backends.router.build_router` 会把
    IndexTTSClientBackend 加入路由（sidecar 不可达则自动降级 Edge）。
    base_url / timeout_s: sidecar HTTP 地址与请求超时（克隆合成较慢）。
    fallback_voice: 克隆音色降级到 Edge-TTS 时使用的兜底 preset 音色。
    preset_map: 克隆音色名 -> preset 音色的显式降级映射（优先于
    fallback_voice；TOML 内联表写法 ``{ li = "zh-CN-XiaoxiaoNeural" }``）。
    """

    enabled: bool = False
    base_url: str = "http://127.0.0.1:8765"
    timeout_s: float = 30.0
    fallback_voice: str = "zh-CN-XiaoxiaoNeural"
    preset_map: dict[str, str] = field(default_factory=dict)


@dataclass
class BrainConfig:
    """Local small-model (D-VOICE brain) configuration.

    backend: "ollama" (native API) or "openai-compatible" (any server that
    speaks the OpenAI chat-completions protocol: llama.cpp, LM Studio, vLLM,
    Ollama's own /v1 endpoint, remote APIs...).
    host: Ollama base URL (backend="ollama").
    base_url: OpenAI-compatible base URL, e.g. http://localhost:8080/v1 for
    llama.cpp-server or http://localhost:1234/v1 for LM Studio.
    api_key_env: env var holding the API key (optional for local servers).
    CPU-friendly models: qwen2.5:0.5b/1.5b, gemma3:1b, llama3.2:1b via
    Ollama; any GGUF q4 quant via llama.cpp.
    """

    backend: str = "ollama"
    enabled: bool = True
    host: str = "http://localhost:11434"
    base_url: str | None = None
    api_key_env: str = "DVOICE_API_KEY"
    model: str = "qwen2.5:1.5b-instruct"
    temperature: float = 0.6
    max_tokens: int = 512
    # Rule-based fallback when the local model server is unreachable.
    fallback_to_rules: bool = True


@dataclass
class CliAgentConfig:
    """A CLI coding agent (codex / opencode / aider / ...) bridged by subprocess.

    command may contain a "{instruction}" placeholder; without one the
    instruction is appended as the final argument.
    """

    name: str = "codex"
    command: list[str] = field(default_factory=lambda: ["codex", "exec", "{instruction}"])
    timeout_s: float = 1800.0


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
    wake_word: WakeWordConfig = field(default_factory=WakeWordConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    asr: ASRConfig = field(default_factory=ASRConfig)
    sidecar: SidecarConfig = field(default_factory=SidecarConfig)
    brain: BrainConfig = field(default_factory=BrainConfig)
    monitor: MonitorConfig = field(default_factory=MonitorConfig)
    cli_agents: list[CliAgentConfig] = field(default_factory=list)
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
    def from_dict(cls, raw: dict[str, Any]) -> VocalisConfig:
        cfg = cls()
        for section, values in raw.items():
            if not hasattr(cfg, section):
                continue
            current = getattr(cfg, section)
            if section == "cli_agents" and isinstance(values, list):
                fields_cli = CliAgentConfig.__dataclass_fields__
                cfg.cli_agents = [
                    CliAgentConfig(**{k: v for k, v in entry.items() if k in fields_cli})
                    for entry in values
                    if isinstance(entry, dict)
                ]
                continue
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
    def load(cls) -> VocalisConfig:
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
