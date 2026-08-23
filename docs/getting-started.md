# Getting started

## 1. Install

```bash
# Core (agents, dvoice, server, CLI)
pip install -e .

# Full voice stack (mic, speaker verification, ASR, TTS)
pip install -e ".[all]"
```

Requirements: Python 3.10+, a microphone for enrollment, and (optional)
[Ollama](https://ollama.com) for the local D-VOICE brain.

## 2. Give D-VOICE a brain (optional but recommended)

```bash
ollama pull qwen2.5:3b-instruct
```

Without Ollama, Vocalis falls back to rule-based dialogue - status reports
and dispatch still work.

## 3. Enroll your voice

```bash
vocalis enroll --user you
# speak 3 x ~3s utterances when prompted
```

Test it:

```bash
vocalis gate --file some_recording.wav
# => ACCEPTED user=you sim=0.93
```

## 4. First task

```bash
# Zero-config demo agent with live narration
vocalis run "generate a release summary"

# Voice-gated dispatch (mic verification first)
vocalis run "run the test suite" --agent claude-code --verify

# Ask the local brain
vocalis ask "当前所有子系统的状态？"
```

## 5. Launch the HUD

```bash
vocalis serve                # backend on :8642
cd ui && npm install && npm run dev   # HUD on :5173
```

Open http://localhost:5173 - you get the live waveform, agent statuses,
task progress bars, the D-VOICE console, and the Voice Studio to tune
rate / pitch / volume / timbre with instant previews.

## Troubleshooting

| Symptom | Fix |
| ------- | --- |
| `resemblyzer` fails to install | Use Python 3.10-3.12; on Windows a C++ Build Tools install may be needed for webrtcvad |
| No audio output on Windows | `TTSService.play_file` falls back to `winsound`; ensure default playback device set |
| D-VOICE replies "(dvoice offline)" | Start Ollama (`ollama serve`) or keep `brain.fallback_to_rules = true` |
| Edge-TTS times out | Requires internet; a local Piper backend is on the roadmap |
