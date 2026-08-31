<div align="center">
<img src="ui/public/favicon.svg" width="80" alt="D-VOICE" />

# D-VOICE / Vocalis

**持续在线的本地语音管家：只在本人唤醒后处理指令。**

当前开发版本 **0.2.0rc1** · 本地 Qwen3 · 声纹与唤醒词绑定 · 休眠零大模型调用

[快速开始](#快速开始) · [版本路线](ROADMAP.md) · [变更记录](CHANGELOG.md) · [验收记录](docs/acceptance-0.2.md) · [产品复盘](docs/product-review-0.2.md) · [本地模型](docs/local-brains.md)
</div>

## 产品工作方式

启动后，D-VOICE 保持本地语音检测。只有已登记声音说出配置的唤醒词，系统才进入短时对话状态。

1. 本人说 **“你好 D-VOICE”**（也支持 `hey d-voice` / `d voice`）。
2. 等待 **“我在，请说。”**，然后单独说指令。唤醒那句话中的附带指令不会执行。
3. 每条后续语音重新核验声纹；另一位已登记用户也不能接管当前会话。
4. 说 **“休眠”**，或闲置约 30 秒，回到本地待机。再次说唤醒词可继续。
5. 退出程序或在 HUD 点击 **“关闭麦克风”**，停止收音；此时不能靠声音唤醒。

```text
本机麦克风 → 有界缓冲 / 本地 VAD → 声纹验证 → 本地 ASR
                                             │
                     休眠：检查唤醒词对应音频的声纹
                     活跃：确认仍是唤醒者，再处理指令
                                             ↓
                               Commander → 本地 Qwen / 显式配置的 agent
                                             ↓
                                  本地语音输出 + HUD
```

**休眠零 token 的含义**：休眠音频不会派发到 Commander、Qwen 或 agent，不进入大脑对话历史，拒绝/未唤醒的转写不会返回网页。仍需要本地收音、声纹推理和 ASR，因此不等于零 CPU、零内存或麦克风完全关闭。这里指零**大模型** token；本地 ASR 仍做识别计算。

当前安全管家采用**半双工**：处理指令和播报期间丢弃收音，避免回声及过期音频成为指令。音频有长度限制，超长语音和队列溢出会休眠，并丢弃尾段直到安静。全双工自然打断仍在路线图中。

## 快速开始

需要 Python 3.10+；Windows 本地语音使用 SAPI/pywin32。首次安装需联网准备依赖和语音识别模型，运行可使用已缓存模型。请在本项目目录操作。

```powershell
python -m pip install -e ".[voice,dev]"

# 只读检查现有本地 Qwen，不启动模型、不执行推理
.\start-local.ps1 -Mode check

# 第一次使用：录入本人声纹，共三段；不启动 Qwen
.\start-local.ps1 -Mode enroll -User you

# 启动/复用本地 Qwen，进入持续待机
.\start-local.ps1 -Mode listen

# 或仅显示回复文字，不播放声音
.\start-local.ps1 -Mode listen -TextOnly
```

启动器默认使用现有部署：

- 模型：`D:\qwen-deployment\models\qwen3-4b-q4_k_m.gguf`
- 运行时：`D:\qwen-deployment\runtime\llama-server.exe`
- 本机地址：`http://127.0.0.1:8080/v1`
- 项目状态：`.vocalis/config.toml`、`.vocalis/profiles/`、`.vocalis/logs/`

启动器不会修改模型目录、下载模型、读取云端密钥或自动切换云端。首次使用前必须已有 faster-whisper `small` 缓存，或在配置中指定本地模型目录；启动器开启 Hugging Face 离线模式，缺少缓存会给出错误而非偷偷下载。Windows 可使用随 Resemblyzer 包提供的声纹模型；其他后端需预先准备。

`.vocalis` 是本启动器专用目录，与直接运行 `vocalis` 时默认的 `~/.vocalis` 分开。已有声纹不会自动复制；请在同一启动方式下登记和使用。更换声纹后端需重新登记。若 PowerShell 策略阻止脚本，使用已安装的 `vocalis local-qwen --start --local-audio` 与 `vocalis listen`，或按组织的脚本策略操作。

选择已部署 8B：` .\start-local.ps1 -Mode listen -ModelFile models/qwen3-8b-q4_k_m.gguf `。若 8080 已运行 4B，检查会明确报告模型不匹配，需自行停止旧服务再切换；程序不会杀掉已有模型进程。

## 网页控制台

```powershell
.\start-local.ps1 -Mode serve
# 另一个终端；首次需 npm install
cd ui
npm run dev
```

访问 [本地 HUD](http://localhost:5173)。开启 **“持续待机”** 后使用的是运行后端那台电脑的麦克风；关闭网页不会关闭常驻检测，请使用 **“关闭麦克风”** 或停止后端。网页单次录音同样经过声纹、唤醒和会话验证，最长 14 秒，不能与持续麦克风同时使用。

`serve` 仅启动控制台后端，不自动取得麦克风；无界面自动开始待机请使用 `listen`。未登记声纹时拒绝开始常驻监听，不会降级成“任何人都可以说”。

系统面板提供本地模型状态、配置、访问令牌、任务信息和语音设置。若后端设置 `VOCALIS_TOKEN`，在面板填写同一令牌；仅保存在当前浏览器会话。后端默认绑定回环地址，请不要把无鉴权接口暴露到公网。

## 本地模型与隐私边界

- 新配置默认 `brain.backend = "local-qwen"` 和 `local_only = true`。旧云端配置缺少新字段时也默认禁止访问外部模型；运行 `vocalis local-qwen` 可显式迁移。
- 使用本机 llama.cpp 的兼容 HTTP 协议，**不是云端 OpenAI API**。默认不发送 Authorization、不读取环境代理、不跟随重定向，只允许回环地址。
- 健康检查读取模型列表，不生成回答、不消耗推理 token。规则降级明确标注“规则模式”，不冒充 Qwen 正常回复。
- Windows 启动器选用 **本地 SAPI**；直接运行旧配置可能仍使用 **在线 Edge-TTS**。Edge 会发送待合成文字，音色目录也联网。请勿把所有运行方式都称为完全离线。
- 本地模式不自动注册内置 OpenAI/Claude Code 连接器；手动配置的 CLI agent、第三方插件、MCP 调用和屏幕按钮是独立的主动操作，可能访问外部服务。使用前审核其行为。
- “看看屏幕”和屏幕监管是明确开启的独立功能，不是待机音频链路。严格休眠隐私场景请保持屏幕监管关闭。
- 声纹是**概率识别**，没有实现活体检测、防录音重放或防声音克隆；不得作为支付、删除数据等高风险操作的唯一授权。参考 [安全说明](SECURITY.md)。

## 配置示例

```toml
[brain]
backend = "local-qwen"
local_only = true
base_url = "http://127.0.0.1:8080/v1"
model = "qwen3-4b-q4_k_m.gguf"
deployment_dir = 'D:\qwen-deployment'
fallback_to_rules = true

[wake_word]
enabled = true
phrases = ["hey d-voice", "d voice", "hey d voice", "你好 d-voice"]
cooldown_s = 2.0

[standby]
idle_timeout_s = 30.0
min_utterance_s = 0.5
max_utterance_s = 15.0
energy_floor = 0.005
sleep_phrases = ["休眠", "睡觉吧", "停止聆听", "go to sleep"]

[tts]
engine = "sapi" # Windows；Edge 是在线服务
```

安全管家使用本地 ASR 的词级时间戳验证唤醒片段。独立 `openWakeWord` 模块仍可用于开发，但其预训练模型名称不是自定义唤醒词模型，也不是安全管家的授权依据。

## 迭代与验证

| 版本 | 状态 | 内容 |
|---|---|---|
| 0.1.x | 历史基础 | 声纹模块、TTS、agent 事件与 HUD；各入口安全规则尚未统一 |
| **0.2.0rc1** | **当前候选版本，未发布** | 安全常驻待机、本地 Qwen、统一网页/CLI 验证、Windows 启动器与独立回归验收 |
| 0.2.0 | 待真人验收 | 声纹误接收/误拒绝、真实唤醒率、冷启动、8 小时稳定性 |
| 0.3+ | 计划 | 全双工打断、流式字幕、更轻量自定义唤醒、多用户和活体检测 |

详见 [ROADMAP](ROADMAP.md)、[CHANGELOG](CHANGELOG.md)、[本轮验收记录](docs/acceptance-0.2.md) 和 [产品复盘](docs/product-review-0.2.md)。测试通过不等于已测量真实声纹准确率或长期稳定性。

```powershell
python -m pytest -q
python -m ruff check vocalis tests examples
cd ui
npm run build
```

## 扩展与开发

- [架构](docs/architecture.md) · [实时音频模块](docs/realtime.md) · [事件与插件](docs/hooks.md)
- [MCP](docs/mcp.md)：`speak` / `report_progress` / `get_status` / `dispatch_task`；MCP 为主动程序调用，不经声纹唤醒，不提供语音审批工具。
- [贡献](CONTRIBUTING.md) · [维护](MAINTENANCE.md) · [演示](SHOWCASE.md)

项目代码与版本记录不包含模型权重、声纹、原始录音或本地日志。代码许可证为 [AGPL-3.0](LICENSE)。
