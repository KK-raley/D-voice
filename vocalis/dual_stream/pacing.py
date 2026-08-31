"""自然停顿策略（pacing）——让工具等待期的沉默像"思考"而非冷场。

真人对话里，说话人在等待检索/回忆时会自然停顿 1~3 秒，有时补一句
"嗯……"、"让我想想"，或先出一声短促的思考提示音。本模块把这些
行为抽成可离线测试的纯策略：

* :class:`PausePlan`   —— 一次停顿的执行计划（时长 / filler / 提示音）。
* :class:`PacingPolicy`—— 从"预计等待时长"推导 :class:`PausePlan` 的规则集。

设计取向：
* 确定性优先：filler 按配置顺序轮换而非随机抽取，便于离线断言。
* 停顿时长 clamp 在 ``[min_pause_s, max_pause_s]``；已知工具预计耗时
  （``expected_wait_s``）时按其取值，未知时取 ``default_pause_s``。
* filler 与思考提示音各有触发阈值——停顿足够长才值得出声，短等待
  保持安静反而更像真人。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PausePlan:
    """一次自然停顿的执行计划。

    属性：
        pause_s:     计划停顿时长（秒），已 clamp 到策略区间。
        filler:      停顿前先说出口的填充短语（如"嗯……"），可为 None。
        chime_audio: 思考提示音音频字节（如一声"叮"），可为 None。
    """

    pause_s: float
    filler: str | None = None
    chime_audio: bytes | None = None

    @property
    def has_filler(self) -> bool:
        """是否包含填充短语。"""
        return bool(self.filler)

    @property
    def has_chime(self) -> bool:
        """是否包含思考提示音。"""
        return self.chime_audio is not None


@dataclass
class PacingPolicy:
    """自然停顿规则集（纯函数式策略，无 IO、无时钟依赖）。

    属性：
        min_pause_s:       单次停顿的下限（默认 1 秒，真人思考停顿起点）。
        max_pause_s:       单次停顿的上限（默认 3 秒，超过即接近冷场）。
        default_pause_s:   预计等待时长未知时的默认停顿（默认 2 秒）。
        filler_phrases:    填充短语池，按顺序轮换；空元组表示从不说 filler。
        filler_min_pause_s: 停顿至少达到该时长才说 filler（默认 1.5 秒）。
        chime_audio:       思考提示音（原始音频字节）；None 表示不用。
        chime_min_pause_s: 停顿至少达到该时长才播提示音（默认 2 秒）。
    """

    min_pause_s: float = 1.0
    max_pause_s: float = 3.0
    default_pause_s: float = 2.0
    filler_phrases: tuple[str, ...] = ()
    filler_min_pause_s: float = 1.5
    chime_audio: bytes | None = None
    chime_min_pause_s: float = 2.0
    _filler_cursor: int = field(default=0, repr=False, compare=False)

    def plan_pause(self, expected_wait_s: float | None = None) -> PausePlan:
        """为一次工具等待规划自然停顿。

        参数：
            expected_wait_s: 工具预计耗时（秒）；None 表示未知。

        返回：
            :class:`PausePlan`，调用方（orchestrator）负责执行：
            先播提示音、再说 filler、然后让语音流进入软停顿。
        """
        if expected_wait_s is None:
            # 未知等待取默认值——同样要 clamp（默认值可被配置出区间）。
            pause_s = self.default_pause_s
        else:
            pause_s = expected_wait_s
        pause_s = min(max(pause_s, self.min_pause_s), self.max_pause_s)

        filler: str | None = None
        if self.filler_phrases and pause_s >= self.filler_min_pause_s:
            # 轮换而非随机：离线测试可精确断言，行为也更"有性格"。
            filler = self.filler_phrases[self._filler_cursor % len(self.filler_phrases)]
            self._filler_cursor += 1

        chime: bytes | None = None
        if self.chime_audio is not None and pause_s >= self.chime_min_pause_s:
            chime = self.chime_audio

        return PausePlan(pause_s=pause_s, filler=filler, chime_audio=chime)
