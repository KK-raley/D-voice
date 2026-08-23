# IndexTTS-2.5 Sidecar 部署与可插拔 TTS 后端

`vocalis/voice/backends/` + `vocalis/voice/sidecar/` 实现了**进程隔离的
语音克隆合成**：IndexTTS-2.5 跑在 GPU 机器的独立 sidecar 服务里，主框架
（可以是纯 CPU 笔记本）经 HTTP 访问；sidecar 不可用时自动降级 Edge-TTS。

## 为什么进程隔离（IndexTTS-2.5 调研结论）

IndexTTS-2.5 的依赖对主框架来说太重：

| 依赖 | 要求 | 与主框架的冲突 |
|---|---|---|
| 安装方式 | git clone + uv | 不在 PyPI，无法进正常依赖树 |
| Python | 3.10–3.11 | 主框架支持更新的版本 |
| 硬件 | CUDA GPU | 笔记本/无 GPU 机器直接不可用 |
| 权重 | ~10 GB | 加载慢且占显存 |

所以 sidecar 独立进程承载引擎，主框架只带一个轻量 HTTP 客户端
（`IndexTTSClientBackend`，仅依赖 httpx）。

## 架构

```
   主框架（任意机器，可纯 CPU）              GPU 机器
+------------------------------+     +-------------------------------+
| TTSRouter (voice/backends/)  |     | python -m vocalis.voice.sidecar|
|  ├─ EdgeTTSBackend  (兜底)   | HTTP|  ├─ /health   永远 200        |
|  └─ IndexTTSClientBackend ---|---->|  ├─ /voices   注册表          |
|        克隆音色 -> sidecar    |JSON |  ├─ /synthesize  base64 WAV   |
|        preset  -> Edge-TTS   |     |  └─ /synthesize/stream PCM    |
+------------------------------+     |     └─ IndexTTSEngine(懒加载) |
                                     +-------------------------------+
```

路由规则（`TTSRouter`，基于能力协商而非 try/except 嗅探）：

1. 克隆音色（在 sidecar 注册表里）-> IndexTTS；
2. preset 音色（`zh-CN-XiaoxiaoNeural` 命名模式）-> Edge-TTS；
3. sidecar 不可达 / 无 GPU / 引擎未就绪 -> 克隆音色按映射表降级到
   Edge-TTS preset（`fallback_voice`），绝不静默失败也绝不抛连接异常；
4. 克隆合成中途挂掉 -> 首块产出前自动用 Edge-TTS 重试；之后切换格式
   会产生损坏的音频流，错误如实上抛。

可用性探测带 30 s TTL：sidecar 恢复后最多一个周期即自动回归克隆路径。

## 一、GPU 机器：安装 IndexTTS-2.5 并启动 sidecar

```bash
# 1) IndexTTS-2.5 本体（git clone + uv，Python 3.10-3.11，需 CUDA）
git clone https://github.com/index-tts/index-tts.git
cd index-tts
uv venv && uv pip install -e .
# 权重（~10 GB）放到 checkpoints/，含 config.yaml

# 2) sidecar 只需要 vocalis 本体 + fastapi/uvicorn（无需 torch 之外的
#    额外依赖；torch/indextts 都是在 sidecar 进程内按需导入的）
uv pip install vocalis-voice-agent fastapi uvicorn

# 3) 启动（默认 127.0.0.1:8765；局域网部署改 --host 0.0.0.0，务必带 --token）
python -m vocalis.voice.sidecar --host 0.0.0.0 --port 8765 \
    --model-dir checkpoints --token "$(openssl rand -hex 32)"
```

CLI 参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--host` | `127.0.0.1` | 监听地址；跨机器访问用 `0.0.0.0` |
| `--port` | `8765` | 与主框架 `SidecarConfig.base_url` 默认一致 |
| `--model-dir` | `checkpoints` | IndexTTS 权重目录（含 config.yaml） |
| `--data-dir` | `~/.vocalis/sidecar` | 注册表数据目录（voices.json + refs/） |
| `--token` | 环境变量 `VOCALIS_SIDECAR_TOKEN` | Bearer 鉴权 token；未设置则无鉴权（仅建议本机回环） |
| `--log-level` | `INFO` | 日志级别 |

### 鉴权（跨机/局域网部署必配）

设置 token 后**所有端点（含 `/health`）**都要求请求携带

- `Authorization: Bearer <token>` 头，或
- `X-Sidecar-Token: <token>` 头

缺失/错误返回 `401`。token 来源优先级：`--token` CLI 参数 >
`VOCALIS_SIDECAR_TOKEN` 环境变量 > 无鉴权。无鉴权模式（两者都未设置）
适用于默认的本机回环监听（`127.0.0.1`）场景，启动日志会输出提示；
一旦 `--host 0.0.0.0` 对局域网开放，必须设置 token。

```bash
# 带鉴权的探测与合成
TOKEN=$(openssl rand -hex 32)   # 生成一次，长期保存
python -m vocalis.voice.sidecar --host 0.0.0.0 --token "$TOKEN" &

curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/health
curl -H "X-Sidecar-Token: $TOKEN" -X POST http://127.0.0.1:8765/synthesize \
    -H "Content-Type: application/json" \
    -d '{"text": "你好", "voice": "li"}'
```

### 请求体限制

- **大小**：请求按 `Content-Length` 前置校验，默认上限 **25 MB**（覆盖
  参考音频注册的 base64 膨胀），超限直接 `413`，不进入处理器；
- **文本长度**：`/synthesize` 系列端点的 `text` 字段上限 **2000 字符**
  （pydantic 校验），超限 `422`。

启动即探测环境（`probe_state`）：无 CUDA 或 `indextts` 不可导入时，
sidecar 以 `engine="none"` **降级模式**启动——`/health` 仍返回 200，
但合成端点返回 503。模型实例懒加载：首次合成才读 ~10 GB 权重。
模型推理串行化：IndexTTS2 实例非线程安全，sidecar 内部以推理锁把并发
请求排成队列（同一时刻只有一次 infer 在跑）。

## 二、主框架：启用 sidecar 客户端

`~/.vocalis/config.toml`：

```toml
[sidecar]
enabled = true                       # 总开关；false 时行为与旧版完全一致
base_url = "http://gpu-box:8765"     # sidecar 地址
timeout_s = 30.0                     # 克隆合成较慢，超时给足
fallback_voice = "zh-CN-XiaoxiaoNeural"  # 降级兜底 preset 音色
preset_map = { li = "zh-CN-YunxiNeural" }  # 指定音色的降级映射（可选）
```

组装入口：`vocalis.voice.backends.router.build_router(config)`——
`enabled=false`（默认）时路由器里只有 Edge-TTS 后端，零行为变化。

```python
from vocalis.voice.backends import build_router, SynthesisOptions

router = build_router()
# 克隆音色（sidecar 可用时走 IndexTTS，不可用自动降级 Edge）
audio = await router.synthesize("你好世界", "li", SynthesisOptions(speed=1.2))
# preset 音色（永远走 Edge-TTS）
audio = await router.synthesize("你好世界", "zh-CN-XiaoxiaoNeural")
# 注册克隆音色（consent 必填，缺失 400——无绕过路径）
info = await router.register_voice("me.wav", "li", "zh", consent="本人同意克隆")
```

## 三、sidecar HTTP 契约

设置 token 后所有端点都要求 `Authorization: Bearer <token>` 或
`X-Sidecar-Token` 头（未授权 401，含 `/health`）；所有请求按
Content-Length 前置校验（默认 25 MB，超限 413）。

| 端点 | 方法 | 请求 | 响应 |
|---|---|---|---|
| `/health` | GET | — | `{status, engine, gpu, voices}`，**永远 200** |
| `/voices` | GET | — | `[{name, language, kind, created_at}]` |
| `/voices/register` | POST | `{name, language, consent, audio_base64}` | 注册条目（consent 缺失 400；非法名/音频 400；>20 MB 413） |
| `/synthesize` | POST | `{text, voice, speed, emotion, duration_factor}` | `{audio(base64 WAV), sample_rate, duration_s, format}`；无引擎 503、未注册音色 404、text 超 2000 字符 422 |
| `/synthesize/stream` | POST | 同上 | 裸 PCM16 分块；采样率在 `X-Sample-Rate` 响应头 |

关键语义：

- **health 与合成分离**——不能因为克隆不可用就把整个服务标记为不健康；
  `engine` 字段（`indextts` / `none`）才是主框架的降级依据；
- **consent 合规**——参考 IndexTTS 官方 vLLM recipe 的强制同意标识：
  参考音频属于说话人生物特征，注册即提取音色特征，每条记录携带注册时
  的同意声明原文（`consent` 字段）作为审计痕迹；音色名白名单校验
  （字母/数字/下划线/连字符）杜绝路径穿越，**加载注册表时同样逐条校验**
  （被篡改的 `voices.json` 条目直接丢弃，`ref_file` 必须等于
  `<name>.wav`，参考路径解析后必须仍在 refs/ 目录内）；
- **注册表持久化**——`voices.json` 原子写 + `refs/<name>.wav` 落盘，
  重启不丢；引擎降级期间注册的音色在 GPU 机器上重启后即可用。

## 四、验证

```bash
# GPU 机器上
curl http://127.0.0.1:8765/health
# {"status":"ok","engine":"indextts","gpu":true,"voices":0}

# 主框架侧跑离线测试（MockTransport + FakeEngine，无需真实 sidecar）
python -m pytest tests/test_tts_backends.py tests/test_sidecar.py -q
```

两类测试完全离线：后端框架用 `httpx.MockTransport` 模拟 sidecar HTTP
响应；sidecar 服务用 `MockEngine` 注入（`create_app(state=...)` 显式
接受引擎实例），不触碰真实 GPU / indextts / 网络。
