"""Local, fail-closed voice authorization before any assistant dispatch.

Standby still captures a bounded microphone buffer and runs local VAD,
speaker verification and ASR. It sends nothing to the brain, agents or TTS.
A wake utterance opens a short, speaker-pinned session; it never executes a
command. Voice verification is probabilistic, not replay/liveness detection.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
import queue
import threading
import time
from collections.abc import Callable
from typing import Any

import numpy as np

from vocalis.config import VocalisConfig
from vocalis.voice.wakeword import WakeWordDetector, _normalize, match_phrase

logger = logging.getLogger(__name__)


class StandbySession:
    """One speaker-pinned session shared by a local microphone or client.

    ``process_audio`` owns no persistent audio or transcription history. All
    blocking local inference runs in worker threads. A lock serializes turns;
    a generation number invalidates work when a concurrent stop/sleep occurs.
    """

    def __init__(self, config: VocalisConfig, gate: Any, transcriber: Any,
                 commander: Any, clock: Callable[[], float] = time.monotonic) -> None:
        self.config = config
        self.gate = gate
        self.transcriber = transcriber
        self.commander = commander
        self.detector = WakeWordDetector(config.wake_word)
        self.clock = clock
        self._lock = asyncio.Lock()
        self._user: str | None = None
        self._deadline = 0.0
        self._generation = 0
        self._processing_generation: int | None = None
        self._reason = "startup"
        self._dispatches = 0
        settings = config.standby
        numeric = (settings.idle_timeout_s, settings.max_utterance_s,
                   settings.min_utterance_s, settings.energy_floor)
        if not all(isinstance(v, (int, float)) and math.isfinite(v) and v > 0
                   for v in numeric):
            raise ValueError("standby limits must be positive finite numbers")
        if not 0.25 <= settings.min_utterance_s <= settings.max_utterance_s <= 60:
            raise ValueError("standby audio limits must satisfy 0.25 <= min <= max <= 60")

    def sleep(self, reason: str = "manual") -> dict[str, Any]:
        # P0-7: clear the active speaker's dialogue context so a later
        # speaker can never touch it (brain keeps only a same-user summary).
        if self._user is not None:
            brain = getattr(self.commander, "brain", None)
            end = getattr(brain, "end_session", None)
            if callable(end):
                try:
                    end(self._user, reason)
                except Exception:
                    logger.debug("end_session failed", exc_info=True)
        self._user = None
        self._deadline = 0.0
        self._generation += 1
        self._reason = reason
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        processing = self._processing_generation is not None
        authorized_processing = self._processing_generation == self._generation
        if self._user is not None and not authorized_processing and self.clock() >= self._deadline:
            self.sleep("idle_timeout")
        return {
            "state": "active" if self._user else "standby",
            "user": self._user,
            "reason": self._reason,
            # The idle timer starts after processing, not while Qwen is busy.
            "remaining_s": (self.config.standby.idle_timeout_s if authorized_processing
                            else max(0.0, self._deadline - self.clock())) if self._user else 0.0,
            "processing": processing,
            "dispatches": self._dispatches,
            "standby_llm_calls": 0,
            "wake_backend": "local-asr",
        }

    def _result(self, kind: str, reason: str = "", **extra: Any) -> dict[str, Any]:
        return {**self.snapshot(), "kind": kind, "reason": reason or self._reason,
                "text": "", "reply": "", **extra}

    def _verify(self, audio: np.ndarray, rate: int, user: str | None = None) -> str | None:
        decision = self.gate.verify(audio, sample_rate=rate)
        if not decision.accepted or not decision.user or not math.isfinite(decision.similarity):
            return None
        if user is not None and decision.user != user:
            return None
        return decision.user

    def _verify_windows(self, audio: np.ndarray, rate: int, user: str) -> bool:
        # A full-utterance embedding can conceal a short second speaker.
        # Independently check overlapping 1s windows including the final tail.
        width = rate
        if audio.size <= width:
            return True
        starts = list(range(0, audio.size - width + 1, rate // 2))
        if starts[-1] != audio.size - width:
            starts.append(audio.size - width)
        for start in starts:
            part = audio[start:start + width]
            if float(np.sqrt(np.mean(part * part))) < self.config.standby.energy_floor:
                continue
            if self._verify(part, rate, user) is None:
                return False
        return True

    def _wake_audio(self, transcription: Any, audio: np.ndarray, rate: int):
        # Match the phrase against aligned words, then verify ONLY that span.
        # No whole-recording fallback: absent/broken timestamps fail closed.
        words = [word for segment in transcription.segments
                 for word in segment.get("words", [])]
        for first in range(len(words)):
            for last in range(first, min(len(words), first + 20)):
                text = " ".join(str(w.get("word", "")) for w in words[first:last + 1])
                if not match_phrase(text, self.config.wake_word.phrases):
                    continue
                # Use a minimal phrase span, never broaden a too-short wake
                # word using an owner's preceding speech to pass verification.
                suffix = " ".join(str(w.get("word", "")) for w in words[first + 1:last + 1])
                if match_phrase(suffix, self.config.wake_word.phrases):
                    break
                start, end = float(words[first]["start"]), float(words[last]["end"])
                if not (math.isfinite(start) and math.isfinite(end)
                        and 0 <= start < end <= audio.size / rate + 0.05):
                    continue
                if end - start < 0.25:
                    continue
                yield audio[int(start * rate):min(audio.size, int(end * rate))]
                break

    async def process_audio(self, audio: np.ndarray,
                            sample_rate: int = 16000) -> dict[str, Any]:
        async with self._lock:
            self.snapshot()
            generation = self._generation
            if not self.config.wake_word.enabled:
                self.sleep("wake_disabled")
                return self._result("ignored", "wake_disabled")
            if not isinstance(sample_rate, int) or not 8000 <= sample_rate <= 96000:
                return self._result("rejected", "invalid_sample_rate")
            audio = np.asarray(audio, dtype=np.float32)
            limits = self.config.standby
            if (audio.ndim != 1 or not limits.min_utterance_s * sample_rate
                    <= audio.size <= limits.max_utterance_s * sample_rate
                    or not np.isfinite(audio).all() or np.max(np.abs(audio)) > 1.01):
                return self._result("rejected", "invalid_audio")
            if float(np.sqrt(np.mean(audio * audio))) < limits.energy_floor:
                return self._result("ignored", "silence")
            try:
                user = await asyncio.to_thread(self._verify, audio, sample_rate, self._user)
                if user is None:
                    return self._result("rejected", "unknown_or_different_speaker")
                same = await asyncio.to_thread(self._verify_windows, audio, sample_rate, user)
                if not same:
                    return self._result("rejected", "mixed_or_different_speaker")
                transcription = await asyncio.to_thread(
                    self.transcriber.transcribe, audio, sample_rate=sample_rate)
                text = transcription.text.strip()
                self.snapshot()
                if generation != self._generation:
                    return self._result("ignored", "session_expired_or_stopped")
                if not text:
                    return self._result("ignored", "empty_transcript")
                if self._user is None:
                    if not match_phrase(text, self.config.wake_word.phrases):
                        return self._result("ignored", "no_wake_phrase")
                    spans = list(self._wake_audio(transcription, audio, sample_rate))
                    if not spans:
                        return self._result("rejected", "wake_phrase_not_aligned")
                    # All possible wake spans must belong to the same user.
                    for span in spans:
                        if await asyncio.to_thread(self._verify, span, sample_rate, user) is None:
                            return self._result("rejected", "wake_speaker_mismatch")
                    if generation != self._generation:
                        return self._result("ignored", "session_expired_or_stopped")
                    if not self.detector.process_text(text, now=self.clock()).detected:
                        return self._result("ignored", "wake_cooldown")
                    self._user = user
                    self._deadline = self.clock() + limits.idle_timeout_s
                    self._reason = "verified_wake"
                    return self._result("wake", reply="我在，请说。")
                if _normalize(text) in {_normalize(p) for p in limits.sleep_phrases}:
                    self.sleep("voice_sleep")
                    return self._result("sleep", reply="已休眠。")
                self._dispatches += 1
                self._processing_generation = generation
                try:
                    command = await self.commander.execute(
                        text, user=self._user, voiceprint="accepted"
                    )
                    # Do not resurrect a session stopped while a command ran.
                    if generation == self._generation:
                        self._deadline = self.clock() + limits.idle_timeout_s
                finally:
                    self._processing_generation = None
                if generation != self._generation:
                    return self._result("ignored", "session_expired_or_stopped")
                return self._result("command", text=text, reply=str(command.get("reply", "")),
                                    command=command)
            except asyncio.CancelledError:
                self.sleep("cancelled")
                raise
            except Exception as exc:
                self.sleep("processing_error")
                logger.warning("Local voice processing failed (%s); check installed speaker/ASR models",
                               type(exc).__name__)
                # Avoid exposing rejected transcripts or biometric details.
                return self._result("error", "local_processing_failed",
                                    error="Local voice processing failed. Check speaker/ASR dependencies, "
                                          "cached model files and enrollment; the assistant remains asleep.")


async def run_microphone(session: StandbySession, on_result: Callable,
                         stop: asyncio.Event, min_pause_s: float = 0.8,
                         adaptive: bool = False) -> None:
    """Run bounded local capture until stopped; propagate device failures.

    The callback runs in PortAudio's capture thread and never invokes models.
    While responding we discard captured audio and reset endpointing, avoiding
    playback echo and stale commands. This secure initial mode is half duplex.
    """
    import sounddevice as sd

    from vocalis.voice.realtime import EnergyVAD, RealtimeSession, TurnDetector

    frames: queue.Queue = queue.Queue(maxsize=100)  # at most 3 s of queued PCM
    overflow = threading.Event()
    paused = threading.Event()
    discard_until_silence = False
    silent_frames = 0

    def capture(indata, _count, _timing, status):
        if paused.is_set():
            return
        if status:
            overflow.set()
        try:
            frames.put_nowait(np.asarray(indata[:, 0], dtype=np.float32).copy())
        except queue.Full:
            overflow.set()

    def next_frame():
        try:
            return frames.get(timeout=0.1)
        except queue.Empty:
            return None

    def chunker():
        return RealtimeSession(
            vad=EnergyVAD(adaptive=adaptive),
            turn=TurnDetector(min_pause_s=min_pause_s,
                              max_turn_s=session.config.standby.max_utterance_s),
            max_buffer_s=session.config.standby.max_utterance_s,
        )

    def clear_frames():
        while True:
            try:
                frames.get_nowait()
            except queue.Empty:
                return

    chunks = chunker()
    stream = sd.InputStream(samplerate=16000, blocksize=480, channels=1,
                            dtype="float32", callback=capture)
    try:
        await asyncio.to_thread(stream.start)
        while not stop.is_set():
            session.snapshot()  # expiry progresses even through total silence
            frame = await asyncio.to_thread(next_frame)
            if frame is None:
                if not stream.active:
                    raise RuntimeError("microphone stream stopped")
                continue
            if overflow.is_set():
                session.sleep("microphone_overflow")
                clear_frames()
                chunks = chunker()
                discard_until_silence = True
                silent_frames = 0
                overflow.clear()
                continue
            if discard_until_silence:
                rms = float(np.sqrt(np.mean(frame * frame)))
                silent_frames = silent_frames + 1 if rms < session.config.standby.energy_floor else 0
                if silent_frames >= max(1, int(min_pause_s / 0.03)):
                    discard_until_silence = False
                    chunks = chunker()
                continue
            for event in chunks.feed_frame(frame):
                if event.kind not in ("utterance_end", "turn_complete") or event.audio is None:
                    continue
                paused.set()
                try:
                    # Force-cut long speech must not become executable fragments.
                    if event.kind == "turn_complete":
                        session.sleep("utterance_too_long")
                        discard_until_silence = True
                        silent_frames = 0
                        result = session._result("rejected", "utterance_too_long")
                    else:
                        result = await session.process_audio(event.audio)
                    if stop.is_set():
                        break
                    response = on_result(result)
                    if inspect.isawaitable(response):
                        await response
                finally:
                    clear_frames()
                    chunks = chunker()
                    paused.clear()
    finally:
        paused.set()
        session.sleep("microphone_stopped")
        await asyncio.to_thread(stream.close)
