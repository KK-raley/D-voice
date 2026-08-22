# Realtime Voice Interaction (chunking / VAD / turn detection / barge-in)

`vocalis/voice/realtime.py` implements the four mechanisms D-VOICE needs to
converse like a human instead of a walkie-talkie. This document explains the
architecture, the tunable parameters, the research it borrows from, and how it
compares to the reference systems surveyed in August 2026.

## Architecture

```
                       30 ms frames (mic_frames generator)
                                     |
                                     v
                    +--------------------------------+
                    |  EnergyVAD (frame state machine)|
                    |  idle --speech_start--> speaking|
                    |  confirm=2 frames, hangover=10  |
                    +--------------------------------+
                       | VadEvent (start/speech/stop/silence)
                       |              |                       |
                       v              v                       v
            +----------------+  +----------------+  +--------------------+
            | TurnDetector   |  | BargeIn        |  | utterance buffer   |
            | pause timing + |  | Controller     |  | (+ pre-roll, trim, |
            | max_turn cap   |  | >=2 consec.    |  |  max_buffer cap)   |
            +----------------+  | voiced frames  |  +--------------------+
              |      |          +----------------+          |
       turn_end   turn_timeout        | barge_in             | audio + start/end
              |      |                |                      v
              v      v                v            +-------------------+
        utterance_end turn_complete  barge_in -->  | ASR -> Commander  |
        (natural)     (forced cut)   (stop TTS)    | -> TTS -> player  |
                                                   +-------------------+
```

`vocalis talk` runs this as one loop: mic frames stream into
`RealtimeSession.feed_frame()`; completed utterances go to faster-whisper,
`Commander.execute()`, and Edge-TTS; during playback the same loop keeps
feeding the mic, so talking over D-VOICE cancels the reply within ~2 frames
(60 ms) - the "full duplex" property TEN Framework advertises.

## The four mechanisms

### 1. EnergyVAD - frame-level voice activity detection

- **Confirm before start** (`speech_confirm_frames=2`, i.e. 60 ms): a single
  loud frame (click, cough) never opens a speech run. Borrowed from
  HuggingFace speech-to-speech's `min_speech_ms` (384 ms there; energy VAD
  needs less because Silero-grade robustness is not the goal here).
- **Hangover after speech** (`hangover_frames=10`, i.e. 300 ms): silent
  frames after speech keep the run open so intra-word pauses ("I'm ...
  thinking") do not shred an utterance. Same idea as HF's
  `short_segment_merge_ms` and classic telephony hangover.
- **Adaptive noise floor** (`adaptive=True`): threshold = rolling
  `median + 3 x 1.4826 x MAD` over a 3 s window (`noise_window=100` frames),
  neutral during the first 20 warmup frames. Default is a fixed
  `energy_floor=0.01` RMS - deterministic and right for quiet rooms.

Tuning: quiet office -> fixed threshold; kitchen/street noise or laptop fans
-> `--adaptive`. If D-VOICE answers while you are still thinking, raise
`energy_floor` (or enable adaptive); if it never hears you, lower it.

### 2. TurnDetector - "has the user finished?"

Pure state machine driven by injected timestamps (no wall clock - that is
what makes it unit-testable):

| Parameter | Default | Meaning |
|---|---|---|
| `min_pause_s` | 0.8 s | Pause shorter than this is a *thinking gap*: keep listening. This is the single most "humanlike" knob - humans hold the floor for 200-300 ms gaps; production systems endpoint around 600-800 ms. |
| `max_turn_s` | 30 s | Force-cut rambling/stuck-open turns (`turn_timeout`). Periodic: a 3 s monologue with `max_turn_s=1.0` yields three `turn_complete` chunks. |
| `max_pause_s` | 2.5 s | Hard ceiling on the wait even if `min_pause_s` is deliberately raised (elderly/slow speakers ~2.5 s per production guidance). |

Key behavior: a pause below `min_pause_s` **never** ends the turn - if the
speaker resumes, the same turn simply continues. HuggingFace speech-to-speech
implements the same idea as `speculative_reopen_ms=800` (a soft-ended turn
reopens when speech resumes); we get it by not ending early at all.

TEN Framework replaces this timing heuristic with a fine-tuned Qwen2.5-7B
classifier (finished / unfinished / wait, ~90-99 % accuracy vs ~60-75 % for
silence-only heuristics). `TurnDetector.update()` deliberately returns the
same three-way outcome (`listening` / `turn_end` / `turn_timeout`) so a
semantic judge can later slot in behind the same interface.

### 3. BargeInController - interrupting D-VOICE

While TTS plays (`session.bot_speaking = True`), the mic keeps streaming.
Two consecutive voiced frames (~60 ms) confirm the user is talking over the
assistant -> `barge_in` event -> `InterruptiblePlayer.stop()`. One frame (a
cough) does not trigger. HF speech-to-speech gates its hard cancel the same
way (`min_speech_ms`); production writeups budget 100-150 ms from speech to
cancellation, which 2 x 30 ms frames comfortably meet.

### 4. RealtimeSession - chunking state machine

- Voiced frames accumulate into an utterance buffer; a small **pre-roll**
  (8 frames) recovers the confirmation frames clipped off the start.
- On turn end the buffer is **trimmed** of trailing silence (2 pad frames
  kept) and emitted with full audio + start/end timestamps - the natural
  hook for per-turn speaker verification (3D-Speaker / CAM++ streaming
  embeddings, VoiceGate) or diarization downstream.
- `max_buffer_s=60` forces a flush so memory can never grow unbounded.
- Micro-utterances (<0.3 s) are dropped as noise bursts.

### InterruptiblePlayer (vocalis/voice/tts.py)

Threaded playback with `stop()`: Windows `winsound` plays synchronously in a
worker thread and is cancelled via `PlaySound(None, SND_PURGE)`; Unix spawns
`mpv`/`ffplay`/`afplay` via `Popen` and `terminate()`s it (polled at 20 ms).
The legacy `TTSService.play_file` is untouched for backward compatibility.

## Comparison with surveyed systems

| Aspect | HF speech-to-speech | TEN Framework | D-VOICE (this module) |
|---|---|---|---|
| VAD | Silero VAD v5 (DNN, 32 ms windows) | TEN VAD (streaming, ~50 ms, low CPU) | EnergyVAD (numpy RMS + state machine) |
| Turn detection | Smart Turn v3.2 semantic endpointing + speculative reopen (800 ms) | LLM classifier (Qwen2.5-7B): finished/unfinished/wait | Timing state machine, same 3-way interface; semantic judge pluggable |
| Barge-in | Confirmed speech -> hard cancel + flush; reversible pre-ducking under discussion (issue #433) | Full-duplex turn detection during agent speech | >=2 consecutive voiced frames -> stop TTS (~60 ms) |
| Transport | OpenAI-Realtime-compatible WebSocket | Agora RTC / WebSocket / SIP | Local generator (30 ms frames) - no network needed |
| Stack weight | torch + silero + STT/LLM/TTS servers | Docker + 4 cloud API keys | numpy-only core; whisper/edge-tts lazy-imported in the CLI |
| Speaker ID | none built-in | diarization example | per-utterance audio+timestamps hook for VoiceGate / 3D-Speaker CAM++ |

## Research sources

- HuggingFace speech-to-speech: <https://github.com/huggingface/speech-to-speech>
  - Smart Turn v3.2 endpointing PR #192 (`min_silence_ms`, `min_speech_ms`,
    `short_segment_merge_ms`, `speculative_reopen_ms`)
  - Barge-in latency / audio ducking discussion: issue #433, PR #434
- TEN Framework: <https://github.com/TEN-framework/ten-framework>
  - Turn detection deep dive: <https://theten.ai/cn/blog/voice-assistant-with-ten-turn-detection>
    and <https://www.shengwang.cn/blog/blogdetail/turn-detection-deep-dive/>
- 3D-Speaker (Alibaba DAMO): <https://github.com/modelscope/3D-Speaker>
  - CAM++ speaker verification model:
    <https://www.modelscope.cn/models/damo/speech_campplus_sv_zh-cn_16k-common>
    (paper: arXiv:2303.00332; toolkit paper: arXiv:2403.19971)
- Silero VAD architecture background (32 ms windows, stateful inference):
  snakers4/silero-vad
- Production endpointing guidance (600 ms min / 1.5 s max / 2.5 s slow
  speakers; 200-300 ms human turn gaps): AssemblyAI voice-agent docs and
  2026 production voice-agent writeups.

## Quick start

```
vocalis talk                       # defaults: 0.8 s pause, 30 s max turn
vocalis talk --min-pause 1.2       # slower speakers
vocalis talk --adaptive            # noisy environment
vocalis talk --profile aria -p     # pick a reply voice
```
