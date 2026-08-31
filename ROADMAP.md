# Roadmap

## 当前里程碑：0.2.0rc1（2026-08-31，候选版，尚未发布）

本轮方向：持续在线的本地语音管家。下方历史章节日期为旧规划；
实际交付以本节和 CHANGELOG 为准，“已实现”不表示已通过真人/生产验收。

| 迭代 | 状态 | 交付与验收门槛 |
|---|---|---|
| 0.1.x | 历史基础 | 独立声纹/唤醒模块、任务事件、HUD、语音输出 |
| 0.2.0rc1 | 已实现，待用户验收 | 休眠零模型调用；本人声纹与唤醒词片段绑定；逐轮验证；超时休眠；网页/CLI 统一入口；本地 Qwen3；Windows SAPI 启动器 |
| 0.2.0 | 未发布 | 完成真人注册、陌生人/录音攻击测试、噪声环境唤醒评估和 8 小时连续运行 |
| 0.3 | 计划 | 安全全双工打断、流式字幕、轻量自定义唤醒模型 |
| 0.4 / 1.0 | 计划 | 更完整的 agent 控制、活体认证、多用户隔离、加密声纹存储 |

### 本轮已完成

- [x] 启动休眠 → 本人唤醒确认 → 逐句验证 → 30 秒闲置/口令休眠。
- [x] 唤醒句不执行附带指令；另一位已注册用户不可接管会话。
- [x] 词级时间戳对齐唤醒片段，重叠声纹窗口检查，异常时拒绝。
- [x] 有界缓冲；超长语音/溢出后丢弃尾段直到安静；处理与播报期间丢弃收音。
- [x] 本地 Qwen3-4B 接入：显式启动、模型身份探测、无云密钥/代理/自动云端降级。
- [x] HUD 持续待机开关、状态及错误；敏感历史鉴权；顺序语音播放。
- [x] 独立技术与产品回归；结果与限制见 [验收记录](docs/acceptance-0.2.md)，优先级改进见 [产品复盘](docs/product-review-0.2.md)。

### 正式版验收门槛

- [ ] 本人录音注册、真实唤醒率、逐轮验证误拒绝率及延迟。
- [ ] 陌生人、电视、本人录音、声音克隆、距离与噪声的误接收评估。
- [ ] 连续 8 小时：CPU/内存/队列上界、设备拔插、系统休眠恢复。
- [ ] 冷启动缺依赖/缺模型体验、麦克风设备选择与权限指引。
- [ ] 多客户端、执行中任务取消和资源隔离进一步强化。

当前半双工不是最终全双工能力；声纹无活体保证。严格离线需使用缓存语音模型、
本地 SAPI，并关闭外部 agent 和屏幕监管，不能仅凭本地 Qwen 宣称整套产品离线。

---

## 历史分阶段规划

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
