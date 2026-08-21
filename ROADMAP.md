# Roadmap

Vocalis is under active development. This document tracks what is shipping next.

## Status Legend

| Icon | Meaning |
| ---- | ------- |
| ✅ | Shipped |
| 🚧 | In progress |
| 📋 | Planned |

---

## v0.2 — "Always Listening" (Q4 2026)

- [ ] 🚧 Wake-word detection (openWakeWord / Porcupine) — hands-free activation
- [ ] 🚧 Streaming ASR with partial hypotheses for real-time subtitles
- [ ] 📋 Multi-user household mode: per-user voice profiles + personalized replies
- [ ] 📋 Roll-call authentication: liveness check (randomized prompt replay)

## v0.3 — "Any Voice" (Q1 2027)

- [ ] 📋 Voice cloning backend (XTTS-v2 / OpenVoice) for custom personas
- [ ] 📋 Emotion & style control in TTS output (calm / excited / concise)
- [ ] 📋 SSML generation for agent long-form responses
- [ ] 📋 Auto-summarization: long agent output condensed before speech

## v0.4 — "Swarm Control" (Q2 2027)

- [ ] 📋 Multi-agent orchestration: parallel task fan-out with voice status board
- [ ] 📋 Proactive interruption: D-VOICE can politely break in on anomalies
- [ ] 📋 Priority queues for concurrent agent tasks
- [ ] 📋 MCP (Model Context Protocol) connector standard

## v1.0 — "Production" (H2 2027)

- [ ] 📋 Cross-platform desktop shell (Tauri) with tray + global hotkey
- [ ] 📋 Encrypted local vault for voiceprints (biometric data protection)
- [ ] 📋 FAR / FRR tuned verification with ROC-calibrated thresholds
- [ ] 📋 Localization: 中文, English, 日本語, Español
- [ ] 📋 Plugin marketplace for community agent connectors

---

## Community Requests

Want to influence the roadmap? Open a
[discussion](https://github.com/KK-raley/D-voice/discussions) or vote on
existing ones — top-voted items get pulled into the next milestone.

## Maintenance Commitment

- Security patches for the latest release: **guaranteed**
- Issue triage SLA: **< 72 hours**
- Dependency refresh: **monthly**
