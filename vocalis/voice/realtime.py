"""Human-like realtime voice interaction: chunking / VAD / turn detection / barge-in.

This module implements the four building blocks D-VOICE needs to converse in
full duplex instead of "record 4 s, transcribe, reply" walkie-talkie style:

1. :class:`EnergyVAD`   - frame-level voice activity detection (state machine).
2. :class:`TurnDetector` - decides *when the user is done speaking*.
3. :class:`BargeInController` - lets the user interrupt D-VOICE mid-sentence.
4. :class:`RealtimeSession`  - chunks the mic stream into complete utterances.

Research notes (2026-08 survey; see docs/realtime.md for full citations)
-----------------------------------------------------------------------
* **HuggingFace speech-to-speech** (github.com/huggingface/speech-to-speech,
  the 2026 de-facto open reference stack: Silero VAD v5 -> STT -> LLM -> TTS
  over an OpenAI-Realtime-compatible WebSocket). Key ideas borrowed:
  - Frame-level VAD feeding a *separate* turn-endpointing stage; VAD only
    answers "is this speech?", never "is the turn over?".
  - ``min_speech_ms`` (~384 ms default): speech must persist before a
    ``speech_started`` turn is confirmed -> our ``speech_confirm_frames``.
  - Hangover / ``short_segment_merge_ms``: bridge short intra-speech pauses
    so words are not shredded -> our ``hangover_frames``.
  - ``speculative_reopen_ms`` (800 ms): a soft-ended turn reopens if the
    speaker resumes quickly -> our TurnDetector keeps the turn open while
    the pause is below ``min_pause_s`` (resume = same turn).
  - Barge-in hard-cancels TTS on confirmed user speech.
* **TEN Framework / Agora** (github.com/TEN-framework): every stage (VAD,
  STT, LLM, TTS, Turn Detection) is an isolated, message-passing extension.
  Borrowed: the strict separation of VAD / turn detection / barge-in into
  independently testable state machines, and the insight that plain silence
  timing is a heuristic stand-in for their LLM-based finished/unfinished/
  wait classifier (TEN Turn Detection, a fine-tuned Qwen2.5-7B, reaches
  ~90-99 % endpointing accuracy vs ~60-75 % for silence-only heuristics).
  Our TurnDetector exposes the same three-way outcome (listening / turn_end
  / turn_timeout) so a semantic judge can later replace the timing logic.
* **3D-Speaker** (Alibaba DAMO, modelscope/3D-Speaker): CAM++ streaming
  speaker embeddings are consumed *at utterance boundaries* in realtime
  pipelines (continuous authentication, <200 ms/frame). Borrowed: complete
  utterances are emitted with their audio + start/end timestamps, which is
  exactly the hook a VoiceGate / CAM++ verifier needs per turn.
* Practical tuning folklore (AssemblyAI / production voice-agent writeups):
  natural human turn gaps are 200-300 ms; endpoints fire around 600 ms
  minimum pause with a ~1.5 s hard ceiling (elderly/slow speakers ~2.5 s);
  barge-in cancellation should land within ~100-150 ms of detected speech;
  a 1-2 frame speech confirmation filters coughs ("uh" back-channels).

Everything here is numpy-only, offline-testable, and time-driven by the
caller: heavy dependencies (sounddevice/whisper/edge-tts) live in the CLI
layer, never in this module.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass

import numpy as np

logger = logging.getLogger("vocalis.voice.realtime")

# Frames kept at the tail of a flushed utterance after the last voiced frame
# (a little breathing room helps Whisper's VAD filter find the boundary).
_TAIL_PAD_FRAMES = 2
# Utterances shorter than this many seconds are treated as noise bursts.
_MIN_UTTERANCE_S = 0.3


@dataclass
class VadEvent:
    """One frame-level VAD transition.

    kinds:
      * ``speech_start`` - a speech run was just confirmed; ``t`` points at
        the *first* voiced frame of the run (not the confirming frame).
      * ``speech``      - a voiced frame inside an ongoing speech run
        (extension kind consumed by TurnDetector/BargeInController).
      * ``speech_stop`` - hangover expired; ``t`` is the end of the *last*
        voiced frame, not the moment the stop was decided.
      * ``silence``     - emitted once right after a speech_stop.
    """

    kind: str
    t: float
    voiced: bool = False


@dataclass
class RealtimeEvent:
    """A session-level event ready for downstream consumption (ASR/TTS).

    kinds:
      * ``utterance_end`` - the user finished a turn (natural pause).
      * ``turn_complete`` - the turn was force-cut (timeout / buffer cap).
      * ``barge_in``      - the user started speaking while D-VOICE talks.
    """

    kind: str
    audio: np.ndarray | None = None
    start: float | None = None
    end: float | None = None


# ----------------------------------------------------------------------
# 1. Frame-level VAD
# ----------------------------------------------------------------------
class EnergyVAD:
    """Energy-based frame VAD with confirmation + hangover (HF-style).

    State machine ``idle -> speaking``:

    * idle: ``speech_confirm_frames`` consecutive voiced frames confirm a
      speech run and emit ``speech_start`` (guards single-frame clicks,
      mirroring HF speech-to-speech's ``min_speech_ms``).
    * speaking: voiced frames reset the hangover counter; after
      ``hangover_frames`` silent frames a ``speech_stop`` is emitted
      (mirrors Silero/HF hangover so intra-word pauses don't shred speech).
    * adaptive=True: threshold tracks a robust noise-floor estimate
      (rolling median + k * scaled MAD over ``noise_window`` frames);
      during the first ``warmup_frames`` frames the detector stays neutral
      while statistics build up. Default is a fixed ``energy_floor``.
    """

    def __init__(
        self,
        frame_ms: int = 30,
        energy_floor: float = 0.01,
        sample_rate: int = 16000,
        adaptive: bool = False,
        hangover_frames: int = 10,          # 10 * 30 ms = 300 ms hangover
        speech_confirm_frames: int = 2,     # 60 ms to confirm speech start
        noise_window: int = 100,            # ~3 s rolling noise estimate
        noise_k: float = 3.0,
        warmup_frames: int = 20,
    ) -> None:
        self.frame_ms = frame_ms
        self.frame_s = frame_ms / 1000.0
        self.energy_floor = energy_floor
        self.sample_rate = sample_rate
        self.adaptive = adaptive
        self.hangover_frames = hangover_frames
        self.speech_confirm_frames = speech_confirm_frames
        self.noise_window = noise_window
        self.noise_k = noise_k
        self.warmup_frames = warmup_frames

        self._n = 0                          # frames fed (0-based current idx)
        self._state = "idle"
        self._confirm_run = 0
        self._candidate_idx = 0              # first voiced frame of a run
        self._hangover = 0
        self._last_voiced_idx = -1
        self._emit_silence_next = False
        self._rms_hist: deque[float] = deque(maxlen=noise_window)

        #: per-frame voicing decision of the most recent feed() - used by
        #: BargeInController for consecutive-frame counting.
        self.last_frame_voiced: bool = False

    # -- threshold -----------------------------------------------------
    def _threshold(self) -> float:
        """Fixed floor, or median + k*MAD noise estimate (adaptive)."""
        if not self.adaptive or len(self._rms_hist) < self.warmup_frames:
            return self.energy_floor
        arr = np.asarray(self._rms_hist, dtype=np.float64)
        med = float(np.median(arr))
        mad = float(np.median(np.abs(arr - med)))
        # 1.4826 * MAD is the consistent sigma estimate for Gaussian noise;
        # the epsilon keeps a perfectly constant hum from sitting exactly on
        # the decision boundary.
        return max(self.energy_floor * 0.5, med + self.noise_k * 1.4826 * mad + 1e-4)

    # -- streaming API ---------------------------------------------------
    def feed(self, frame: np.ndarray) -> VadEvent | None:
        """Feed one frame; return a transition event or None."""
        frame = np.asarray(frame, dtype=np.float32)
        rms = float(np.sqrt(np.mean(np.square(frame)))) if frame.size else 0.0
        self._rms_hist.append(rms)

        idx = self._n
        now = (idx + 1) * self.frame_s
        calibrating = self.adaptive and len(self._rms_hist) < self.warmup_frames
        voiced = (not calibrating) and rms > self._threshold()
        self.last_frame_voiced = voiced
        self._n += 1

        if self._state == "idle":
            if voiced:
                self._confirm_run += 1
                if self._confirm_run == 1:
                    self._candidate_idx = idx
                if self._confirm_run >= self.speech_confirm_frames:
                    self._state = "speaking"
                    self._hangover = 0
                    self._last_voiced_idx = idx
                    # t = start of the run's first voiced frame.
                    return VadEvent(kind="speech_start", t=self._candidate_idx * self.frame_s, voiced=True)
            else:
                self._confirm_run = 0
                if self._emit_silence_next:
                    self._emit_silence_next = False
                    return VadEvent(kind="silence", t=now, voiced=False)
        else:  # speaking
            if voiced:
                self._hangover = 0
                self._last_voiced_idx = idx
                return VadEvent(kind="speech", t=now, voiced=True)
            self._hangover += 1
            if self._hangover > self.hangover_frames:
                self._state = "idle"
                self._confirm_run = 0
                self._emit_silence_next = True
                # t = end of the last voiced frame (hangover not counted).
                return VadEvent(
                    kind="speech_stop",
                    t=(self._last_voiced_idx + 1) * self.frame_s,
                    voiced=False,
                )
        return None


# ----------------------------------------------------------------------
# 2. Turn endpointing
# ----------------------------------------------------------------------
class TurnDetector:
    """Decide whether the user finished their turn (TEN-style 3-way output).

    Returns one of:

    * ``"listening"``   - keep collecting; the pause is still short enough to
      be a thinking gap (the humanlike behavior: humans hold the floor for
      200-300 ms gaps, so we never cut below ``min_pause_s``).
    * ``"turn_end"``    - pause exceeded ``min_pause_s``: the turn is over.
    * ``"turn_timeout"``- the turn exceeded ``max_turn_s`` in total: force a
      cut so a rambling (or stuck-open) mic cannot block the pipeline.

    Pauses shorter than ``min_pause_s`` *never* end the turn - if the speaker
    resumes, the same turn simply continues (HF speech-to-speech calls this
    the speculative-reopen window; we get it for free by not ending early).

    ``max_pause_s`` caps the wait regardless of ``min_pause_s``: even a
    deliberately large ``min_pause_s`` (e.g. 2.5 s for slow speakers) still
    hard-stops at ``max_pause_s``.

    Pure state machine: all timing comes from the ``now`` argument, so tests
    inject clocks instead of sleeping.
    """

    def __init__(
        self,
        min_pause_s: float = 0.8,
        max_turn_s: float = 30.0,
        max_pause_s: float = 2.5,
    ) -> None:
        self.min_pause_s = min_pause_s
        self.max_turn_s = max_turn_s
        self.max_pause_s = max_pause_s
        self._in_turn = False
        self._turn_start = 0.0
        self._last_voice = 0.0

    @property
    def in_turn(self) -> bool:
        return self._in_turn

    def reset(self) -> None:
        """Abandon the current turn without emitting a decision."""
        self._in_turn = False

    def update(self, vad_event: VadEvent | None, now: float) -> str:
        """Advance the state machine by one frame.

        ``vad_event`` is whatever the VAD produced for this frame (often
        None - silent frames carry no transition).
        """
        if vad_event is not None:
            if vad_event.kind == "speech_stop":
                # The VAD reports the *end of the last voiced frame*; anchor
                # the pause there so hangover time is not billed to the user.
                self._last_voice = vad_event.t
            elif vad_event.kind in ("speech_start", "speech"):
                if not self._in_turn:
                    self._in_turn = True
                    self._turn_start = vad_event.t
                self._last_voice = now

        if not self._in_turn:
            return "listening"

        # Force-cut long monologues first (TEN's turn timeout equivalent).
        if now - self._turn_start >= self.max_turn_s:
            self._in_turn = False
            return "turn_timeout"
        # Endpoint: pause since the last voiced frame crossed the threshold.
        if now - self._last_voice >= min(self.min_pause_s, self.max_pause_s):
            self._in_turn = False
            return "turn_end"
        return "listening"


# ----------------------------------------------------------------------
# 3. Barge-in
# ----------------------------------------------------------------------
class BargeInController:
    """Interrupt D-VOICE's playback when the user starts talking over it.

    A barge-in is confirmed only after ``confirm_frames`` *consecutive*
    voiced frames (~2 x 30 ms): a single cough / desk-bump frame must not
    cancel the reply. HF speech-to-speech gates its hard cancel the same way
    (``min_speech_ms``); production writeups budget ~100-150 ms from speech
    to cancellation, which two 30 ms frames comfortably meet.

    Two feeding styles:

    * :meth:`observe` - frame-level boolean (used by RealtimeSession).
    * :meth:`should_interrupt` - consume VadEvent objects directly.
    """

    def __init__(self, confirm_frames: int = 2) -> None:
        self.confirm_frames = confirm_frames
        self.interrupted_at: float | None = None
        self._streak = 0

    def observe(self, voiced: bool, now: float) -> bool:
        """Feed one frame's voicing; True once a barge-in is confirmed."""
        self._streak = self._streak + 1 if voiced else 0
        if self._streak >= self.confirm_frames:
            self.interrupted_at = now
            self._streak = 0  # fire once per burst
            return True
        return False

    def should_interrupt(self, vad_event: VadEvent | None) -> bool:
        """Event-based convenience wrapper around observe()."""
        if vad_event is None:
            self._streak = 0
            return False
        voiced = vad_event.kind in ("speech_start", "speech")
        return self.observe(voiced, vad_event.t)

    def reset(self) -> None:
        """Re-arm after an interruption (streak and timestamp cleared)."""
        self._streak = 0
        self.interrupted_at = None


# ----------------------------------------------------------------------
# 4. Session (chunking state machine)
# ----------------------------------------------------------------------
@dataclass
class _BufferedFrame:
    t: float                 # frame start time (s)
    data: np.ndarray
    voiced: bool


class RealtimeSession:
    """Combine VAD + turn detection + barge-in into a chunking pipeline.

    Feed 30 ms mic frames in; receive :class:`RealtimeEvent` objects out:

    * Voiced frames accumulate into an utterance buffer (with a small
      pre-roll so the confirmation frames are not clipped off the start).
    * When the TurnDetector endpoints the turn, the buffer is trimmed of
      trailing silence and flushed as ``utterance_end`` (natural pause) or
      ``turn_complete`` (forced cut) - this is the "every so often hand the
      collected speech to ASR" chunking step.
    * While ``bot_speaking`` is True (set by the caller around TTS
      playback), confirmed user speech fires ``barge_in`` and disarms
      itself until the caller re-arms for the next reply.

    The emitted utterance carries full audio + start/end timestamps: exactly
    the boundary a 3D-Speaker/CAM++ verifier or VoiceGate wants per turn.
    """

    def __init__(
        self,
        vad: EnergyVAD | None = None,
        turn: TurnDetector | None = None,
        bargein: BargeInController | None = None,
        sample_rate: int = 16000,
        frame_ms: int = 30,
        max_buffer_s: float = 60.0,
        pre_roll_frames: int = 8,
    ) -> None:
        self.sample_rate = sample_rate
        self.frame_ms = frame_ms
        self.frame_s = frame_ms / 1000.0
        self.max_buffer_s = max_buffer_s
        self.vad = vad or EnergyVAD(frame_ms=frame_ms, sample_rate=sample_rate)
        self.turn = turn or TurnDetector()
        self.bargein = bargein or BargeInController()

        #: Set True while D-VOICE's TTS is playing; user speech then fires
        #: barge_in events (auto-disarmed on the first firing).
        self.bot_speaking = False

        self._frames_fed = 0
        self._pre_roll: deque[_BufferedFrame] = deque(maxlen=pre_roll_frames)
        self._buffer: list[_BufferedFrame] = []
        self._active = False
        self._last_voiced_pos = -1

    # -- internals -------------------------------------------------------
    def _begin_turn_buffer(self, start_t: float) -> None:
        self._active = True
        self._last_voiced_pos = -1
        # Fold pre-roll frames at/after the speech start back into the
        # utterance so the confirm frames are not lost.
        for fr in self._pre_roll:
            if fr.t >= start_t - 1e-9:
                self._append(fr)

    def _append(self, fr: _BufferedFrame) -> None:
        self._buffer.append(fr)
        if fr.voiced:
            self._last_voiced_pos = len(self._buffer) - 1

    def _flush(self, kind: str) -> RealtimeEvent | None:
        """Trim trailing silence and emit the completed utterance."""
        if not self._buffer:
            self._active = False
            return None
        keep = self._last_voiced_pos + 1 + _TAIL_PAD_FRAMES
        keep = max(1, min(keep, len(self._buffer)))
        frames = self._buffer[:keep]
        self._buffer = []
        self._active = False
        self._last_voiced_pos = -1
        duration = keep * self.frame_s
        if duration < _MIN_UTTERANCE_S:
            # Too short to be speech (a cough that slipped through): drop.
            logger.debug("dropping %.2fs micro-utterance", duration)
            return None
        audio = np.concatenate([f.data for f in frames])
        return RealtimeEvent(
            kind=kind,
            audio=audio,
            start=frames[0].t,
            end=frames[-1].t + self.frame_s,
        )

    # -- streaming API ---------------------------------------------------
    def feed_frame(self, frame: np.ndarray) -> list[RealtimeEvent]:
        """Feed one mic frame; return zero or more session events."""
        idx = self._frames_fed
        now = (idx + 1) * self.frame_s
        self._frames_fed += 1

        frame = np.asarray(frame, dtype=np.float32)
        buffered = _BufferedFrame(t=idx * self.frame_s, data=frame, voiced=False)
        self._pre_roll.append(buffered)

        events: list[RealtimeEvent] = []
        vad_event = self.vad.feed(frame)
        buffered.voiced = self.vad.last_frame_voiced

        # -- barge-in supervision (only meaningful while D-VOICE talks) --
        if self.bot_speaking and self.bargein.observe(self.vad.last_frame_voiced, now):
            events.append(RealtimeEvent(kind="barge_in"))
            self.bot_speaking = False  # fire once; caller re-arms next reply

        # -- utterance accumulation --------------------------------------
        if vad_event is not None and vad_event.kind == "speech_start":
            self._begin_turn_buffer(vad_event.t)
        elif self._active:
            self._append(buffered)
        elif vad_event is not None and vad_event.kind == "speech":
            # Speech continuing right after a forced flush (timeout/cap):
            # open a fresh buffer for the remainder (VAD is still speaking).
            self._begin_turn_buffer(buffered.t)

        # -- memory guard --------------------------------------------------
        if self._active and len(self._buffer) * self.frame_s >= self.max_buffer_s:
            self.turn.reset()
            ev = self._flush("turn_complete")
            if ev is not None:
                events.append(ev)

        # -- turn endpointing ---------------------------------------------
        decision = self.turn.update(vad_event, now)
        if decision == "turn_end":
            ev = self._flush("utterance_end")
            if ev is not None:
                events.append(ev)
        elif decision == "turn_timeout":
            ev = self._flush("turn_complete")
            if ev is not None:
                events.append(ev)

        return events
