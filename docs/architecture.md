# Vocalis architecture

```
            ┌──────────────────────────── Voice input ─────────────────────────┐
            │                                                                   │
   Mic ────►│  VoiceGate (speaker verification)                                │
            │   ├─ Resemblyzer d-vector embedding                               │
            │   ├─ cosine match vs ~/.vocalis/profiles                          │
            │   └─ REJECT unknown speakers  ──► event: voice.rejected           │
            │                          │ accepted                              │
            │                          ▼                                        │
   Mic ────►│  ASR (faster-whisper, offline) ──► event: asr.final               │
            └──────────────────────────────┬────────────────────────────────────┘
                                           │ text
                                           ▼
                              ┌─────────────────────────┐
                              │   Commander (planner)   │
                              │  fan-out / question /   │
                              │   status query routing  │
                              └───────┬─────────┬───────┘
                      question │              │ orders
                              ▼              ▼
                  ┌───────────────┐   ┌──────────────────────┐
                  │  JarvisBrain  │   │  AgentRegistry       │
                  │ (Ollama local │   │  ├─ echo (demo)      │
                  │  small model, │   │  ├─ claude-code CLI  │
                  │  rule-based   │   │  └─ openai API       │
                  │  fallback)    │   └──────────┬───────────┘
                  └───────┬───────┘              │ progress events
                          │                      ▼
                          │            ┌──────────────────┐
                          │            │  TaskMonitor     │
                          │            │  milestones,     │
                          │            │  watchdog, ETA   │
                          │            └───────┬──────────┘
                          │                    │ completion
                          │                    ▼
                          │            ┌──────────────────┐
                          └───────────►│  Notifier        │
                          narration    │  voice + toast   │
                                      └───────┬──────────┘
                                              │
   HUD (React) ◄── WebSocket /ws ◄── EventBus ◄┘  (all events fan out)
```

## Modules

| Module | Responsibility |
| ------ | -------------- |
| `vocalis/voice/speaker.py` | d-vector embedding (256-dim), resampling |
| `vocalis/voice/gate.py` | enrollment, threshold verification, profile store |
| `vocalis/voice/asr.py` | offline transcription |
| `vocalis/voice/tts.py` | Edge-TTS engine + tunable `VoiceProfile` |
| `vocalis/agents/*` | connector base + implementations |
| `vocalis/jarvis/assistant.py` | local LLM dialogue, narration, status reports |
| `vocalis/jarvis/monitor.py` | milestone narration + watchdog alerts |
| `vocalis/jarvis/commander.py` | intent routing and fan-out dispatch |
| `vocalis/notify/notifier.py` | spoken chime + desktop toast |
| `vocalis/server/*` | FastAPI REST + `/ws` event stream |

## Event bus

Everything is decoupled through `EventBus` (`vocalis/server/events.py`):
publishers emit typed events (`task.progress`, `voice.rejected`, ...),
consumers subscribe with wildcard patterns (`task.*`). The HUD is just
another consumer. Slow consumers drop oldest events instead of blocking.

## Threat model (voice)

- Impostor speech -> rejected by VoiceGate before reaching any LLM.
- Replay attacks -> not yet covered; liveness detection is on the v0.2 roadmap.
- Voiceprints -> local-only storage; see SECURITY.md.
