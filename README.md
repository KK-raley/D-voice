<div align="center">

<img src="ui/public/favicon.svg" width="96" alt="Vocalis logo" />

# V O C A L I S

**Your voice. Your agents. Your D-VOICE.**

A voice-first, local-first agent ecosystem that **only obeys you**, speaks agent
output aloud in a voice you tune yourself, and narrates what your AI workforce
is doing — in real time.

[![CI](https://img.shields.io/github/actions/workflow/status/KK-raley/D-voice/ci.yml?branch=main&logo=github&label=CI)](https://github.com/KK-raley/D-voice/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776ab?logo=python&logoColor=white)](https://github.com/KK-raley/D-voice)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)](LICENSE)
[![Code style: ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Discussions](https://img.shields.io/github/discussions/KK-raley/D-voice?color=34d399&logo=github)](https://github.com/KK-raley/D-voice/discussions)
[![Maintenance](https://img.shields.io/maintenance/yes/2027?color=34d399)](https://github.com/KK-raley/D-voice/graphs/commit-activity)

[Quick start](#quick-start) · [Architecture](#architecture) · [MCP for agents](#mcp-server-for-coding-agents) · [How it works](#how-it-works) · [Local brains & CLI agents](docs/local-brains.md) · [Showcase](SHOWCASE.md) · [Roadmap](ROADMAP.md) · [Maintenance](MAINTENANCE.md) · [Contributing](CONTRIBUTING.md)

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
| 🔐 | **Speaker authentication (VoiceGate)** | ERes2Net-large embeddings (3D-Speaker SOTA, optional `voiceprint` extra; Resemblyzer fallback) + cosine threshold. Enrolled impostors get rejected *before* any LLM sees their words. Voiceprints never leave your machine. |
| 🗣️ | **Unified speech output** | Every connected agent's text is spoken through one TTS layer (Edge-TTS neural voices, keyless & free). |
| 🎛️ | **Voice your way** | Tunable `VoiceProfile`s — voice, rate, pitch, volume — adjustable live from the HUD's Voice Studio. Evening mode? Slower, warmer, quieter. |
| 📡 | **Real-time status narration** | A local small model (Ollama / llama.cpp / LM Studio — any OpenAI-compatible server) turns raw agent events into calm spoken reports. |
| 🔗 | **MCP server for agents** | Agents connect *to* D-VOICE over the Model Context Protocol (stdio) and call `speak` / `report_progress` / `get_status` / `dispatch_task`. Agents speak up proactively instead of the user polling a dashboard. |
| 🧩 | **Pluggable ecosystem** | Third-party agents register via Python entry-points (`vocalis.agents` group) — no forking needed. Documented event hook contract ([docs/hooks.md](docs/hooks.md)). |
| ⚡ | **Dual-stream orchestration** | Voice + tool run as parallel streams on the event bus: sentence-level streaming TTS (real-time) and an async tool worker pool that never blocks speech. A natural *pause* (soft hold — finish what's said, then wait at a sentence boundary) is deliberately different from a *barge-in* (hard interrupt). Speech holds while tools work, like a real conversation. |
| 🎭 | **Pluggable TTS backends** | `TTSRouter` capability negotiation — Edge-TTS (fixed presets, zero resources) ↔ IndexTTS-2.5 sidecar (voice cloning, offline, high expression). No GPU? Automatic fallback to Edge-TTS via voice mapping. |
| 🧠 | **Ask-anything console** | Interrupt anytime with questions; the local brain answers with live system context, fully offline. |
| 🤖 | **Command your agents** | Natural-language dispatch with fan-out: *"让 echo 跑个演示，然后 claude-code 重构测试"* → parallel dispatch + progress bars. |
| 🛡️ | **Agent resilience** | Opt-in retry + circuit breaker. Task cancellation propagates to subprocesses. Connector health tracked (latency, error rate, failures). |
| 🔔 | **Done = you know** | Completion, failure and stall detection trigger a spoken chime + desktop toast. |
| 📊 | **Voiceprint calibration** | `vocalis calibrate` — sweep gate thresholds, FAR/FRR/EER per candidate, weighted recommendation. |

And when no local model is available, a rule-based fallback keeps the whole
loop alive. **The ecosystem never goes mute.**

---

## Demo moments

```
You (mic)   : "D-VOICE，现在什么情况？"
VoiceGate   : ACCEPTED user=li  (sim 0.94)
D-VOICE(TTS): "两个任务正在执行。claude-code 正在重构解析器，进度 60%。
               echo 已完成数据摘要。"
You (mic)   : "让 claude-code 把测试也修一下"
D-VOICE(TTS): "已派发。预计 3 分钟。完成后我会提醒您。"
...
D-VOICE(TTS): 任务完成。claude-code 已修复全部 14 个测试，耗时 2 分 41 秒。
```

> Full walkthrough scripts live in [`examples/`](examples) — from enrollment
> to the complete D-VOICE loop.

---

## Quick start

```bash
# 1 · install (voice extras included)
pip install "vocalis-voice-agent[all]"

# 2 · optional but recommended: give D-VOICE a local brain
ollama pull qwen2.5:1.5b-instruct    # or see docs/local-brains.md for
                                     # llama.cpp / LM Studio / Gemma on CPU

# 3 · enroll YOUR voice (3 short takes)
vocalis enroll --user you

# 4 · talk to your agents
vocalis run "summarize this repo"                 # demo agent, zero config
vocalis run "fix the flaky tests" --agent claude-code --verify
vocalis ask "当前状态？"                            # ask the local brain

# 5 · launch the D-VOICE HUD
vocalis serve                        # backend :8642
cd ui && npm i && npm run dev        # HUD     :5173
```

<details>
<summary><b>Try it in 60 seconds without a microphone</b></summary>

```bash
pip install -e ".[dev]"
pytest -q                                # offline test suite
python examples/04_full_dvoice.py          # full loop, no mic needed
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

## Architecture

```
 Mic --> VoiceGate --(reject impostors)--> ASR --> Commander --> Agents
              |                                    |       ^
              |                              questions|      | progress
              |                                    v      |
              |                              DVoiceBrain  TaskMonitor
              |                              (local LLM)  (milestones+watchdog)
              +------------> EventBus <----------+----------+
                              |
        +---------------------+------------------+
        v                     v                  v
   TTS narration       HUD (React/WS)      Notifications
   (VoiceProfile)      live ops feed       chime + toast
```

Everything is decoupled through an asyncio **event bus**: publishers emit
typed events (`voice.rejected`, `task.progress`, `monitor.alert`, ...), and the
HUD, monitor, narrator and notifier are all just subscribers. Deep dive:
[`docs/architecture.md`](docs/architecture.md).

---

## MCP server for coding agents

D-VOICE doubles as a **Model Context Protocol (MCP) server**, so your coding
agents (Claude Code, Codex, opencode, aider, Cursor, ...) can connect and
narrate their work aloud.

### How it works

Instead of you polling a dashboard, the agent calls D-VOICE directly:

```
Agent (Claude Code)  --stdio-->  D-VOICE MCP server  --TTS-->  You hear it
```

Four tools are exposed:

| Tool | What it does | When to call |
|---|---|---|
| `speak` | Say text aloud right now | Milestone reached, result ready, user attention needed |
| `report_progress` | Report a progress update | Task at 30%, 60%, 90% — narrated as step description |
| `get_status` | Snapshot of agents, tasks, voices | Session start, or after dispatching |
| `dispatch_task` | Fire-and-forget task assignment | Delegate work to another agent |

### Run it

```bash
# Install with MCP support
pip install 'vocalis-voice-agent[mcp]'

# Start the MCP server (stdio transport)
python -m vocalis.server.mcp
```

### Glama / MCP client deployment

```bash
docker build -t vocalis-mcp -f Dockerfile.mcp .
docker run -i --rm vocalis-mcp
```

The MCP server is headless (no web UI, no audio playback needed) — it
synthesizes speech and returns the path to the audio file. Configuration
is via environment variables (`OPENAI_API_KEY`, `VOCALIS_HOME`, etc.).

> **Design decision**: No authorization tools are exposed via MCP. Spoken
> consent is too casual for destructive operations. Approvals stay in the
> agent's native UI (terminal/IDE confirmation). D-VOICE narrates the
> request but never grants it.

---

## Project layout

```
vocalis/
+-- vocalis/               # Python package
|   +-- voice/             #   VoiceGate . ASR . TTS . audio IO
|   +-- agents/            #   connector base + echo / claude-code / openai
|   +-- dvoice/            #   brain . task monitor . commander
|   +-- notify/            #   spoken + toast notifications
|   +-- server/            #   FastAPI . MCP . WebSocket event stream
|   +-- cli.py             #   `vocalis` command
+-- ui/                    # React HUD (Vite . TS . zero UI deps)
+-- examples/              # 01-enroll to 04-full-dvoice
+-- tests/                 # offline pytest suite (3 OS x 3 py matrix in CI)
+-- docs/                  # architecture . getting-started . hooks . mcp
```

---

## Configuration

Everything lives in `~/.vocalis/config.toml` (auto-created):

```toml
[voice_gate]
backend = "eres2net-large"  # SOTA (pip install ...[voiceprint]); or "resemblyzer"
# threshold = 0.55          # override the per-backend default if needed

[brain]
backend = "ollama"          # or "openai-compatible" (llama.cpp/LM Studio/vLLM)
model = "qwen2.5:1.5b-instruct"
fallback_to_rules = true  # never go mute

[[cli_agents]]             # bridge any terminal agent by voice
name = "codex"
command = ["codex", "exec", "{instruction}"]

[tts]
default_profile = "aria"

[monitor]
watchdog_timeout_s = 300  # stall alerts
```

Secrets (`OPENAI_API_KEY`, ...) are always environment variables — never stored.

---

## Roadmap

| Version | Theme | Highlights |
| ------- | ----- | ---------- |
| **v0.2** | Always Listening | wake-word, streaming ASR, realtime dialogue, MCP server, hook contract, agent resilience, voiceprint calibration |
| **v0.3** | Any Voice | IndexTTS-2.5 voice-cloning sidecar + pluggable TTS backends, dual-stream (voice+tool) orchestration, deep protocol adapters |
| **v0.4** | Swarm Control | parallel fan-out board, proactive interruptions, tri-stream full-duplex model |
| **v1.0** | Production | Tauri desktop shell, encrypted voiceprint vault, multi-user households |

Full detail in [ROADMAP.md](ROADMAP.md) . history in [CHANGELOG.md](CHANGELOG.md).

---

## Contributing

PRs are welcome — connector integrations, TTS backends, docs and bug fixes
especially. Read [CONTRIBUTING.md](CONTRIBUTING.md), then:

```bash
pip install -e ".[all,dev]"
pytest -q && ruff check vocalis tests examples
```

**House rules:** voiceprints and recordings are never committed; tests must
pass offline; keep new dependencies justified.

---

## Security & privacy

- Voiceprints = biometric data -> stored **only** under `~/.vocalis/`, git-ignored.
- No telemetry. No cloud. Brain runs on your machine via Ollama.
- Report vulnerabilities privately — see [SECURITY.md](SECURITY.md).

---

## License

[GNU AGPL-3.0](LICENSE) (c) Vocalis Contributors

---

<div align="center">

**"Sometimes you gotta run before you can walk."** — build your D-VOICE today.

Made with microphone + brain . [Discussions](https://github.com/KK-raley/D-voice/discussions) . [Issues](https://github.com/KK-raley/D-voice/issues)

</div>