# 事件钩子契约（Hooks Contract）

本文档是 Vocalis / D-VOICE 事件总线的**对外契约**：每个事件的语义、payload
字段、发布者，以及第三方如何通过 `bus.on()` 订阅事件、通过 entry-point
插件注册 agent connector、通过 TTS 文本钩子改写口播内容。

总线实现见 `vocalis/server/events.py`（`EventBus` + `EventType` 枚举），
事件源头代码分布在 `vocalis/agents/`、`vocalis/voice/`、`vocalis/dvoice/`、
`vocalis/server/`、`vocalis/notify/`。

---

## 1. 事件信封（Event envelope）

所有事件都是同一个 `Event` dataclass，经 WebSocket `/ws` 以如下 JSON
结构转发给 HUD（`Event.to_dict()`）：

| 字段 | 类型 | 说明 |
| ---- | ---- | ---- |
| `id` | `str` | 12 位十六进制随机 id（`uuid4().hex[:12]`），同一任务的 queued/started/progress/completed 事件共享同一 `data.id`（任务 id） |
| `ts` | `float` | Unix 时间戳（秒） |
| `type` | `str` | 事件类型字符串，如 `"task.progress"`（即 `EventType` 成员值） |
| `data` | `dict` | 各事件类型专属 payload，见下文事件目录 |

总线保留最近 256 条事件的历史（`bus.history`，`/api/events/history` 只读
接口；HUD 的 WebSocket `/ws` 连接时会自动回放历史事件，见
[vocalis/server/app.py](../vocalis/server/app.py)）。

## 2. `bus.on()` 使用配方

```python
from vocalis.server.events import bus, EventType

# (a) 精确匹配：只在任务完成时回调
def on_done(event):
    print(f"{event.data['agent']} 完成: {event.data['instruction']}")

bus.on(EventType.TASK_COMPLETED.value, on_done)

# (b) 前缀匹配："task.*" 命中 task.queued / started / progress / completed / failed
async def track_task(event):
    print(event.type, event.data.get("progress"))

bus.on("task.*", track_task)

# (c) 全量通配："*" 接收所有事件（日志、审计、调试面板）
bus.on("*", lambda e: log.debug("%s %s", e.type, e.data))
```

匹配规则（`EventBus._match`）：

| pattern | 命中范围 |
| ------- | -------- |
| `*` | 所有事件 |
| `task.*`（任何以 `.*` 结尾的前缀） | `value.startswith("task.")` |
| 其他 | 精确相等 |

**handler 签名与 async 支持**：handler 接收一个 `Event` 参数，返回值被
忽略。同步与异步（`async def`）handler 均可：`publish()` 发现返回协程时
自动 `asyncio.create_task` 调度，不会阻塞发布方。

**错误隔离**：单个 handler 抛出的异常**绝不会**影响发布方或其他订阅者
——同步 handler 的异常在 `publish()` 内被捕获并 `logger.exception` 记录；
异步 handler 运行在独立 task 里，其异常同样不会外溢（建议在 async
handler 体内自行 `try/except` 并记录日志，便于排障）。

队列式订阅（HUD、`TaskMonitor` 采用的通道）：`q = bus.subscribe(pattern)`
返回 `asyncio.Queue`（容量 512，慢消费者丢最旧事件而不是阻塞发布方），
用完调用 `bus.unsubscribe(q)`。

---

## 3. 事件目录（按 `EventType`）

### 3.1 语音管线（预留契约事件）

> **注意**：以下五个事件已在 `EventType` 中定义、并被架构文档与 HUD
> （`ui/src/components/AgentFeed.tsx`）消费，但**当前版本代码中尚无
> 内置发布者**——它们是 VoiceGate / ASR / Realtime 管线接入时的既定
> 契约。payload 字段按现有 `GateDecision.to_dict()`
> （`vocalis/voice/gate.py`）约定，接入时请遵守。

#### `voice.detected`

麦克风检测到人声活动（VAD 命中）。

| 字段 | 类型 | 说明 |
| ---- | ---- | ---- |
| `energy` | `float` | 语音段能量（约定字段） |

发布者：预留（实时语音管线）。

#### `voice.accepted` / `voice.rejected`

声纹门（VoiceGate）判定结果——被拒绝的语音在到达任何 LLM / agent
之前就已拦截。

| 字段 | 类型 | 说明 |
| ---- | ---- | ---- |
| `accepted` | `bool` | 是否放行（`rejected` 事件恒为 `False`） |
| `user` | `str \| None` | 命中的已注册用户名；拒绝时为 `None` |
| `similarity` | `float` | 与最相似声纹中心的余弦相似度（保留 4 位） |
| `threshold` | `float` | 本次判定使用的阈值 |
| `runner_up` | `[str, float] \| None` | 次相近用户与其相似度（诊断用） |

发布者：预留（`VoiceGate` 接入总线时）。

#### `asr.partial` / `asr.final`

ASR 流式识别的中间/最终转写。

| 字段 | 类型 | 说明 |
| ---- | ---- | ---- |
| `text` | `str` | 转写文本（partial 为增量中间结果，final 为定稿） |

发布者：预留（`vocalis/voice/asr.py` 接入总线时）。

### 3.2 TTS

#### `tts.speaking`

TTS 开始合成一段文本（HUD 用来镜像口播内容）。

| 字段 | 类型 | 说明 |
| ---- | ---- | ---- |
| `profile` | `str` | 使用的音色 profile 名（如 `"aria"`） |
| `text` | `str` | 即将合成的文本，**截断到 120 字符**；已过 pre 钩子变换（见第 6 节） |

发布者：`TTSService.synthesize`（`vocalis/voice/tts.py`）。

### 3.3 Agent 任务生命周期

以下五个事件的 payload 均为 `TaskRecord.to_dict()`
（`vocalis/agents/base.py`），同一任务共享同一 `id`：

| 字段 | 类型 | 说明 |
| ---- | ---- | ---- |
| `id` | `str` | 任务 id（10 位 hex） |
| `agent` | `str` | 执行任务的 connector 名（如 `"echo"`） |
| `instruction` | `str` | 原始指令文本 |
| `status` | `str` | `queued` / `running` / `completed` / `failed` / `cancelled` |
| `createdAt` | `float` | 任务创建时间戳 |
| `elapsed` | `float` | 已耗时秒数（未结束则取当前时刻） |
| `progress` | `float` | 进度 0..1（保留 3 位） |
| `currentStep` | `str` | 最近一次人类可读步骤描述（D-VOICE 会朗读它） |
| `output` | `str` | 最终输出文本 |
| `error` | `str \| None` | 失败原因（仅 `failed`） |

#### `task.queued`

任务已入队、尚未开始执行（status=`queued`，`output` 为空）。

发布者：`AgentRegistry.dispatch`（`vocalis/agents/registry.py`）、
`DVoiceMCPContext.dispatch`（`vocalis/server/mcp.py`）。

#### `task.started`

connector 开始执行（status=`running`）。

发布者：`AgentConnector.run`（`vocalis/agents/base.py`）。

#### `task.progress`

两种发布形态：

1. **connector 内部进度**（payload 为上表 `TaskRecord` 字段）——
   发布者：`AgentConnector.run`（每个 yield 的步骤/进度都会发布一次）。
2. **外部主动上报**（G8，payload 见下）——发布者：
   `DVoiceMCPContext.report_progress`（`vocalis/server/mcp.py`）：

| 字段 | 类型 | 说明 |
| ---- | ---- | ---- |
| `task_id` | `str` | 上报方自己的任务标识（注意与形态 1 的 `id` 字段名不同） |
| `agent` | `str` | 上报的 agent 名 |
| `progress` | `float` | 进度，已 clamp 到 [0, 1] |
| `current_step` | `str` | 当前步骤描述 |
| `note` | `str` | 自由备注 |

#### `task.completed` / `task.failed`

任务终态（`progress=1.0` / `error` 填充失败原因）。

发布者：`AgentConnector.run`。

> **取消语义**：任务被取消时（`asyncio.Task.cancel()` 传播），复用
> `task.failed` 事件，`status="cancelled"`、`error="cancelled"`——
> 订阅者应先查 `status` 再决定语义，不要把取消当作失败播报。

### 3.4 Agent 状态

#### `agent.status`

connector 状态变化（任务结束/出错后发布，HUD 的 agent 卡片用它刷新）。

| 字段 | 类型 | 说明 |
| ---- | ---- | ---- |
| `agent` | `str` | connector 名 |
| `status` | `str` | `idle` / `busy` / `error` / `offline` |
| `health` | `dict` | 连接器滚动健康快照（`ConnectorHealth.to_dict()`）：`last_error`、`last_latency_ms`、`last_success_ts`、`consecutive_failures`、`total_runs`、`total_failures`（可选字段，老版本事件无此键） |

发布者：`AgentConnector.run` 的 `finally` 块（`vocalis/agents/base.py`）。

### 3.5 D-VOICE 大脑

#### `dvoice.saying`

D-VOICE 即将口播一段文本（先于 `tts.speaking` 发布）。

| 字段 | 类型 | 说明 |
| ---- | ---- | ---- |
| `text` | `str` | 完整口播文本（不截断） |

发布者：`DVoiceMCPContext.speak`（`vocalis/server/mcp.py`）、
`AppState._narrate` 与对话端点（`vocalis/server/app.py`）。

#### `dvoice.command`

Commander 解析完一条语音指令、生成执行计划。

| 字段 | 类型 | 说明 |
| ---- | ---- | ---- |
| `assignments` | `list[{agent, instruction}]` | 拆解出的子任务（支持多 agent 并行 fan-out） |
| `question` | `str \| None` | 识别出的问答（交给 DVoiceBrain），无则 `None` |
| `statusQuery` | `bool` | 是否为状态询问 |
| `raw` | `str` | 原始用户话语 |

发布者：`Commander.execute`（`vocalis/dvoice/commander.py`）。

#### `monitor.alert`

监控告警（watchdog 发现任务停滞、本地大脑降级等）。

| 字段 | 类型 | 说明 |
| ---- | ---- | ---- |
| `message` | `str` | 人类可读告警描述 |
| `task_id` | `str` | 关联任务 id（仅 watchdog 停滞告警携带） |

发布者：`TaskMonitor._watchdog_loop`（`vocalis/dvoice/monitor.py`）、
`DVoiceBrain` 降级路径（`vocalis/dvoice/assistant.py`）。

### 3.6 系统

#### `system`

通用系统消息（启动横幅、通知摘要等）。

| 字段 | 类型 | 说明 |
| ---- | ---- | ---- |
| `message` | `str` | 消息正文 |
| `level` | `str` | 可选；通知摘要使用 `"notify"` |

发布者：FastAPI 启动横幅（`vocalis/server/app.py`）、
`Notifier.notify_task`（`vocalis/notify/notifier.py`）、
CLI 唤醒词命中（`vocalis/cli.py`）。

---

## 4. 第三方集成（一）：entry-point 插件注册 connector

第三方包**无需改动 Vocalis 代码**即可挂载自定义 agent：在
`pyproject.toml` 声明 `vocalis.agents` group 的 entry point，
`build_default_registry()` 会在启动时自动发现并注册
（`vocalis/agents/plugins.py`）。

**宿主侧契约**（由 `load_entry_point_connectors` 保证）：

- entry point 加载结果必须是**返回 `AgentConnector` 实例的可调用对象**
  （connector 类或工厂函数均可；直接加载出实例也被宽容接受）。
- 工厂签名若接受至少一个位置参数，调用时第一个参数传入 `event_bus`
  （与宿主共享同一条总线）；无参工厂亦可（connector 回落到全局单例
  `bus`）。
- **失败隔离**：单个插件的加载/实例化/校验失败只记 `WARNING` 日志，
  不影响其余插件与宿主进程。
- **重名保护**：与已注册 connector（含内置 `echo`）重名的插件被跳过，
  绝不覆盖。
- 返回值为本次新注册的 connector 列表。

**插件包示例**：

```toml
# pyproject.toml（插件包）
[project.entry-points."vocalis.agents"]
weather = "my_vocalis_pack.plugin:WeatherAgent"
```

```python
# my_vocalis_pack/plugin.py
from typing import Any

from vocalis.agents.base import AgentConnector, TaskRecord


class WeatherAgent(AgentConnector):
    name = "weather"
    description = "查询天气的示例第三方 agent"
    capabilities = ("weather",)

    # 签名接受 event_bus -> 宿主注入总线；写成 def __init__(self) 也可以
    def __init__(self, event_bus=None) -> None:
        super().__init__(event_bus)

    async def stream_run(self, instruction: str, record: TaskRecord, **_: Any):
        yield "正在查询天气"
        yield 0.5
        record.output = f"今天晴，25 度（{instruction}）"
        yield 1.0
```

安装该包（`pip install my-vocalis-pack`）后重启 Vocalis，`weather`
即出现在 `registry.list()` 中，可被语音指令 `"让 weather 查一下北京天气"`
直接调用。

## 5. 第三方集成（二）：订阅事件做自定义通知

进程内插件/脚本直接用 `bus.on()` 挂回调（错误隔离见第 2 节）：

```python
import logging

from vocalis.server.events import bus, EventType

logger = logging.getLogger("my.notify")

async def notify_task_done(event):
    """任务完成 -> 自定义推送（企业微信 / 邮件 / 桌面通知……）。"""
    if event.type == EventType.TASK_COMPLETED:
        await send_webhook(f"{event.data['agent']} 完成：{event.data['instruction']}")
    elif event.type == EventType.TASK_FAILED:
        await send_webhook(f"{event.data['agent']} 失败：{event.data['error']}")

bus.on("task.*", notify_task_done)     # 整个任务生命周期
bus.on(EventType.MONITOR_ALERT.value,  # watchdog 告警
       lambda e: logger.warning("monitor: %s", e.data["message"]))
```

---

## 6. TTS 文本钩子（pre / post hooks）

`TTSService`（`vocalis/voice/tts.py`）支持两类文本钩子，用于在合成
前后插入自定义处理——典型场景是把 TTS 引擎念不好的文本规范化成口语
（如 `"2m41s"` → `"两分四十一秒"`）、或对合成结果做统计与缓存。

**pre_hooks**：`callable[[str], str]`，合成前**按注册顺序依次**变换
文本。`tts.speaking` 事件、引擎调用与音频缓存键使用的都是变换后的
文本；`post_hooks` 收到的也是变换后的文本。

**post_hooks**：`callable[[str, bool], None]`，合成结束后回调
（`(text, ok)`）；**成功与失败（引擎异常 / 引擎未注册）路径都会触发**，
`ok=False` 时可用于失败统计。

**异常隔离**：单个钩子抛异常只记 `WARNING` 日志——pre 钩子失败保留
当前文本继续，post 钩子失败不影响其他钩子与合成结果返回。

两种注册方式：

```python
from vocalis.voice.tts import TTSService

# (a) 构造时注入（依赖注入风格，与 engines/event_bus 一致）
svc = TTSService(config, bus, pre_hooks=[normalize_duration],
                 post_hooks=[log_stats])

# (b) 运行时追加（插件在加载期挂钩子）
svc.add_pre_hook(normalize_duration)
svc.add_post_hook(lambda text, ok: stats.record(text, ok))
```

**示例：时长口语化 pre 钩子**

```python
import re

_DIGITS = "零一二三四五六七八九"

def _num_to_chinese(n: int) -> str:
    if n < 10:
        return _DIGITS[n]
    tens, ones = divmod(n, 10)
    head = "十" if tens == 1 else _DIGITS[tens] + "十"
    return head + (_DIGITS[ones] if ones else "")

def normalize_duration(text: str) -> str:
    """'2m41s' -> '两分四十一秒'（TTS 引擎读不好缩写时长）。"""
    def repl(m: re.Match[str]) -> str:
        minutes, seconds = int(m.group(1)), int(m.group(2))
        parts = []
        if minutes:
            parts.append(("两" if minutes == 2 else _num_to_chinese(minutes)) + "分")
        if seconds or not minutes:
            parts.append(_num_to_chinese(seconds) + "秒")
        return "".join(parts)
    return re.sub(r"\b(\d+)m(\d+)s\b", repl, text)
```

`speak()` 内部复用 `synthesize()`，同样应用这两类钩子。

---

## 7. 相关测试

| 测试文件 | 覆盖 |
| -------- | ---- |
| `tests/test_plugins.py` | entry-point 插件：成功注册、失败隔离、重名跳过、非法返回值、`build_default_registry` 集成 |
| `tests/test_tts_hooks.py` | TTS 钩子：文本规范化、链式变换、异常隔离、成功/失败路径回调、构造注入与运行时注册 |
| `tests/test_core.py` | EventBus 模式匹配、发布/订阅基础行为 |
