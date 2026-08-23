"""IndexTTS sidecar 服务（独立进程，跑在 GPU 机器上）。

进程隔离的动机（IndexTTS-2.5 调研结论）：IndexTTS 依赖重（git clone +
uv 安装、Python 3.10-3.11、CUDA、~10 GB 权重），不能 import 进主框架；
sidecar 独立进程承载引擎，主框架（可以是纯 CPU 笔记本）经
:class:`~vocalis.voice.backends.indextts_client.IndexTTSClientBackend`
以 HTTP 访问。

启动::

    python -m vocalis.voice.sidecar                 # 127.0.0.1:8765
    python -m vocalis.voice.sidecar --host 0.0.0.0 --port 9000

无 GPU / 未装 indextts 时以 engine="none" 降级模式启动（/health 仍 200，
合成返回 503），主框架据此自动回退 Edge-TTS。
"""

from vocalis.voice.sidecar.engine import (
    EngineAudio,
    SidecarState,
    SynthesisEngine,
    probe_gpu,
    probe_indextts,
    probe_state,
)
from vocalis.voice.sidecar.registry import VoiceRegistry
from vocalis.voice.sidecar.server import create_app

__all__ = [
    "EngineAudio",
    "SidecarState",
    "SynthesisEngine",
    "VoiceRegistry",
    "create_app",
    "probe_gpu",
    "probe_indextts",
    "probe_state",
]
