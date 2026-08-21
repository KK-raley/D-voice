# D-VOICE Showcase · 看得见的使用效果

> 本页用**场景 + 视频**展示 D-VOICE 的真实使用效果。每个场景都给出可复现
> 的命令、录屏/动图、以及"看什么"的观察点。
>
> 媒体文件统一放在 [`docs/media/`](docs/media)，命名规则见
> [§7 媒体资产管理](#7-媒体资产管理)。链接失效 = bug，欢迎提 issue。

---

## 目录

1. [60 秒总览](#1-60-秒总览)
2. [场景一：声纹门禁](#2-场景一声纹门禁只有你能指挥)
3. [场景二：语音指挥 + 实时播报](#3-场景二语音指挥--实时播报)
4. [场景三：Voice Studio 调音](#4-场景三voice-studio-实时调音)
5. [场景四：多 agent 并行 + 完成提醒](#5-场景四多-agent-并行--完成提醒)
6. [场景五：无本地模型也不哑](#6-场景五无本地模型也不哑)
7. [媒体资产管理](#7-媒体资产管理)
8. [录屏工具链](#8-录屏工具链)
9. [主演示视频制作规划](#9-主演示视频制作规划)

---

## 1. 60 秒总览

<p align="center">
  <img src="docs/media/overview.gif" alt="D-VOICE 60-second overview"
       width="840" />
</p>

一个循环：**你说话 → VoiceGate 验证声纹 → ASR 转文字 → Commander 派发 →
agent 执行 → TaskMonitor 里程碑播报 → 完成提醒（语音 + 桌面通知）**。

复现：

```bash
vocalis serve &            # 后端 :8642
cd ui && npm run dev       # HUD  :5173
# 对 HUD 说："D-VOICE，现在什么情况？"
```

**看什么**：左上波形随你的声音起伏；右栏 Live Operations Feed 依次滚出
`voice.accepted → asr.final → dvoice.saying → tts.speaking` 四类事件。

---

## 2. 场景一：声纹门禁（只有你能指挥）

| 媒体 | 内容 |
|---|---|
| `docs/media/voicegate-accept.mp4` | 已注册用户说话 → `ACCEPTED (sim 0.94)` → 指令执行 |
| `docs/media/voicegate-reject.mp4` | 未注册同事说话 → `REJECTED (sim 0.41)` → 指令**不**进入任何 LLM |

<p align="center"><img src="docs/media/voicegate.gif" width="840" alt="VoiceGate accept vs reject" /></p>

复现：

```bash
vocalis enroll --user you     # 3 次短朗读完成注册
vocalis gate                  # 实时验证模式：说一句看判定
vocalis run "summarize this repo" --verify   # 带声纹验证的任务
```

**看什么**：Feed 里的 `voice.rejected` 事件 —— 被拒绝的语音**从未到达**
ASR 与 agent，这是与"普通话音输入"的本质区别。

---

## 3. 场景二：语音指挥 + 实时播报

<p align="center">
  <video src="docs/media/dispatch-narration.mp4" controls
         width="840"></video>
</p>

台词（真实播报录音）：

```text
你      : "让 claude-code 重构解析器，echo 顺便跑个演示。"
D-VOICE : "已派发两项任务。claude-code 预计三分钟，echo 十秒内完成。"
D-VOICE : "claude-code 进度 50%，正在改 parser.py。"        ← 里程碑自动播报
D-VOICE : ♪ "任务完成。claude-code 通过 14 个测试，耗时 2 分 41 秒。"
```

复现：`vocalis run "fix the flaky tests" --agent claude-code --verify`

**看什么**：播报不是读日志 —— 本地小模型把 `task.progress` 事件
改写成**人话**（含耗时、测试数），且在 25/50/75% 里程碑才打断你，
不是每条事件都念。

---

## 4. 场景三：Voice Studio 实时调音

<p align="center"><img src="docs/media/voice-studio.gif" width="840" alt="Voice Studio" /></p>

复现：HUD → **Voice Studio** 面板 → 拖动 rate / pitch / volume → 点
`Preview` 即刻试听 → `Save as Profile` 持久化到 `~/.vocalis/config.toml`。

**看什么**：语速 ±50%、音调 ±20Hz 拖动后**无需重启**，下一次播报立即生效；
"evening" 预设 = 慢 10% + 温和音色 + 音量 +10%（见 [MAINTENANCE Track B](MAINTENANCE.md)）。

---

## 5. 场景四：多 agent 并行 + 完成提醒

<p align="center"><img src="docs/media/fanout.gif" width="840" alt="Multi-agent fan-out" /></p>

复现：

```bash
vocalis run "让 echo 做第一件事；然后 claude-code 修测试"   # 一句话 fan-out
```

**看什么**：HUD 任务卡并行推进两个进度条；任一 agent 完成时右下角
桌面通知 + 语音提示音同时出现（`notify_on = completed/failed/stalled`）。

---

## 6. 场景五：无本地模型也不哑

<p align="center"><img src="docs/media/degraded.gif" width="840" alt="Rule-based fallback" /></p>

复现：`ollama stop`（或干脆不装 Ollama）→ 问"当前状态？" →
仍得到规则引擎的状态汇报。

**看什么**：Feed 出现 `monitor.alert: D-VOICE local model unavailable`
—— 降级**可见**而非静默；这正是 [MAINTENANCE 原则 1](MAINTENANCE.md#1-maintenance-principles)
"Never go mute" 的现场证明。

---

## 7. 媒体资产管理

| 规则 | 约定 |
|---|---|
| 目录 | 全部入仓 `docs/media/`（与文档同生命周期，杜绝外链腐坏） |
| 命名 | `<场景>-<内容>.<ext>`，小写-连字符：`voicegate-reject.mp4` |
| 格式优先级 | ① `.gif`（自动播放，PR/README 内联首选，≤10MB）② `.mp4`（GitHub 原生 `<video>` 播放，≤50MB）③ `asciinema` 嵌入（终端场景） |
| 分辨率 | 统一 1280×720 @15fps（gif 降到 12fps 控体积） |
| 隐私 | 录屏**不得**包含：真实声纹文件路径内容、API key、他人声音；演示语音用项目合成音 |
| 失效处理 | 每次发版核对本页所有媒体可加载；失效即提 `docs` issue（模板已含 Docs/CI 复选框） |

GitHub 中嵌入视频的两种写法（本页使用第二种，仓库内文件不走外链）：

```html
<!-- ① 拖拽上传到 Issue 后复制 URL（适合一次性演示） -->
<video src="https://user-images.githubusercontent.com/…/demo.mp4" controls></video>

<!-- ② 入仓文件直接引用（可持续维护，本页采用） -->
<video src="docs/media/dispatch-narration.mp4" controls></video>
```

## 8. 录屏工具链

| 用途 | 工具 | 要点 |
|---|---|---|
| HUD/桌面录屏 → mp4 | OBS Studio | 场源 1280×720；关掉鼠标点击音 |
| mp4 → gif | [ScreenToGif](https://github.com/NickeManarin/ScreenToGif) / `ffmpeg -i in.mp4 -vf "fps=12,scale=1280:-1" out.gif` | gif >10MB 就降 fps 或裁时长 |
| 终端会话 | [asciinema](https://asciinema.org) + `agg` 转 gif | `vocalis gate` 这类纯终端场景首选，体积最小 |
| 声音分离 | OBS 双轨：系统声（TTS 输出）/ 麦克风 | 后期可只保留 TTS 轨保护隐私 |
| 字幕 | `ffmpeg -vf subtitles=demo.srt` | 播报词即字幕稿，直接用场景里的台词块 |

> **贡献演示**：录好一个场景 → 媒体按上表命名入仓 → 在本页加一节
> （复现命令 + 媒体 + 看什么三件套）→ PR 标题带 `showcase`。

---

## 9. 主演示视频制作规划

> 目标：一支 **60–90 秒**、放在 README 顶部的主演示视频。
> 原则：**真实运行、零剪辑特效**——产品本身（波形、播报、通知）就是特效。

### 9.1 分镜脚本（90 秒版）

| # | 时间 | 画面 | 声音 | 字幕 |
|---|---|---|---|---|
| 1 | 0–5s | 黑屏 → HUD 淡入，波形待命 | 提示音 ♪ | "如果你的 AI 助手，只听你一个人的话？" |
| 2 | 5–15s | **声纹拒绝**：同事（或变声）说指令 → Feed 弹 `voice.rejected` 红色事件 | 播报："未授权语音，已拦截。" | "陌生人说话 —— 拦截" |
| 3 | 15–20s | **声纹通过**：同一指令你再读一遍 → `voice.accepted` 绿色事件，任务开始执行 | （事件提示音） | "你的声音 —— 通过" |
| 4 | 20–40s | **语音指挥 + 播报**：说"让 echo 跑个演示，claude-code 修一下测试" → 双任务卡并行推进进度条 | D-VOICE 播报："已派发两项…claude-code 进度 50%…" | 播报词即字幕 |
| 5 | 40–55s | **Voice Studio**：拖动 rate/pitch → `Preview` 试听 → 切换 "evening" 预设 | 同一句话的三种声音对比 | "输出音色，随你调" |
| 6 | 55–70s | **完成提醒**：右下角桌面通知弹出 + 提示音；Feed 滚出 `task.completed` | "♪ 任务完成，14 个测试全部通过，耗时 2 分 41 秒。" | "完成 = 你知道" |
| 7 | 70–80s | 拉远：完整 HUD 全景，六个面板各自活跃 | BGM 渐入 | "本地运行 · 声纹门禁 · 实时播报" |
| 8 | 80–90s | Logo + GitHub 地址 `github.com/KK-raley/D-voice` | BGM 收尾 | "D-VOICE — Your voice. Your agents." |

节奏要点：前 5 秒必须出现"被拒绝"的反常画面（钩子）；每 10–15 秒一个新画面事件，无超过 3 秒的静止。

### 9.2 录制前准备清单（Pre-flight）

- [ ] `vocalis enroll --user you` 完成，阈值调稳（连测 5 次通过）
- [ ] 找一位同事（或用手机外放变声）做"未注册声纹"素材
- [ ] echo agent 演示指令预先跑通一遍，确认里程碑播报词顺口
- [ ] OBS 场源布局：HUD 浏览器窗口 1280×720；隐藏书签栏；深色主题
- [ ] **双音轨**：轨道1 = 系统声（TTS 输出），轨道2 = 麦克风（你的指令）——后期可独立调平
- [ ] 桌面清空：无关窗口、通知（微信/邮件）全部关闭或勿扰模式
- [ ] 练习台词 3 遍（指令要说得短、稳、自然）

### 9.3 制作流程（一天内完成）

```
上午 · 录制（每镜头单独文件，宁多勿缺）
  R01 钩子空镜            R05 Voice Studio 操作 ×3 遍
  R02 声纹拒绝 ×3 遍      R06 完成通知特写
  R03 声纹通过            R07 HUD 全景慢摇（屏录软件全景截图滚动）
  R04 语音指挥长镜头 ×2   R08 备用：降级场景（可选彩蛋）

下午 · 剪辑
  粗剪（按分镜表排序、去废镜头）→ 精剪（卡节奏，画面事件对齐音轨）
  → 字幕（直接用播报词，勿加解释性长句）→ BGM（-20dB 垫底，播报时闪避）
  → 导出：1080p/30fps/H.264，≤50MB
```

剪辑工具任选：剪映（字幕快）/ DaVinci Resolve（免费专业）/ CapCut。

### 9.4 发布 checklist

- [ ] `docs/media/demo-main.mp4` 入仓（≤50MB），README 顶部 `<video>` 嵌入
- [ ] 同时传一份到 B 站/YouTube（外站备用链接写进 SHOWCASE §1）
- [ ] GitHub Release v0.2 附该视频（release 附件不受 50MB 仓库限制）
- [ ] 封面帧：选"声纹拒绝"红色事件那一帧（最有记忆点），存 `docs/media/cover.png` 供社交分享

