# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- **Rebrand**: the assistant brain is now **D-VOICE** (`vocalis.dvoice`,
  replacing `vocalis.jarvis`); HUD console, event names (`dvoice.saying`,
  `dvoice.command`) and docs updated accordingly.

### Planned
- Streaming ASR with partial transcription (ETA v0.3)
- Wake-word detection for always-on listening
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

[Unreleased]: https://github.com/vocalis-ai/vocalis/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/vocalis-ai/vocalis/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/vocalis-ai/vocalis/releases/tag/v0.1.0
