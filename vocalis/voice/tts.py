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
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from vocalis.config import VocalisConfig, audio_cache_dir
from vocalis.server.events import EventBus, EventType, bus

logger = logging.getLogger("vocalis.tts")

#: 合成前文本变换钩子：依次应用，``str -> str``（如 "2m41s" -> "两分四十一秒"）。
PreTextHook = Callable[[str], str]
#: 合成后回调钩子：``(text, ok) -> None``，成功与失败路径都会触发（统计/缓存用）。
PostTextHook = Callable[[str, bool], None]


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


# -- voice enumeration ------------------------------------------------
FALLBACK_VOICES: list[dict[str, str]] = [
    {"ShortName": "en-US-AriaNeural", "Gender": "Female", "Locale": "en-US"},
    {"ShortName": "en-US-GuyNeural", "Gender": "Male", "Locale": "en-US"},
    {"ShortName": "zh-CN-XiaoxiaoNeural", "Gender": "Female", "Locale": "zh-CN"},
    {"ShortName": "zh-CN-YunxiNeural", "Gender": "Male", "Locale": "zh-CN"},
]


async def list_voices() -> list[dict]:
    """Enumerate Edge-TTS voices as ``{ShortName, Gender, Locale}`` dicts.

    Results are normalized to exactly those three keys and sorted by
    Locale. When edge-tts is not installed (or the network call fails) a
    small built-in fallback list is returned so the voice picker always
    has something to offer.
    """
    voices: list[dict] = []
    try:
        import edge_tts

        raw = await edge_tts.list_voices()
        voices = [
            {
                "ShortName": str(v.get("ShortName", "")),
                "Gender": str(v.get("Gender", "")),
                "Locale": str(v.get("Locale", "")),
            }
            for v in raw
        ]
    except Exception:
        voices = [dict(v) for v in FALLBACK_VOICES]
    return sorted(voices, key=lambda v: (v["Locale"], v["ShortName"]))


def filter_voices(
    voices: list[dict], locale: str | None = None, gender: str | None = None
) -> list[dict]:
    """Filter a voice list for the picker (pure function).

    ``locale`` is a prefix match ("zh" matches zh-CN / zh-TW), ``gender``
    an exact case-insensitive match. Missing / empty filters return the
    list unchanged.
    """
    out = voices
    if locale:
        prefix = locale.lower()
        out = [v for v in out if str(v.get("Locale", "")).lower().startswith(prefix)]
    if gender:
        target = gender.lower()
        out = [v for v in out if str(v.get("Gender", "")).lower() == target]
    return out


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
    """Profile-aware speech synthesis + local playback.

    文本钩子（Track C4）：``pre_hooks`` 在合成前依次变换文本（``str ->
    str``，如数字口语化）；``post_hooks`` 在合成结束后以 ``(text, ok)``
    回调（可用于统计/缓存）。两类钩子均可经构造函数注入，也可在运行时
    用 :meth:`add_pre_hook` / :meth:`add_post_hook` 追加；单个钩子异常
    只记录 warning，绝不影响合成本身。详见 docs/hooks.md。
    """

    def __init__(
        self,
        config: VocalisConfig | None = None,
        event_bus: EventBus | None = None,
        engines: dict[str, TTSEngine] | None = None,
        pre_hooks: list[PreTextHook] | None = None,
        post_hooks: list[PostTextHook] | None = None,
    ) -> None:
        self.config = config or VocalisConfig.load()
        self.bus = event_bus or bus
        self.engines: dict[str, TTSEngine] = engines or {EdgeTTSEngine.name: EdgeTTSEngine()}
        self._pre_hooks: list[PreTextHook] = list(pre_hooks or [])
        self._post_hooks: list[PostTextHook] = list(post_hooks or [])
        self._profiles: dict[str, VoiceProfile] = {
            name: VoiceProfile(name=name, **params)
            for name, params in self.config.tts.profiles.items()
        }

    # -- text hooks ----------------------------------------------------
    def add_pre_hook(self, hook: PreTextHook) -> None:
        """追加一个合成前文本变换钩子（签名 ``str -> str``）。"""
        self._pre_hooks.append(hook)

    def add_post_hook(self, hook: PostTextHook) -> None:
        """追加一个合成后回调钩子（签名 ``(text, ok) -> None``）。"""
        self._post_hooks.append(hook)

    def _apply_pre_hooks(self, text: str) -> str:
        """依次应用 pre 钩子；单个钩子异常记 warning 并保留当前文本。"""
        for hook in self._pre_hooks:
            try:
                text = hook(text)
            except Exception:
                logger.warning(
                    "TTS pre-hook %r failed; text left unchanged",
                    getattr(hook, "__name__", hook),
                    exc_info=True,
                )
        return text

    def _run_post_hooks(self, text: str, ok: bool) -> None:
        """触发 post 钩子；单个钩子异常记 warning，不影响其他钩子。"""
        for hook in self._post_hooks:
            try:
                hook(text, ok)
            except Exception:
                logger.warning(
                    "TTS post-hook %r failed",
                    getattr(hook, "__name__", hook),
                    exc_info=True,
                )

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

    def apply_preset(self, name: str) -> VoiceProfile:
        """Upsert a scenario preset as a live profile and make it the default.

        Presets are one-click bundles (focus / evening / presentation)
        defined in ``config.tts.presets``; applying one materializes it as
        a normal profile, persists it, and switches ``default_profile``.
        """
        presets = self.config.tts.presets
        if name not in presets:
            raise KeyError(f"unknown preset '{name}' - available: {sorted(presets)}")
        profile = VoiceProfile(name=name, **dict(presets[name]))
        self.upsert_profile(profile)
        self.config.tts.default_profile = name
        self.config.save()
        return profile

    def profile_for_agent(self, agent: str) -> str:
        """Profile name an agent should speak with; ``default_profile`` fallback.

        Per-agent voices let parallel tasks be distinguished by ear
        ("listen and know which agent is talking").
        """
        return self.config.tts.agent_voices.get(agent, self.config.tts.default_profile)

    # -- synthesis -----------------------------------------------------
    async def synthesize(
        self, text: str, profile_name: str | None = None
    ) -> SpeakResult:
        """Synthesize to bytes + cache file without playing.

        文本先经 ``pre_hooks`` 依次变换（事件、引擎调用与缓存键用的都是
        变换后的文本），结束后无论成败都触发 ``post_hooks(text, ok)``。
        """
        text = self._apply_pre_hooks(text)
        profile = self.get_profile(profile_name)
        engine_name = self.config.tts.engine
        engine = self.engines.get(engine_name)
        if engine is None:
            self._run_post_hooks(text, False)
            return SpeakResult(ok=False, error=f"engine '{engine_name}' not registered", profile=profile.name)

        await self.bus.publish(EventType.TTS_SPEAKING, profile=profile.name, text=text[:120])
        try:
            audio_bytes = await engine.synthesize(text, profile)
        except Exception as e:
            self._run_post_hooks(text, False)
            return SpeakResult(ok=False, engine=engine_name, profile=profile.name, error=str(e))

        key = hashlib.sha1(f"{text}|{profile.name}|{profile.voice}|{profile.rate}|{profile.pitch}|{profile.volume}".encode()).hexdigest()[:16]
        path = audio_cache_dir() / f"{key}.mp3"
        path.write_bytes(audio_bytes)
        self._run_post_hooks(text, True)
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


# ----------------------------------------------------------------------
# Interruptible playback (barge-in support)
# ----------------------------------------------------------------------
def _blocking_play(path: Path, stop: threading.Event) -> None:
    """Playback kernel for :class:`InterruptiblePlayer` (runs in a thread).

    Windows: winsound plays synchronously in this worker thread; the owner
    interrupts by calling ``PlaySound(None, SND_PURGE)`` from another thread,
    which cancels the in-flight playback. Unix: spawn mpv/ffplay/afplay via
    Popen and poll ``stop`` so ``terminate()`` lands within ~20 ms.
    """
    try:
        import winsound
    except ImportError:
        winsound = None

    if winsound is not None:
        winsound.PlaySound(str(path), winsound.SND_FILENAME)
        return

    import shutil
    import subprocess

    players: list[tuple[str, list[str]]] = [
        ("afplay", ["afplay", str(path)]),
        ("mpv", ["mpv", "--no-video", "--really-quiet", str(path)]),
        ("ffplay", ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", str(path)]),
    ]
    for name, cmd in players:
        if shutil.which(name):
            proc = subprocess.Popen(cmd)
            try:
                while proc.poll() is None:
                    if stop.is_set():
                        proc.terminate()
                        break
                    time.sleep(0.02)
            finally:
                if proc.poll() is None:
                    proc.kill()
            return


class InterruptiblePlayer:
    """Threaded audio playback that can be stopped mid-utterance.

    Needed for barge-in: while D-VOICE speaks, the mic loop keeps running;
    once the user is confirmed to be talking over the assistant (see
    ``vocalis.voice.realtime.BargeInController``), ``stop()`` cancels the
    reply within milliseconds. Mirrors the hard-cancel behavior of
    HuggingFace speech-to-speech and TEN Framework voice agents.

    ``TTSService.play_file`` is intentionally untouched - this class is the
    streaming counterpart, not a replacement.
    """

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def play(self, path: str | Path) -> None:
        """Start playing ``path`` in a background thread (non-blocking)."""
        self.stop()
        self.wait(timeout=2.0)  # let a previous kernel unwind first
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._worker, args=(Path(path),), daemon=True
        )
        self._thread.start()

    def _worker(self, path: Path) -> None:
        try:
            _blocking_play(path, self._stop)
        except Exception:  # playback must never crash the conversation loop
            pass

    def stop(self) -> None:
        """Cancel playback immediately (safe when nothing is playing)."""
        self._stop.set()
        try:
            import winsound

            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:
            pass  # non-Windows or winsound unavailable: Popen path handles it

    def wait(self, timeout: float | None = None) -> bool:
        """Wait for the worker thread; True if it finished (or never ran)."""
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    @property
    def playing(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive() and not self._stop.is_set()
