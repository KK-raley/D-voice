# Competitive Analysis & Improvement Backlog · D-VOICE (Vocalis)

> Written 2026-08-22 after a survey of the GitHub voice-assistant × agent
> ecosystem. This document records (1) where D-VOICE already leads, (2) the
> honest gaps, and (3) the concrete improvements adopted into
> [MAINTENANCE.md](../MAINTENANCE.md) and [ROADMAP.md](../ROADMAP.md).
> Re-survey every quarter (see §5).

---

## 1. Landscape (four project families)

| Family | Representatives | What they do | Relation to D-VOICE |
|---|---|---|---|
| ① Generic speech pipelines | HF `speech-to-speech`, LocalAI | VAD → STT → LLM → TTS loop, OpenAI Realtime protocol | Single-turn "listen–think–speak"; no agent ecosystem, no task lifecycle, no speaker ID |
| ② JARVIS-style assistants | `Project-Jarvis` (ECAPA-TDNN voiceprint, 1 commit), `openjarvis` (Discord, heavy deploy) | Voiceprint gate + local Whisper/TTS + skills | Have speaker ID but never connect to coding agents; most are demo-grade |
| ③ Voice × coding agents (direct rivals) | `convobox` (1042 commits, stream-json/JSON-RPC protocol bridging + voice approval), `codey` (macOS-only, WhisperKit), `agent-speech` (read-only narration) | Voice-driven CLI coding agents | Closest lane — see §2 |
| ④ Voice-agent frameworks | LiveKit Agents, Pipecat, TEN | WebRTC realtime convo, barge-in, turn detection | Infrastructure for phone/support scenarios, not a personal ops layer |

## 2. Head-to-head vs the direct rivals

| Dimension | convobox | codey | agent-speech | **D-VOICE** |
|---|---|---|---|---|
| Voiceprint gate (who may command) | ✗ | ✗ | ✗ | ✅ ERes2Net-large (SOTA) |
| Platforms | Windows (tested) | macOS only | macOS | ✅ cross-platform Python, CPU laptop |
| Local brain + graceful degrade | ✗ | partial | ✗ | ✅ Ollama / OpenAI-compatible, rules fallback |
| Per-task live narration | partial | partial | read-only | ✅ event bus + milestones + watchdog |
| Agent integration style | deep protocols | built-ins | Claude Code only | ✅ 30-line connector abstraction (any CLI/HTTP) |
| Offline-testable every branch | partial | ✗ | ✗ | ✅ echo agent, 26+ tests, CI matrix |
| Chinese scenario | ✗ | ✗ | ✗ | ✅ bilingual Commander + zh voices |

**Positioning sentence**: *Others add voice **to** agents. D-VOICE adds
identity **and** oversight — only your voice commands; every agent narrates.*

The unique selling chain is the supervision triad:
**voiceprint (who can command) → narration (what is happening) → approval
(how far it may go)**. No surveyed rival has the full chain.

## 3. Honest gaps → improvements adopted

| # | Gap vs rivals | Adopted as | Where |
|---|---|---|---|
| G1 | Wake-word / always-on listening (everyone has it; we didn't) | **Shipped v0.2**: `vocalis/voice/wakeword.py` — openWakeWord backend + ASR keyword fallback + cooldown | ROADMAP wake-word item closed |
| G2 | Deep agent protocols (stream-json, JSON-RPC) give structured tool events; our stdout bridge is coarser | MCP connector (v0.3, Track D1) + ACP evaluation | MAINTENANCE Track D |
| G3 | Voice approval gate for dangerous tool calls (convobox has it) | Approval mode: voiceprint + spoken "confirm" for destructive tools | ROADMAP v0.3 |
| G4 | Full-duplex realtime + barge-in (HF/TEN class) | Streaming ASR + interruption (v0.3+); not urgent for ops narration use-case | ROADMAP v0.3 |
| G5 | Voice picker hardcoded 4 voices; no presets | **Shipped v0.2**: `/api/voices` enumeration + locale/gender filter + focus/evening/presentation presets + per-agent voices | MAINTENANCE Track B (B1/B2/B4) |
| G6 | HUD polish (skeletons, a11y, agent identity) | **Shipped v0.2**: loading skeletons, reduced-motion, focus rings, ARIA live regions, per-agent colors/avatars | MAINTENANCE Track A (A1/A2/A4) |
| G7 | No voiceprint FAR/FRR calibration story | `vocalis calibrate` harness (v0.3, Track E2) — no rival has engineering-grade calibration | MAINTENANCE Track E |
| G8 | D-VOICE as MCP *server* (agents call `speak`/`report_progress` tools) | v0.3 flagship: agents actively speak instead of being polled | ROADMAP v0.3 |

## 4. Strategic bets (architecture)

1. **D-VOICE as MCP server** — expose `speak`, `report_progress`,
   `request_confirmation` tools so *agents* initiate narration. Unique lane:
   rivals bridge agent→terminal; we become the agent-ecosystem narrator.
2. **Supervision triad as the product** — voiceprint + narration + approval.
   Security story rivals can't match without re-architecture.
3. **Connector abstraction over protocol depth** — accept coarser events from
   any agent today, add deep protocol adapters incrementally (Claude Code
   stream-json first, then Codex app-server).
4. **CPU-first** — every model (ERes2Net, Whisper small, Qwen 0.5–1.5B,
   openWakeWord) runs on a plain laptop; rivals mostly assume GPUs/macOS.

## 5. Re-survey cadence

Quarterly: re-run the survey (search terms: voice agent, MCP voice,
realtime voice assistant, speaker verification agent), update §1/§2 tables,
move closed gaps to "shipped", add new rivals. Keep the positioning
sentence honest — if a rival ships the full supervision triad, revise it.
