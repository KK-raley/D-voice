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

- [x] ✅ Wake-word detection — `vocalis listen` with openWakeWord backend
      (pip extra `wakeword`) + ASR keyword fallback, cooldown, bilingual
      phrases ("hey D-VOICE" / "你好 D-VOICE")
- [x] ✅ Voice picker & presets — `/api/voices` catalog (locale/gender
      filter), focus/evening/presentation one-click presets, per-agent
      voices (hear which agent speaks)
- [x] ✅ HUD accessibility & polish — loading skeletons, reduced-motion,
      focus rings, ARIA live regions, per-agent color identity
- [x] ✅ Realtime human-like interaction (pulled from v0.3, G4) —
      EnergyVAD + TurnDetector (short pauses don't cut you off) +
      BargeInController (interrupt D-VOICE mid-sentence) + streaming
      chunking via RealtimeSession; `vocalis talk` full-duplex command
      (see [docs/realtime.md](docs/realtime.md))
- [x] ✅ D-VOICE as MCP server (pulled from v0.3, G8) — agents connect
      *to* D-VOICE: `speak` / `report_progress` / `get_status` /
      `dispatch_task` tools over stdio; no voice-approval tools by design
      (see [docs/mcp.md](docs/mcp.md))
- [ ] 🚧 Streaming ASR with partial hypotheses for real-time subtitles
- [ ] 📋 Multi-user household mode: per-user voice profiles + personalized replies
- [ ] 📋 Roll-call authentication: liveness check (randomized prompt replay)

## v0.3 — "Any Voice" (Q1 2027)

- [ ] 🚧 IndexTTS-2.5 voice-cloning backend (sidecar) + pluggable TTS router —
      Edge-TTS (preset) ↔ IndexTTS (clone), GPU-optional with graceful
      fallback, implemented on `dul-stream`
- [ ] 🚧 Dual-stream orchestration (voice + tool, no text stream) — real-time
      sentence-level streaming TTS + async tool worker pool; natural pause
      (soft) ≠ barge-in (hard), implemented on `dul-stream`
- [ ] 📋 Emotion & style control in TTS output (calm / excited / concise)
- [ ] 📋 SSML generation for agent long-form responses
- [ ] 📋 Auto-summarization: long agent output condensed before speech

## v0.4 — "Swarm Control" (Q2 2027)

- [ ] 📋 Multi-agent orchestration: parallel task fan-out with voice status board
- [ ] 📋 Proactive interruption: D-VOICE can politely break in on anomalies
- [ ] 📋 Priority queues for concurrent agent tasks
- [ ] 📋 Deep agent protocol adapters (Claude Code stream-json, Codex
      JSON-RPC) on top of the shipped MCP port

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
