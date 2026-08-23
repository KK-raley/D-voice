"""vocalis.dual_stream——双流（dual-stream）语音代理运行时。

与旧三流架构不同：没有 UI 文本展示流，只保留两条流——

* :class:`VoiceStream`：实时语音输出（增量文本 -> 句级流式 TTS -> 排队播放，
  低首包延迟；支持自然停顿 / 恢复 / barge-in 硬打断）。
* :class:`ToolStream`：工具 / 长任务的后台 asyncio 队列，持续执行、
  绝不阻塞语音流，结果以 ``task.*`` 事件广播。

:class:`DualStreamOrchestrator` 负责协调两流：工具执行期间语音自然停顿
（可选 filler / 思考提示音，见 :class:`PacingPolicy`），工具结果就绪后语音
恢复并播报；用户打断时语音立即停播而工具继续工作。

最小示例::

    from vocalis.dual_stream import DualStreamOrchestrator

    orch = DualStreamOrchestrator()
    await orch.start()
    orch.say("我来查一下。")
    orch.submit_tool("search", do_search, announce="查到了。")
    ...
    await orch.shutdown()
"""

from vocalis.dual_stream.orchestrator import (
    SESSION_INTERRUPTED,
    SESSION_STARTED,
    SESSION_STOPPED,
    DualStreamOrchestrator,
)
from vocalis.dual_stream.pacing import PacingPolicy, PausePlan
from vocalis.dual_stream.tool_stream import ToolFactory, ToolResult, ToolStream
from vocalis.dual_stream.voice_stream import (
    VOICE_ERROR,
    VOICE_IDLE,
    VOICE_INTERRUPTED,
    VOICE_PAUSED,
    VOICE_RESUMED,
    AudioSink,
    PlayerSink,
    VoiceStream,
)

__all__ = [
    "SESSION_INTERRUPTED",
    "SESSION_STARTED",
    "SESSION_STOPPED",
    "AudioSink",
    "DualStreamOrchestrator",
    "PausePlan",
    "PacingPolicy",
    "PlayerSink",
    "ToolFactory",
    "ToolResult",
    "ToolStream",
    "VOICE_ERROR",
    "VOICE_IDLE",
    "VOICE_INTERRUPTED",
    "VOICE_PAUSED",
    "VOICE_RESUMED",
    "VoiceStream",
]
