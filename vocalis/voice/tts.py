"""Unified TTS with user-tunable voice profiles.

The default backend is Edge-TTS (free, no API key, dozens of neural voices).
A ``VoiceProfile`` controls voice identity plus rate / pitch / volume so each
user can shape how agent responses *sound*. Engines are pluggable: implement
:class:`TTSEngine` and register it to add e.g. XTTS-v2 or a local Piper model.

Playback is always offloaded to a worker thread so synthesis never blocks
the asyncio event loop.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from vocalis.config import VocalisConfig, audio_cache_dir
from vocalis.server.events import EventBus, EventType, bus


@dataclass
class VoiceProfile:
    """Tunable output-voice characteristics persisted per user."""

    name: str = "aria"
    voice: str = "zh-CN-XiaoxiaoNeural"
    rate: str = "+0%"      # e.g. "+15%" faster, "-10%" slower
    pitch: str = "+0Hz"    # e.g. "+4Hz" brighter
    volume: str = "+0%"    # e.g. "+20%" louder

    def apply_delta(self, rate: int = 0, pitch: int = 0, volume: int = 0) -> VoiceProfile:
        def merge(current: str, delta: int, unit: str) -> str:
            try:
                value = int(current[1 : -len(unit)]) if unit in current else 0
            except ValueError:
                value = 0
            value = max(-100, min(100, value + delta))
            return f"{'+' if value >= 0 else ''}{value}{unit}"

        self.rate = merge(self.rate, rate, "%")
        self.pitch = merge(self.pitch, pitch, "Hz")
        self.volume = merge(self.volume, volume, "%")
        return self

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SpeakResult:
    ok: bool
    audio_path: Path | None = None
    audio_bytes: bytes | None = None
    engine: str = "edge"
    profile: str = ""
    characters: int = 0
    error: str | None = None


class TTSEngine:  # pragma: no cover - interface
    name: str = "base"

    async def synthesize(self, text: str, profile: VoiceProfile) -> bytes: ...


class EdgeTTSEngine(TTSEngine):
    """Microsoft Edge neural voices - free, keyless, high quality."""

    name = "edge"

    async def synthesize(self, text: str, profile: VoiceProfile) -> bytes:
        import edge_tts

        communicate = edge_tts.Communicate(
            text=text,
            voice=profile.voice,
            rate=profile.rate,
            pitch=profile.pitch,
            volume=profile.volume,
        )
        chunks: list[bytes] = []
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                chunks.append(chunk["data"])
        if not chunks:
            raise RuntimeError("edge-tts produced no audio")
        return b"".join(chunks)


class TTSService:
    """Profile-aware speech synthesis + local playback."""

    def __init__(
        self,
        config: VocalisConfig | None = None,
        event_bus: EventBus | None = None,
        engines: dict[str, TTSEngine] | None = None,
    ) -> None:
        self.config = config or VocalisConfig.load()
        self.bus = event_bus or bus
        self.engines: dict[str, TTSEngine] = engines or {EdgeTTSEngine.name: EdgeTTSEngine()}
        self._profiles: dict[str, VoiceProfile] = {
            name: VoiceProfile(name=name, **params)
            for name, params in self.config.tts.profiles.items()
        }

    # -- profiles ------------------------------------------------------
    def profiles(self) -> dict[str, VoiceProfile]:
        return {k: replace(v) for k, v in self._profiles.items()}

    def get_profile(self, name: str | None = None) -> VoiceProfile:
        """Return a *copy* of the profile - mutating it never touches the store."""
        name = name or self.config.tts.default_profile
        if name not in self._profiles:
            raise KeyError(f"unknown voice profile '{name}' - available: {list(self._profiles)}")
        return replace(self._profiles[name])

    def upsert_profile(self, profile: VoiceProfile) -> None:
        self._profiles[profile.name] = replace(profile)
        self.config.tts.profiles[profile.name] = {
            "voice": profile.voice,
            "rate": profile.rate,
            "pitch": profile.pitch,
            "volume": profile.volume,
        }
        self.config.save()

    # -- synthesis -----------------------------------------------------
    async def synthesize(
        self, text: str, profile_name: str | None = None
    ) -> SpeakResult:
        """Synthesize to bytes + cache file without playing."""
        profile = self.get_profile(profile_name)
        engine_name = self.config.tts.engine
        engine = self.engines.get(engine_name)
        if engine is None:
            return SpeakResult(ok=False, error=f"engine '{engine_name}' not registered", profile=profile.name)

        await self.bus.publish(EventType.TTS_SPEAKING, profile=profile.name, text=text[:120])
        try:
            audio_bytes = await engine.synthesize(text, profile)
        except Exception as e:
            return SpeakResult(ok=False, engine=engine_name, profile=profile.name, error=str(e))

        key = hashlib.sha1(f"{text}|{profile.name}|{profile.voice}|{profile.rate}|{profile.pitch}|{profile.volume}".encode()).hexdigest()[:16]
        path = audio_cache_dir() / f"{key}.mp3"
        path.write_bytes(audio_bytes)
        return SpeakResult(
            ok=True,
            audio_path=path,
            audio_bytes=audio_bytes,
            engine=engine_name,
            profile=profile.name,
            characters=len(text),
        )

    async def speak(
        self,
        text: str,
        profile_name: str | None = None,
        play: bool = True,
    ) -> SpeakResult:
        result = await self.synthesize(text, profile_name)
        if result.ok and play and result.audio_path is not None:
            # Never block the event loop with audio hardware.
            await asyncio.to_thread(self.play_file, result.audio_path)
        return result

    @staticmethod
    def play_file(path: Path) -> None:
        """Cross-platform blocking playback (call via asyncio.to_thread)."""
        try:
            import winsound

            winsound.PlaySound(str(path), winsound.SND_FILENAME)
            return
        except Exception:
            pass
        players: list[tuple[str, list[str]]] = [
            ("afplay", ["afplay", str(path)]),
            ("mpv", ["mpv", "--no-video", "--really-quiet", str(path)]),
            ("ffplay", ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", str(path)]),
        ]
        import shutil
        import subprocess

        for name, cmd in players:
            if shutil.which(name):
                subprocess.run(cmd, check=False)
                return
