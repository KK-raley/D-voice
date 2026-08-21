<div align="center">

<img src="ui/public/favicon.svg" width="96" alt="Vocalis logo" />

# V O C A L I S

**Your voice. Your agents. Your JARVIS.**

A voice-first, local-first agent ecosystem that **only obeys you**, speaks agent
output aloud in a voice you tune yourself, and narrates what your AI workforce
is doing — in real time.

[![CI](https://img.shields.io/github/actions/workflow/status/vocalis-ai/vocalis/ci.yml?branch=main&logo=github&label=CI)](https://github.com/vocalis-ai/vocalis/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/vocalis-voice-agent?color=22d3ee&logo=pypi)](https://pypi.org/project/vocalis-voice-agent/)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776ab?logo=python&logoColor=white)](https://pypi.org/project/vocalis-voice-agent/)
[![License: MIT](https://img.shields.io/badge/license-MIT-818cf8.svg)](LICENSE)
[![Code style: ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Discussions](https://img.shields.io/github/discussions/vocalis-ai/vocalis?color=34d399&logo=github)](https://github.com/vocalis-ai/vocalis/discussions)
[![Maintenance](https://img.shields.io/maintenance/yes/2027?color=34d399)](https://github.com/vocalis-ai/vocalis/graphs/commit-activity)

[Quick start](#-quick-start) · [Architecture](#-architecture) · [How it works](#-how-it-works) · [Roadmap](ROADMAP.md) · [Contributing](CONTRIBUTING.md)

> 🌟 **Star this repo** to follow the ride — wake-word listening, voice cloning and
> multi-agent swarm control are landing next (see the [roadmap](ROADMAP.md)).

</div>

---

## Why Vocalis?

Voice input for LLMs is everywhere — but so is a problem nobody talks about:
**your microphone obeys anyone**. A colleague, a TV commercial, a family member —
anyone who speaks near your machine can inject instructions into your agent.

Vocalis fixes that, and then goes further: it's the missing **voice operating
layer** for the agentic era.

| | Capability | How |
|---|---|---|
| 🔐 | **Speaker authentication (VoiceGate)** | Resemblyzer d-vector embeddings + cosine threshold. Enrolled impostors get rejected *before* any LLM sees their words. Voiceprints never leave your machine. |
| 🗣️ | **Unified speech output** | Every connected agent's text output is spoken through one TTS layer (Edge-TTS neural voices, keyless & free). |
| 🎛️ | **Voice your way** | Tunable `VoiceProfile`s — voice, rate, pitch, volume — adjustable live from the HUD's Voice Studio or CLI. Evening mode? Slower, warmer, quieter. |
| 📡 | **Real-time status narration** | A local small model (Ollama) turns raw agent events into calm JARVIS-style spoken reports: *"claude-code is at 60% — refactoring the parser."* |
| 🧠 | **Ask-anything console** | Interrupt anytime with questions; the local brain answers with live system context, fully offline. |
| 🤖 | **Command your agents** | Natural-language dispatch with fan-out: *"让 echo 跑个演示，然后 claude-code 重构测试"* → parallel dispatch + progress bars. |
| 🔔 | **Done = you know** | Completion, failure and stall detection trigger a spoken chime + desktop toast. |

And when no local model is available, a rule-based fallback keeps the whole
loop alive. **The ecosystem never goes mute.**

---

## ✨ Demo moments

```
You (mic)   : "JARVIS，现在什么情况？"
VoiceGate   : ACCEPTED user=li  (sim 0.94)
JARVIS (TTS): "两个任务正在执行。claude-code 正在重构解析器，进度 60%。
               echo 已完成数据摘要。"
You (mic)   : "让 claude-code 把测试也修一下"
JARVIS (TTS): "已派发。预计 3 分钟。完成后我会提醒您。"
...
JARVIS (TTS): ♪ 任务完成。claude-code 已修复全部 14 个测试，耗时 2 分 41 秒。
```

> Full walkthrough scripts live in [`examples/`](examples) — from enrollment
> to the complete JARVIS loop.

---

## 🚀 Quick start

```bash
# 1 · install (voice extras included)
pip install "vocalis-voice-agent[all]"

# 2 · optional but recommended: give JARVIS a local brain
ollama pull qwen2.5:3b-instruct

# 3 · enroll YOUR voice (3 short takes)
vocalis enroll --user you

# 4 · talk to your agents
vocalis run "summarize this repo"                 # demo agent, zero config
vocalis run "fix the flaky tests" --agent claude-code --verify
vocalis ask "当前状态？"                            # ask the local brain

# 5 · launch the JARVIS HUD
vocalis serve                        # backend :8642
cd ui && npm i && npm run dev        # HUD     :5173
```

<details>
<summary><b>Try it in 60 seconds without a microphone</b></summary>

```bash
pip install -e ".[dev]"
pytest -q                                # offline test suite
python examples/04_full_jarvis.py        # full loop, no mic needed
```

The `echo` agent simulates a realistic workflow with live progress events,
so you can watch the HUD, narration and notifications work end-to-end.

</details>

<details>
<summary><b>Connect real agents</b></summary>

| Agent | Setup |
| ----- | ----- |
| **claude-code** | Install the Claude Code CLI and sign in — Vocalis bridges it via subprocess with watchdog timeouts. |
| **openai** (or any OpenAI-compatible API) | `export OPENAI_API_KEY=...` (+ optional `OPENAI_BASE_URL`, `OPENAI_MODEL`). |
| **your own** | Subclass `AgentConnector`, implement `stream_run()` yielding progress — ~30 lines. See [`vocalis/agents/echo.py`](vocalis/agents/echo.py). |

</details>

---

## 🏗️ Architecture

```
 Mic ──► VoiceGate ──(reject impostors)──► ASR ──► Commander ──► Agents
              │                                    │       ▲
              │                              questions│      │ progress
              │                                    ▼      │
              │                              JarvisBrain  TaskMonitor
              │                              (local LLM)  (milestones+watchdog)
              └────────────► EventBus ◄──────────┴──────────┘
                              │
        ┌─────────────────────┼──────────────────┐
        ▼                     ▼                  ▼
   TTS narration       HUD (React/WS)      Notifications
   (VoiceProfile)      live ops feed       chime + toast
```

Everything is decoupled through an asyncio **event bus**: publishers emit
typed events (`voice.rejected`, `task.progress`, `monitor.alert`, ...), and the
HUD, monitor, narrator and notifier are all just subscribers. Deep dive:
[`docs/architecture.md`](docs/architecture.md).

## 📦 Project layout

```
vocalis/
├── vocalis/               # Python package
│   ├── voice/             #   VoiceGate · ASR · TTS · audio IO
│   ├── agents/            #   connector base + echo / claude-code / openai
│   ├── jarvis/            #   brain · task monitor · commander
│   ├── notify/            #   spoken + toast notifications
│   ├── server/            #   FastAPI · WebSocket event stream
│   └── cli.py             #   `vocalis` command
├── ui/                    # React HUD (Vite · TS · zero UI deps)
├── examples/              # 01-enroll → 04-full-jarvis
├── tests/                 # offline pytest suite (3·OS × 3·py matrix in CI)
└── docs/                  # architecture · getting-started
```

## ⚙️ Configuration

Everything lives in `~/.vocalis/config.toml` (auto-created):

```toml
[voice_gate]
threshold = 0.80          # stricter = raise me

[brain]
model = "qwen2.5:3b-instruct"
fallback_to_rules = true  # never go mute

[tts]
default_profile = "aria"

[monitor]
watchdog_timeout_s = 300  # stall alerts
```

Secrets (`OPENAI_API_KEY`, ...) are always environment variables — never stored.

## 🗺️ Roadmap

| Version | Theme | Highlights |
| ------- | ----- | ---------- |
| **v0.2** 🚧 | Always Listening | wake-word, streaming ASR, multi-user households, liveness checks |
| **v0.3** | Any Voice | XTTS-v2 voice cloning, emotion control, auto-summarize before speech |
| **v0.4** | Swarm Control | parallel fan-out board, proactive interruptions, MCP connectors |
| **v1.0** | Production | Tauri desktop shell, encrypted voiceprint vault, FAR/FRR tuning |

Full detail in [ROADMAP.md](ROADMAP.md) · history in [CHANGELOG.md](CHANGELOG.md).

## 🤝 Contributing

PRs are welcome — connector integrations, TTS backends, docs and bug fixes
especially. Read [CONTRIBUTING.md](CONTRIBUTING.md), then:

```bash
pip install -e ".[all,dev]"
pytest -q && ruff check vocalis tests examples
```

**House rules:** voiceprints and recordings are never committed; tests must
pass offline; keep new dependencies justified.

## 🛡️ Security & privacy

- Voiceprints = biometric data → stored **only** under `~/.vocalis/`, git-ignored.
- No telemetry. No cloud. Brain runs on your machine via Ollama.
- Report vulnerabilities privately — see [SECURITY.md](SECURITY.md).

## 📄 License

[MIT](LICENSE) © Vocalis Contributors

---

<div align="center">

**"Sometimes you gotta run before you can walk."** — build your JARVIS today.

Made with 🎙️ + 🧠 · [Discussions](https://github.com/vocalis-ai/vocalis/discussions) · [Issues](https://github.com/vocalis-ai/vocalis/issues)

</div>
