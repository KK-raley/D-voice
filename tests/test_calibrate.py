"""G7 声纹 FAR/FRR 校准 harness 测试（完全离线）。

用假 gate（verify 返回预设相似度，不加载任何模型）验证统计计算的
数学正确性：far = 误接受的冒充样本数 / 冒充样本数，frr = 误拒绝的
本人样本数 / 本人样本数；以及推荐阈值、报告序列化与 CLI 集成。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from typer.testing import CliRunner

import vocalis.cli as cli_module
from vocalis.voice.audio import save_wav
from vocalis.voice.calibrate import evaluate_thresholds
from vocalis.voice.gate import GateDecision, VoiceGate


# ---------------------------------------------------------------------
# 离线假 gate
# ---------------------------------------------------------------------
class FakeGate:
    """verify(audio) 按标签返回预设相似度。

    evaluate_thresholds 只把 audio 透传给 gate.verify，因此测试用
    字符串标签代替真实音频即可。
    """

    def __init__(self, scores: dict[str, float], threshold: float = 0.5) -> None:
        self.scores = dict(scores)
        self.threshold = threshold
        self.verify_calls = 0

    def verify(self, audio: str, sample_rate: int = 16000) -> GateDecision:
        self.verify_calls += 1
        score = self.scores[audio]
        return GateDecision(
            accepted=score >= self.threshold,
            user="alice" if score >= self.threshold else None,
            similarity=score,
            threshold=self.threshold,
        )


# 本人相似度高、冒充者相似度低的标准场景（3 + 4 条样本）。
SELF_SCORES = {"self-1": 0.9, "self-2": 0.8, "self-3": 0.7}
IMPOSTOR_SCORES = {"imp-1": 0.6, "imp-2": 0.5, "imp-3": 0.4, "imp-4": 0.3}
THRESHOLDS = [0.45, 0.55, 0.65, 0.75, 0.85]


def _evaluate(**kwargs):
    gate = FakeGate({**SELF_SCORES, **IMPOSTOR_SCORES})
    report = evaluate_thresholds(
        gate, list(SELF_SCORES), list(IMPOSTOR_SCORES), THRESHOLDS, **kwargs
    )
    return gate, report


# ---------------------------------------------------------------------
# FAR / FRR 数学
# ---------------------------------------------------------------------
def test_far_frr_math_per_threshold():
    """far = 假接受数/冒充样本数，frr = 假拒绝数/本人样本数（手工核算）。"""
    _, report = _evaluate()

    # 结果按阈值升序排列，便于阅读
    assert [r.threshold for r in report.results] == sorted(THRESHOLDS)

    expected = {
        0.45: (0.5, 0.0),        # imp-1(0.6)/imp-2(0.5) 被误接受
        0.55: (0.25, 0.0),       # imp-1(0.6) 被误接受
        0.65: (0.0, 0.0),
        0.75: (0.0, 1 / 3),      # self-3(0.7) 被误拒绝
        0.85: (0.0, 2 / 3),      # self-2/self-3 被误拒绝
    }
    by_threshold = {r.threshold: r for r in report.results}
    for t, (far, frr) in expected.items():
        row = by_threshold[t]
        assert row.far == pytest.approx(far), f"threshold={t} FAR"
        assert row.frr == pytest.approx(frr), f"threshold={t} FRR"
        assert row.eer_distance == pytest.approx(abs(far - frr))


def test_boundary_score_equal_to_threshold_is_accepted():
    """相似度恰好等于阈值时按“接受”处理（与 VoiceGate 的 >= 语义一致）。"""
    gate = FakeGate({"s": 0.7, "i": 0.5})
    report = evaluate_thresholds(gate, ["s"], ["i"], [0.5, 0.7])
    by_threshold = {r.threshold: r for r in report.results}
    assert by_threshold[0.5].far == 1.0  # 0.5 >= 0.5 -> 误接受
    assert by_threshold[0.7].frr == 0.0  # 0.7 >= 0.7 -> 不算误拒绝


def test_eer_distance_zero_at_crossover():
    """FAR 与 FRR 相等（EER 点）时 eer_distance 为 0。"""
    gate = FakeGate({"s1": 0.9, "s2": 0.6, "i1": 0.7, "i2": 0.4})
    report = evaluate_thresholds(gate, ["s1", "s2"], ["i1", "i2"], [0.65])
    row = report.results[0]
    assert row.far == pytest.approx(0.5)
    assert row.frr == pytest.approx(0.5)
    assert row.eer_distance == pytest.approx(0.0)


# ---------------------------------------------------------------------
# 推荐阈值
# ---------------------------------------------------------------------
def test_recommended_minimizes_weighted_error():
    _, report = _evaluate()
    # 0.65 是唯一加权误差为 0 的阈值（FAR=FRR=0）
    assert report.recommended.threshold == 0.65
    assert report.recommended.far == 0.0
    assert report.recommended.frr == 0.0


def test_recommended_tie_prefers_stricter_threshold():
    """完美区分（多个零误差阈值）时取更高（更安全）的阈值。"""
    gate = FakeGate({"s1": 0.9, "s2": 0.8, "i1": 0.4, "i2": 0.3})
    report = evaluate_thresholds(gate, ["s1", "s2"], ["i1", "i2"], [0.5, 0.6, 0.7])
    assert report.recommended.threshold == 0.7


def test_recommended_respects_far_weight():
    """提高 FAR 权重 -> 愿意牺牲 FRR 换取更低误接受率。"""
    gate = FakeGate(
        {"s1": 0.74, "s2": 0.73, "s3": 0.72, "s4": 0.71, "i1": 0.75, "i2": 0.3}
    )
    self_audios = ["s1", "s2", "s3", "s4"]
    impostor_audios = ["i1", "i2"]
    balanced = evaluate_thresholds(gate, self_audios, impostor_audios, [0.7, 0.76])
    assert balanced.recommended.threshold == 0.7
    security = evaluate_thresholds(
        gate, self_audios, impostor_audios, [0.7, 0.76], far_weight=3.0
    )
    assert security.recommended.threshold == 0.76


# ---------------------------------------------------------------------
# 效率 / 输入校验 / 报告序列化
# ---------------------------------------------------------------------
def test_verify_called_once_per_audio():
    """每条音频只验证一次（复用相似度，不随阈值数放大）。"""
    gate, report = _evaluate()
    assert gate.verify_calls == len(SELF_SCORES) + len(IMPOSTOR_SCORES)
    assert report.n_self == 3
    assert report.n_impostor == 4


def test_thresholds_deduplicated_and_sorted():
    gate = FakeGate({"s": 0.9, "i": 0.4})
    report = evaluate_thresholds(gate, ["s"], ["i"], [0.7, 0.5, 0.7])
    assert [r.threshold for r in report.results] == [0.5, 0.7]


def test_empty_inputs_raise_friendly_errors():
    gate = FakeGate({})
    with pytest.raises(ValueError, match="本人"):
        evaluate_thresholds(gate, [], ["i"], [0.5])
    with pytest.raises(ValueError, match="冒充"):
        evaluate_thresholds(gate, ["s"], [], [0.5])
    with pytest.raises(ValueError, match="阈值"):
        evaluate_thresholds(gate, ["s"], ["i"], [])


def test_to_dict_is_json_serializable_and_complete():
    _, report = _evaluate()
    d = report.to_dict()
    assert d["n_self"] == 3
    assert d["n_impostor"] == 4
    assert d["far_weight"] == 1.0
    assert d["frr_weight"] == 1.0
    assert d["recommended"]["threshold"] == 0.65
    assert len(d["results"]) == len(THRESHOLDS)
    for row in d["results"]:
        assert set(row) == {"threshold", "far", "frr", "eer_distance"}
    json.dumps(d)  # 必须可直接序列化


def test_summary_is_plain_chinese():
    _, report = _evaluate()
    text = report.summary()
    assert "推荐阈值 0.65" in text
    assert "误接受率" in text
    assert "误拒绝率" in text
    assert "本人样本 3" in text
    assert "冒充者样本 4" in text
    assert "voice_gate.threshold" in text  # 提示如何应用推荐值


def test_evaluate_works_with_real_voice_gate(monkeypatch):
    """真实 VoiceGate 实例 + monkeypatch verify：锁定接口兼容性。"""
    gate = VoiceGate.__new__(VoiceGate)  # 跳过构造副作用（同 test_core 的做法）
    gate.config = None
    gate.backend = "resemblyzer"
    gate.threshold = 0.8
    gate.enroll_consistency = 0.75
    gate.profiles = {"alice": np.ones(4, dtype=np.float32)}

    def fake_verify(audio: str, sample_rate: int = 16000) -> GateDecision:
        score = {"s1": 0.9, "s2": 0.82, "i1": 0.4, "i2": 0.3}[audio]
        return GateDecision(
            accepted=score >= gate.threshold,
            user="alice" if score >= gate.threshold else None,
            similarity=score,
            threshold=gate.threshold,
        )

    monkeypatch.setattr(gate, "verify", fake_verify)
    report = evaluate_thresholds(gate, ["s1", "s2"], ["i1", "i2"], [0.8])
    assert report.n_self == 2
    assert report.n_impostor == 2
    assert report.results[0].frr == 0.0
    assert report.results[0].far == 0.0


# ---------------------------------------------------------------------
# CLI: vocalis calibrate
# ---------------------------------------------------------------------
runner = CliRunner()


class _CLIGate:
    """CLI 集成用假 gate：相似度取音频峰值（wav 幅度即编码的分数）。"""

    backend = "fake"
    threshold = 0.5

    def __init__(self) -> None:
        self.calls = 0

    def verify(self, audio, sample_rate: int = 16000) -> GateDecision:
        self.calls += 1
        score = float(np.max(np.abs(audio)))
        return GateDecision(
            accepted=score >= self.threshold,
            user="alice",
            similarity=score,
            threshold=self.threshold,
        )


def _make_wav_dir(tmp_path: Path, name: str, amplitudes: list[float]) -> Path:
    """生成一组 0.3s 测试 wav，幅度编码该样本的相似度。"""
    directory = tmp_path / name
    directory.mkdir()
    for i, amplitude in enumerate(amplitudes):
        save_wav(
            directory / f"utt-{i}.wav",
            np.full(4800, amplitude, dtype=np.float32),
            16000,
        )
    return directory


@pytest.fixture()
def _cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VOCALIS_HOME", str(tmp_path))


def test_cli_calibrate_missing_dir_friendly_error(_cli_env) -> None:
    result = runner.invoke(
        cli_module.app,
        [
            "calibrate",
            "--self-dir",
            "nope-self",
            "--impostor-dir",
            "nope-imp",
        ],
    )
    assert result.exit_code == 1
    assert "不存在" in result.output


def test_cli_calibrate_missing_impostor_dir_friendly_error(
    _cli_env, tmp_path: Path
) -> None:
    self_dir = _make_wav_dir(tmp_path, "self", [0.9])
    result = runner.invoke(
        cli_module.app,
        [
            "calibrate",
            "--self-dir",
            str(self_dir),
            "--impostor-dir",
            str(tmp_path / "nope-imp"),
        ],
    )
    assert result.exit_code == 1
    assert "不存在" in result.output


def test_cli_calibrate_empty_dir_friendly_error(_cli_env, tmp_path: Path) -> None:
    self_dir = tmp_path / "empty"
    self_dir.mkdir()
    impostor_dir = _make_wav_dir(tmp_path, "imp", [0.4])
    result = runner.invoke(
        cli_module.app,
        [
            "calibrate",
            "--self-dir",
            str(self_dir),
            "--impostor-dir",
            str(impostor_dir),
        ],
    )
    assert result.exit_code == 1
    assert "WAV" in result.output


def test_cli_calibrate_invalid_thresholds_friendly_error(
    _cli_env, tmp_path: Path
) -> None:
    self_dir = _make_wav_dir(tmp_path, "self", [0.9])
    impostor_dir = _make_wav_dir(tmp_path, "imp", [0.4])
    result = runner.invoke(
        cli_module.app,
        [
            "calibrate",
            "--self-dir",
            str(self_dir),
            "--impostor-dir",
            str(impostor_dir),
            "--thresholds",
            "abc,0.5",
        ],
    )
    assert result.exit_code == 1
    assert "无效" in result.output


def test_cli_calibrate_json_report(
    _cli_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    self_dir = _make_wav_dir(tmp_path, "self", [0.9, 0.9, 0.9])
    impostor_dir = _make_wav_dir(tmp_path, "imp", [0.4, 0.4])
    gate = _CLIGate()
    monkeypatch.setattr(cli_module, "VoiceGate", lambda: gate)

    result = runner.invoke(
        cli_module.app,
        [
            "calibrate",
            "--self-dir",
            str(self_dir),
            "--impostor-dir",
            str(impostor_dir),
            "--thresholds",
            "0.5,0.6,0.7",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert gate.calls == 5  # 每条音频只验证一次
    assert '"n_self"' in result.output
    assert '"n_impostor"' in result.output
    assert '"recommended"' in result.output
    # 本人 0.9 / 冒充者 0.4 完美区分：三个阈值零误差，推荐更严格的 0.7
    assert "0.7" in result.output


def test_cli_calibrate_table_output(
    _cli_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    self_dir = _make_wav_dir(tmp_path, "self", [0.9, 0.9])
    impostor_dir = _make_wav_dir(tmp_path, "imp", [0.4])
    gate = _CLIGate()
    monkeypatch.setattr(cli_module, "VoiceGate", lambda: gate)

    result = runner.invoke(
        cli_module.app,
        [
            "calibrate",
            "--self-dir",
            str(self_dir),
            "--impostor-dir",
            str(impostor_dir),
            "--thresholds",
            "0.5,0.7",
        ],
    )

    assert result.exit_code == 0
    assert "FAR" in result.output  # 阈值评估表
    assert "推荐阈值 0.7" in result.output  # 中文摘要面板
