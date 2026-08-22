"""VoiceGate 声纹阈值校准 harness（competitive-analysis G7 / Track E2）。

声纹验证的两类错误此消彼长：

* FAR（False Accept Rate，误接受率）：冒充者被当成本人放行的比例；
* FRR（False Reject Rate，误拒绝率）：本人被当成陌生人拒绝的比例。

两者都由 ``voice_gate.threshold``（余弦相似度门槛）决定：阈值越高越安全
（FAR 下降）但越难用（FRR 上升）。本模块提供工程化的校准流程：

1. 采集两组音频——本人（已注册用户）若干条 + 冒充者（其他说话人）若干条；
2. 对每个候选阈值计算 FAR / FRR / ``|FAR - FRR|``（eer_distance）；
3. 按 ``far_weight * FAR + frr_weight * FRR`` 加权误差最小者推荐阈值
   （安全优先场景调大 far_weight，体验优先场景调大 frr_weight；
   完全平分时取更高即更严格的阈值）。

评估只调用一次 ``gate.verify(audio)`` 并复用其返回的相似度——每条音频
只需计算一次说话人嵌入（这是最贵的步骤），不随候选阈值数量放大。
任何满足 ``verify(audio, sample_rate) -> GateDecision`` 协议的对象都能
接入（真实组件是 :class:`vocalis.voice.gate.VoiceGate`；离线测试用假
gate 注入预设结果）。

用法::

    from vocalis.voice.calibrate import evaluate_thresholds
    report = evaluate_thresholds(gate, self_audios, impostor_audios, [0.4, 0.5, 0.6])
    print(report.summary())  # 中文人话摘要
    config.voice_gate.threshold = report.recommended.threshold

CLI 入口：``vocalis calibrate --self-dir ... --impostor-dir ...``。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ThresholdResult:
    """单个候选阈值的评估结果。"""

    threshold: float
    far: float = 0.0           # 冒充者被误接受的比例（越低越安全）
    frr: float = 0.0           # 本人被误拒绝的比例（越低越好用）
    eer_distance: float = 0.0  # |FAR - FRR|，0 表示恰好落在等错误率(EER)点

    def to_dict(self) -> dict[str, Any]:
        return {
            "threshold": round(self.threshold, 6),
            "far": round(self.far, 6),
            "frr": round(self.frr, 6),
            "eer_distance": round(self.eer_distance, 6),
        }


@dataclass
class CalibrationReport:
    """一次校准的完整报告：各阈值结果 + 推荐阈值 + 样本统计。"""

    results: list[ThresholdResult]  # 按阈值升序
    recommended: ThresholdResult    # 加权误差最小的候选
    n_self: int                     # 本人样本数
    n_impostor: int                 # 冒充者样本数
    far_weight: float = 1.0
    frr_weight: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_self": self.n_self,
            "n_impostor": self.n_impostor,
            "far_weight": self.far_weight,
            "frr_weight": self.frr_weight,
            "recommended": self.recommended.to_dict(),
            "results": [r.to_dict() for r in self.results],
        }

    def summary(self) -> str:
        """中文人话摘要（CLI 校准报告面板直接展示）。"""
        rec = self.recommended
        return "\n".join(
            [
                f"校准完成：本人样本 {self.n_self} 条、冒充者样本 {self.n_impostor} 条，"
                f"共评估 {len(self.results)} 个候选阈值。",
                f"推荐阈值 {rec.threshold:g}：误接受率(FAR) {rec.far * 100:.1f}%，"
                f"误拒绝率(FRR) {rec.frr * 100:.1f}%，|FAR-FRR| {rec.eer_distance * 100:.1f}%。",
                f"含义：每 100 次冒充尝试约有 {rec.far * 100:.1f} 次被误放行；"
                f"每 100 次本人说话约有 {rec.frr * 100:.1f} 次被误拒绝。",
                f"应用：将 ~/.vocalis/config.toml 中 voice_gate.threshold 设为 {rec.threshold:g}。",
            ]
        )


def evaluate_thresholds(
    gate: Any,
    self_audios: Sequence[Any],
    impostor_audios: Sequence[Any],
    thresholds: Iterable[float],
    *,
    sample_rate: int = 16000,
    far_weight: float = 1.0,
    frr_weight: float = 1.0,
) -> CalibrationReport:
    """对每个候选阈值评估 FAR/FRR，并给出推荐阈值。

    参数
    ----
    gate:
        任何满足 ``verify(audio, sample_rate) -> GateDecision`` 协议的对象。
        只要求返回值带 ``similarity`` 属性——阈值扫描基于相似度重新判定
        接受/拒绝，gate 自身的 threshold 不参与计算。
    self_audios:
        本人（已注册用户）的音频列表，用于计算 FRR。
    impostor_audios:
        冒充者（其他说话人）的音频列表，用于计算 FAR。
    thresholds:
        候选阈值集合（去重后按升序评估）。
    sample_rate:
        透传给 ``gate.verify`` 的采样率（与实际录音一致）。
    far_weight / frr_weight:
        推荐阈值的加权系数：``far_weight * FAR + frr_weight * FRR`` 最小者
        胜出。安全优先 -> 调大 far_weight；免打扰优先 -> 调大 frr_weight。
        平分时优先 eer_distance 更小者，再平分取更高（更严格）的阈值。

    数学
    ----
    far = 误接受的冒充样本数 / 冒充样本总数
    frr = 误拒绝的本人样本数 / 本人样本总数
    eer_distance = |far - frr|
    """
    if not self_audios:
        raise ValueError("缺少本人（self）音频样本：至少需要 1 条用于计算 FRR")
    if not impostor_audios:
        raise ValueError("缺少冒充者（impostor）音频样本：至少需要 1 条用于计算 FAR")
    candidates = sorted({float(t) for t in thresholds})
    if not candidates:
        raise ValueError("候选阈值列表为空：请至少提供一个阈值（如 0.5,0.6,0.7）")

    # 每条音频只验证一次，之后基于相似度扫描阈值（嵌入计算是最贵的步骤）。
    self_scores = [
        float(gate.verify(a, sample_rate=sample_rate).similarity) for a in self_audios
    ]
    impostor_scores = [
        float(gate.verify(a, sample_rate=sample_rate).similarity)
        for a in impostor_audios
    ]

    n_self, n_impostor = len(self_scores), len(impostor_scores)
    results: list[ThresholdResult] = []
    for t in candidates:
        far = sum(1 for s in impostor_scores if s >= t) / n_impostor
        frr = sum(1 for s in self_scores if s < t) / n_self
        results.append(
            ThresholdResult(threshold=t, far=far, frr=frr, eer_distance=abs(far - frr))
        )

    # 推荐阈值：加权误差最小；平分时先比 |FAR-FRR|，再取更高（更安全）的阈值。
    recommended = min(
        results,
        key=lambda r: (
            far_weight * r.far + frr_weight * r.frr,
            r.eer_distance,
            -r.threshold,
        ),
    )
    return CalibrationReport(
        results=results,
        recommended=recommended,
        n_self=n_self,
        n_impostor=n_impostor,
        far_weight=far_weight,
        frr_weight=frr_weight,
    )
