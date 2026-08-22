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

The unique selling chain is the supervision dyad:
**voiceprint (who can command) → narration (what is happening)**. Approvals
are deliberately *not* part of the voice loop — see the rejected gap G3
below. No surveyed rival has even this dyad.

## 3. Honest gaps → improvements adopted

| # | Gap vs rivals | Adopted as | Where |
|---|---|---|---|
| G1 | Wake-word / always-on listening (everyone has it; we didn't) | **Shipped v0.2**: `vocalis/voice/wakeword.py` — openWakeWord backend + ASR keyword fallback + cooldown | ROADMAP wake-word item closed |
| G2 | Deep agent protocols (stream-json, JSON-RPC) give structured tool events; our stdout bridge is coarser | **Partly shipped (2026-08-22)**: MCP server (`python -m vocalis.server.mcp`, 4 tools) gives agents a structured bidirectional port; deep stream-json/JSON-RPC adapters still v0.3 (Track D1) | MAINTENANCE Track D |
| G3 | Voice approval gate for dangerous tool calls (convobox has it) | **Rejected by design (2026-08-22)**: spoken authorization is too casual for destructive actions (file deletion, "what next" decisions). Approvals stay in each agent's native UI (terminal/IDE confirmation); D-VOICE narrates the request but never grants it. If an approval surface is ever added it will be explicit (button/key), never voice-only | — (deliberate non-feature) |
| G4 | Full-duplex realtime + barge-in (HF/TEN class) | **Shipped (2026-08-22)**: `vocalis/voice/realtime.py` — EnergyVAD (confirm frames + hangover + adaptive noise floor), TurnDetector (3-state, TEN-compatible interface for future semantic judge), BargeInController, RealtimeSession chunking + `vocalis talk` full-duplex command; HF speech-to-speech parameter philosophy adopted | docs/realtime.md |
| G5 | Voice picker hardcoded 4 voices; no presets | **Shipped v0.2**: `/api/voices` enumeration + locale/gender filter + focus/evening/presentation presets + per-agent voices | MAINTENANCE Track B (B1/B2/B4) |
| G6 | HUD polish (skeletons, a11y, agent identity) | **Shipped v0.2**: loading skeletons, reduced-motion, focus rings, ARIA live regions, per-agent colors/avatars | MAINTENANCE Track A (A1/A2/A4) |
| G7 | No voiceprint FAR/FRR calibration story | **Shipped (2026-08-22)**: `vocalis calibrate --self-dir ... --impostor-dir ...` — threshold sweep with FAR/FRR/EER per candidate, weighted recommendation and human-readable summary; each audio verified once, similarity reused across thresholds (`vocalis/voice/calibrate.py`) | MAINTENANCE Track E2 closed |
| G8 | D-VOICE as MCP *server* (agents call `speak`/`report_progress` tools) | **Shipped (2026-08-22)**: `vocalis/server/mcp.py` — 4 tools (speak / report_progress / get_status / dispatch_task), stdio transport, mcp 1.x & 2.x SDK compatible, no authorization tools by design (G3) | docs/mcp.md |

## 4. Strategic bets (architecture)

1. **D-VOICE as MCP server** — expose `speak`, `report_progress`, `get_status`
   tools so *agents* initiate narration. Unique lane: rivals bridge
   agent→terminal; we become the agent-ecosystem narrator. (No approval /
   confirmation tool by design — see G3.)
2. **Supervision dyad as the product** — voiceprint + narration. Security
   story rivals can't match without re-architecture. Approvals are
   intentionally out of the voice loop (spoken consent is too weak for
   destructive operations).
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
