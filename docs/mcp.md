# D-VOICE as an MCP server

D-VOICE can run as a [Model Context Protocol](https://modelcontextprotocol.io)
server, exposing its narration pipeline to any MCP-capable coding agent —
Claude Code, Codex, opencode, Cursor, ...

This flips the classic monitoring pattern on its head: instead of the user
passively polling a dashboard while agents work silently, the **agent
proactively speaks up**. When a long build finishes, when tests go red, when
input is needed — the agent calls `report_progress` or `speak` and D-VOICE
narrates it aloud through the user's speakers. This is strategic bet G8 from
the [competitive analysis](competitive-analysis.md): *the agent that speaks
first wins* — rivals bridge agent→terminal, D-VOICE becomes the agent
ecosystem's narrator.

## Tools

| Tool | Parameters | Purpose |
|---|---|---|
| `speak` | `text` (str), `profile` (str, optional), `agent` (str, optional) | 让 D-VOICE 立即开口说话 — synthesize + play text aloud. Voice resolution: explicit `profile` > per-agent mapping > default. Returns `ok / audio_path / profile / characters`. Playback failure on headless machines degrades to synthesis-only (audio file still returned). |
| `report_progress` | `task_id` (str), `agent` (str), `progress` (float 0..1), `step` (str, optional), `note` (str, optional) | 汇报任务进度 — publish a `task.progress` event into the narration pipeline. Progress is clamped to [0, 1]. Call at meaningful milestones, not every step. |
| `get_status` | — | 查询系统状态 — registered agents, active/recent tasks, default voice profile, available profiles, local brain availability. Good once per session. |
| `dispatch_task` | `agent` (str), `instruction` (str) | 派发任务 — fire-and-forget dispatch to a registered agent connector (e.g. `echo`). Returns a `task_id` immediately; lifecycle events flow through the bus. Unknown agents get the available-agent list back. |

All tools publish onto the same event bus as the HTTP/WebSocket server
(`dvoice.saying`, `task.progress`, `task.queued`, ...), so the HUD, monitor,
and notifier all keep working when an MCP agent narrates.

## Install

```bash
pip install 'vocalis-voice-agent[mcp]'
```

That adds the official `mcp` SDK (both the 1.x `FastMCP` and the 2.x
`MCPServer` API are supported — the module picks whichever is installed).
Without the SDK, `vocalis.server.mcp` still imports fine; only server
construction reports a clear install hint.

## Connect an agent

### Claude Code

```bash
claude mcp add dvoice -- python -m vocalis.server.mcp
```

Then just ask Claude Code to keep you posted out loud, e.g. *"run the test
suite and report progress through dvoice"*.

### Generic MCP client (stdio)

```json
{
  "mcpServers": {
    "dvoice": {
      "command": "python",
      "args": ["-m", "vocalis.server.mcp"],
      "env": {
        "VOCALIS_HOME": "C:/Users/you/.vocalis"
      }
    }
  }
}
```

Notes:

- `command` may need the full interpreter path if the agent spawns a clean
  shell (e.g. `D:/anaconda3/python.exe` or wherever vocalis is installed).
- The server speaks stdio and logs to stderr only — stdout is the protocol
  channel and is never polluted.
- `VOCALIS_HOME` (optional) isolates config/profiles/audio cache; unset it
  to share the same `~/.vocalis` as the CLI and HUD.

## Design decision: no authorization tools

This server deliberately exposes **no** `request_confirmation` / `approve`
tools. Spoken authorization ("say yes to continue") is far too casual a
channel for security-relevant decisions — easy to mishear, easy to spoof,
and it leaves no audit trail. Authorization stays in the agent's native UI
(terminal prompts, permission dialogs); D-VOICE is a narration layer, never
an approval authority. See [competitive analysis](competitive-analysis.md)
G3/G8 for the full rationale.

## Example session

The user is cooking dinner while a refactor runs:

> **User**: 重构 auth 模块并跑全量测试，有进展就告诉我。
>
> **Agent**: （调用 `dispatch_task` 派发任务，开始工作）
>
> **Agent**: 调用 `report_progress(task_id="a1b2", agent="claude-code", progress=0.4, step="重构完成，开始跑测试")`
>
> **D-VOICE** (扬声器): *"重构完成，开始跑全量测试。"*
>
> **Agent**: ... 调用 `report_progress(task_id="a1b2", agent="claude-code", progress=1.0, step="测试全绿")`
>
> **D-VOICE**: *"全部 214 个测试通过，重构完成，零失败。"*
>
> **User**（没看屏幕）: 好，帮我合并。

Milestones are spoken in the agent's mapped voice (`claude-code` → `orion`
by default), so parallel agents are distinguishable by ear. Tune the mapping
in `~/.vocalis/config.toml` under `[tts] agent_voices`.

## Development

```bash
# offline unit tests (mcp SDK optional - those cases skip without it)
python -m pytest tests/test_mcp.py -q

# interactive protocol debugging
npx @modelcontextprotocol/inspector python -m vocalis.server.mcp
```

Implementation lives in
[`vocalis/server/mcp.py`](../vocalis/server/mcp.py):
`DVoiceMCPContext` assembles config/bus/registry/TTS/brain headlessly (no
FastAPI, no monitor loop — nothing to poll when agents push), and
`build_mcp_server` registers the four tools with bilingual docstrings
written for the agent's LLM.
