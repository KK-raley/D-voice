# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
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
- **Competitive analysis** (`docs/competitive-analysis.md`): survey of the
  voice-assistant × agent ecosystem driving the improvement backlog.
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

### Changed
- `--dim` HUD text color raised to meet WCAG AA 4.5:1 contrast.
- `vocalis listen` degrades to rules-based guidance instead of tracebacks
  when sounddevice/faster-whisper/openwakeword are missing.
- **Rebrand**: the assistant brain is now **D-VOICE** (`vocalis.dvoice`,
  replacing `vocalis.jarvis`); HUD console, event names (`dvoice.saying`,
  `dvoice.command`) and docs updated accordingly.
- Default brain model lowered to `qwen2.5:1.5b-instruct` (CPU-friendly).

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
