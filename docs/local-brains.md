# Local Brains & CLI Agents — CPU-friendly guide

D-VOICE's brain (conversation, status narration) is intentionally decoupled
from any single model server. Two properties matter on a plain CPU laptop:

1. **No GPU required** — 0.5B–3B q4-quantized models run fine on CPU.
2. **No vendor lock-in** — anything speaking Ollama or the OpenAI
   chat-completions protocol works.

## 1. Brain backends

### Option A: Ollama (easiest)

```toml
# ~/.vocalis/config.toml
[brain]
backend = "ollama"                    # default
host = "http://localhost:11434"
model = "qwen2.5:1.5b-instruct"
```

```bash
ollama pull qwen2.5:1.5b-instruct    # ~1 GB, runs on any laptop
```

### Option B: llama.cpp-server (Gemma / any GGUF)

```bash
# gemma-3-1b q4 fits in ~800 MB RAM, CPU-only
llama-server -m gemma-3-1b-it-Q4_K_M.gguf --port 8080
```

```toml
[brain]
backend = "openai-compatible"
base_url = "http://localhost:8080/v1"
model = "gemma-3-1b-it"
```

### Option C: LM Studio / vLLM / remote APIs

Same `openai-compatible` config — set `base_url` (LM Studio defaults to
`http://localhost:1234/v1`) and export your key if the server needs one:

```bash
export DVOICE_API_KEY="sk-..."
```

### CPU-friendly model matrix (all tested class: 1-3B q4)

| Model | Size | RAM | Get it via |
|---|---|---|---|
| Qwen2.5 0.5B / 1.5B instruct | 0.4–1 GB | 1–2 GB | `ollama pull qwen2.5:0.5b` |
| Gemma 3 1B it | ~0.8 GB | ~1.5 GB | GGUF → llama.cpp |
| Llama 3.2 1B instruct | ~0.8 GB | ~1.5 GB | `ollama pull llama3.2:1b` |
| Qwen2.5 3B (best quality) | ~2 GB | 3–4 GB | `ollama pull qwen2.5:3b-instruct` |

No model server at all? D-VOICE degrades to deterministic rule-based
replies (status reports still work) — it never goes mute.

## 2. Bridge any CLI agent (codex, opencode, aider, ...)

Any terminal agent that accepts an instruction and streams output can be
driven by voice. Declare it in `~/.vocalis/config.toml`:

```toml
[[cli_agents]]
name = "codex"
command = ["codex", "exec", "{instruction}"]
timeout_s = 1800

[[cli_agents]]
name = "opencode"
command = ["opencode", "run", "{instruction}"]
```

`{instruction}` receives your spoken words verbatim; omit the placeholder to
append the instruction as the last argument. Then:

```text
You   : "让 codex 修复登录 bug，然后让 opencode 跑一遍测试"
D-VOICE: "已并行派发两个任务。codex 正在工作…"
You   : "现在进度怎么样？"
D-VOICE: "2 个任务进行中。codex 正在改 auth.py，opencode 已跑完 60% 测试。"
```

Status queries work for *any* registered agent (built-in or CLI) because
progress lives in the shared `TaskMonitor`, not inside the agent — see
[`MAINTENANCE.md`](../MAINTENANCE.md) Track D for the roadmap (MCP port,
health panel, cancellation).

## 3. Speaker verification backend

```toml
[voice_gate]
backend = "eres2net-large"   # SOTA (default), needs:
# pip install "vocalis-voice-agent[voiceprint]"
# backend = "resemblyzer"   # light fallback, zero extra setup
# threshold = 0.55          # override per-backend default if needed
```

Switching backends requires re-running `vocalis enroll` (embeddings from
different models are not comparable; profiles are stored per backend).
