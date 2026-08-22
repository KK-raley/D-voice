# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Hook contract & extensibility** (Track C, G-survey W2 pulled forward):
  - [docs/hooks.md](docs/hooks.md): the documented event-hook contract —
    every `EventType` with payload schema, publisher, `bus.on()` recipes and
    third-party integration guide (C1).
  - **Entry-point plugins** (`vocalis/agents/plugins.py`): third parties
    register connectors via the `vocalis.agents` entry-point group without
    forking; per-plugin failure isolation, duplicate-name protection (C2).
  - **TTS text hooks** (`TTSService` pre/post hooks): text normalization
    before synthesis (e.g. `2m41s` → `两分四十一秒`) and post-synthesis
    callbacks; hook exceptions never break synthesis (C4).
- **Agent resilience** (Track D, W3 pulled forward):
  - **Task cancellation** (D4): `TaskStatus.CANCELLED` + `CancelledError`
    handling in `AgentConnector.run` (event sequence, health untouched),
    cancelled tasks enter registry history; `GenericCLIAgent` terminates
    its subprocess (terminate → grace → kill) on cancellation.
  - **Retries + circuit breaker** (D3, `vocalis/agents/resilience.py`):
    `retry_async` exponential backoff and `CircuitBreaker`
    (closed/open/half-open, injectable clock) opt-in via connector class
    attributes — defaults keep every existing connector unchanged.
  - **Connector health** (D2): `ConnectorHealth` per connector
    (last_error / last_latency_ms / consecutive_failures …) carried on
    `agent.status` events and `GET /api/agents`.
- **Voiceprint FAR/FRR calibration harness** (G7 / Track E2,
  `vocalis calibrate --self-dir ... --impostor-dir ...`): sweeps candidate
  gate thresholds, reports FAR/FRR/EER per threshold with a weighted
  recommendation and a human-readable summary; verifies each audio once
  and reuses the similarity across thresholds.
- **Event history replay on HUD connect** (Track F3): the WebSocket `/ws`
  endpoint replays `bus.history` (oldest→newest, `"replayed": true`
  marker) before live forwarding, `?replay=N` caps it, `?replay=0`
  disables — HUD sees pre-connect events without UI changes.

### Changed
- Monitor no longer narrates cancelled tasks as failures: `task.failed`
  events with `status="cancelled"` are silently finalized (user-initiated
  stop ≠ connector failure).
- **Realtime human-like interaction** (`vocalis/voice/realtime.py`, informed
  by the HF speech-to-speech & TEN Framework survey — see
  [docs/realtime.md](docs/realtime.md)):
  - `EnergyVAD`: frame-level voice activity detection with speech-confirmation
    frames, hangover padding (short pauses don't fragment speech) and an
    adaptive noise-floor mode (`median + k·MAD`).
  - `TurnDetector`: 3-state turn end detection (listening / turn_end /
    turn_timeout) — brief pauses (< 0.8 s default) no longer cut the user
    off; interface matches TEN's semantic-judge shape for a future drop-in.
  - `BargeInController`: interrupt D-VOICE mid-sentence — 2 consecutive
    voiced frames while TTS is speaking stop playback immediately.
  - `RealtimeSession`: streaming chunking state machine that emits complete
    utterances (with pre-roll and trailing-silence trim) for downstream
    ASR/LLM processing — the "feed audio to the LLM periodically" core.
  - New `vocalis talk` command: full-duplex conversation loop (mic stream →
    VAD → turn detection → ASR → Commander → interruptible TTS playback).
  - `InterruptiblePlayer` in TTS: thread-based playback with cross-platform
    `stop()` (winsound purge / player process terminate).
- **D-VOICE as MCP server** (`python -m vocalis.server.mcp`, new `mcp` pip
  extra): agents connect *to* D-VOICE over stdio with 4 tools — `speak`,
  `report_progress` (agents proactively narrate their own progress),
  `get_status`, `dispatch_task`. Compatible with both mcp SDK 1.x and 2.x.
  See [docs/mcp.md](docs/mcp.md).
- **Wake-word detection ("Always Listening")**: new `vocalis listen` command
  with two interchangeable backends — openWakeWord (small ONNX models, new
  `wakeword` pip extra) and an ASR keyword fallback (zero extra deps) —
  bilingual phrases ("hey D-VOICE" / "你好 D-VOICE"), NFKC/punctuation-tolerant
  matching, per-detection cooldown, and graceful dependency-missing guidance.
- **Voice catalog API** (`GET /api/voices?locale=&gender=`): enumerates
  Edge-TTS voices with locale-prefix and gender filters, 5-minute server
  cache, offline fallback list; HUD voice picker now browses the full
  catalog instead of four hardcoded voices.
- **One-click voice presets** (`GET/POST /api/voice/presets`): focus /
  evening / presentation scenario bundles — one tap re-tunes rate, pitch,
  volume and voice, persisted as normal profiles.
- **Per-agent voices** (`tts.agent_voices`): narration picks a distinct
  voice per agent (claude-code answers in Orion, echo in Aria) so parallel
  tasks are distinguishable by ear.
- **HUD accessibility & polish**: loading skeletons for feed/agents/tasks,
  empty-state illustrations, `prefers-reduced-motion` support, keyboard
  focus rings and ARIA live regions, per-agent color identity & avatars
  (stable hash colors for unknown agents).
- **ERes2Net-large speaker verification** (SOTA, 3D-Speaker/ModelScope): new
  default `voice_gate.backend = "eres2net-large"` via the `voiceprint` extra;
  profiles are namespaced per backend (`{user}.{backend}.voiceprofile.json`)
  with per-backend calibrated thresholds. Resemblyzer remains as a light
  fallback and is auto-selected when the `voiceprint` extra is absent.
- **Brain multi-backend**: `brain.backend = "ollama" | "openai-compatible"`
  — D-VOICE can now ride llama.cpp-server, LM Studio, vLLM or any
  OpenAI-compatible endpoint (Gemma, Qwen, Llama 3.2 ... on CPU laptops).
- **Generic CLI agent connector** (`cli_agents` in config.toml): bridge
  codex / opencode / aider / any terminal agent by voice; instructions are
  dispatched as subprocesses with watchdog + streamed output narration.
- **Competitive analysis** (`docs/competitive-analysis.md`): survey of the
  voice-assistant × agent ecosystem driving the improvement backlog.

### Changed
- `--dim` HUD text color raised to meet WCAG AA 4.5:1 contrast.
- `vocalis listen` degrades to rules-based guidance instead of tracebacks
  when sounddevice/faster-whisper/openwakeword are missing.
- **Rebrand**: the assistant brain is now **D-VOICE** (`vocalis.dvoice`,
  replacing `vocalis.jarvis`); HUD console, event names (`dvoice.saying`,
  `dvoice.command`) and docs updated accordingly.
- Default brain model lowered to `qwen2.5:1.5b-instruct` (CPU-friendly).

### Removed
- **Voice-approval concept dropped by design** (competitive-analysis G3):
  spoken authorization is too casual for destructive agent actions (file
  deletions, "what next" decisions). Approvals stay in each agent's native
  UI; D-VOICE narrates requests but never grants them. The supervision
  story is now the **dyad** (voiceprint → narration); no
  request_confirmation/approve tool is or will be exposed via MCP.

### Fixed
- Rich markup injection in CLI output (bracketed text no longer eaten by
  console styles).
- Slider labels now properly associated with inputs (`htmlFor`/`id`);
  profile cards keyboard-operable (Enter/Space).

### Planned
- Streaming ASR with partial transcription (ETA v0.3)
- Voice cloning via XTTS-v2 backend
- Plugin system for third-party agent connectors

## [0.1.1] - 2026-08-21

### Fixed
- Windows toast notification fallback when `win10toast` is unavailable
- VoiceGate now correctly normalizes 48 kHz input to 16 kHz before embedding
- WebSocket reconnection logic in the HUD frontend

### Changed
- Upgraded default local brain model to `qwen2.5:3b-instruct`
- Reduced first-load latency of the speaker encoder by caching weights

## [0.1.0] - 2026-08-15

### Added
- **VoiceGate**: speaker verification powered by Resemblyzer d-vector embeddings
  with per-user enrollment, cosine-similarity threshold, and anti-spoof rejection
  of unknown voices.
- **Unified TTS layer**: Edge-TTS engine with adjustable `VoiceProfile`
  (voice, rate, pitch, volume) and hot-swappable engine backends.
- **ASR**: offline transcription via faster-whisper.
- **Agent connectors**: pluggable registry with built-in `echo` (local demo),
  `claude-code` (CLI bridge), and `openai` (API) connectors.
- **Jarvis core**: local LLM brain (Ollama) for real-time dialogue, task
  monitoring, status reporting, and agent commanding; graceful rule-based
  fallback when no local model is present.
- **TaskMonitor**: event-driven progress tracking with watchdog timeouts and
  completion notifications (voice chime + system toast).
- **FastAPI server** with WebSocket event stream for the HUD.
- **React HUD**: JARVIS-style dark interface with live waveform, agent feed,
  voice-profile studio, and chat panel.
- CLI (`vocalis enroll|gate|speak|run|ask|serve|agents|status`), examples,
  tests, CI pipeline, and full documentation.

[Unreleased]: https://github.com/KK-raley/D-voice/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/KK-raley/D-voice/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/KK-raley/D-voice/releases/tag/v0.1.0
