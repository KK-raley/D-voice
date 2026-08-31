"""``vocalis doctor``: one-shot local-environment diagnosis (P0-4).

First-install experience beats another model parameter: this check collects
the usual failure points - microphone devices, sample rate, speaker-encoder
backend, faster-whisper cache, local brain service, TTS engines (including
Windows SAPI), permissions and the network boundary - and reports each as
pass / warn / fail with a concrete fix hint. Checks never raise: a broken
subsystem is a red row, not a traceback.
"""

from __future__ import annotations

import asyncio
import os
import platform
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import vocalis


@dataclass
class CheckResult:
    """One diagnostic row: ``ok`` True/False/None (None = warning only)."""

    name: str
    ok: bool | None
    detail: str
    fix: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "detail": self.detail,
            "fix": self.fix,
            **self.extra,
        }


def _model_cache_dirs() -> list[Path]:
    home = Path.home()
    candidates = [
        home / ".cache" / "huggingface" / "hub",
        home / ".cache" / "vocalis",
        Path(os.environ.get("HF_HOME", home / ".cache" / "huggingface")),
    ]
    return candidates


def _dir_size_mb(path: Path) -> float:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total / (1024 * 1024)


def check_python() -> CheckResult:
    version = sys.version_info
    if version < (3, 10):
        return CheckResult(
            "python", False, f"{platform.python_version()} (需要 >= 3.10)",
            fix="使用 Python 3.10-3.12",
        )
    return CheckResult("python", True, platform.python_version())


def check_config() -> CheckResult:
    home = Path(os.environ.get("VOCALIS_HOME", Path.home() / ".vocalis"))
    path = home / "config.toml"
    if not path.is_file():
        return CheckResult(
            "config", None, f"未找到 {path}（将使用默认配置）",
            fix="运行任意 vocalis 命令即可生成默认配置",
        )
    try:
        from vocalis.config import VocalisConfig

        if sys.version_info >= (3, 11):
            import tomllib
        else:
            import tomli as tomllib  # type: ignore[no-redef]
        VocalisConfig.from_dict(tomllib.loads(path.read_text(encoding="utf-8")))
        return CheckResult("config", True, str(path))
    except Exception as e:
        return CheckResult(
            "config", False, f"{path} 解析失败：{e}",
            fix="备份后删除该文件以重新生成默认配置",
        )


def check_audio() -> CheckResult:
    try:
        import sounddevice as sd

        devices = sd.query_devices()
        default_input = sd.default.device[0]
        if default_input < 0 or default_input >= len(devices):
            return CheckResult(
                "microphone", None, "未设置默认输入设备",
                fix="接入麦克风并在系统声音设置中设为默认",
            )
        info = devices[default_input]
        name = str(info.get("name", "?"))
        channels = int(info.get("max_input_channels", 0))
        rate = int(info.get("default_samplerate", 0))
        if channels <= 0:
            return CheckResult(
                "microphone", False, f"{name}（无输入通道）",
                fix="检查设备是否为输入设备 / 驱动是否正常",
            )
        return CheckResult(
            "microphone", True, f"{name} · {rate} Hz · {channels}ch",
            extra={"devices": len(devices), "sample_rate": rate},
        )
    except Exception as e:
        return CheckResult(
            "microphone", False, f"无法枚举音频设备：{e}",
            fix="pip install 'vocalis-voice-agent[voice]' 并检查 PortAudio",
        )


def check_voice_gate(config: Any) -> CheckResult:
    try:
        from vocalis.voice.gate import VoiceGate

        gate = VoiceGate(config)
        backend = getattr(gate, "backend_name", None) or type(
            getattr(gate, "encoder", None)
        ).__name__
        count = gate.profile_count()
        detail = f"后端 {backend} · 已注册 {count} 个声纹"
        if count == 0:
            return CheckResult(
                "voiceprint", None, detail,
                fix="运行 vocalis enroll --user YOUR_NAME 录入声纹",
            )
        return CheckResult("voiceprint", True, detail)
    except Exception as e:
        return CheckResult(
            "voiceprint", False, f"声纹后端不可用：{e}",
            fix="安装 voiceprint/voice extra 并确认模型缓存可读写",
        )


def check_asr_cache(config: Any) -> CheckResult:
    model = getattr(getattr(config, "asr", None), "model", None) or "small"
    found: list[tuple[str, float]] = []
    for base in _model_cache_dirs():
        if not base.is_dir():
            continue
        for p in base.iterdir():
            if "whisper" in p.name.lower() or "faster-whisper" in p.name.lower():
                found.append((p.name, _dir_size_mb(p)))
    if found:
        names = ", ".join(f"{n} ({s:.0f}MB)" for n, s in found[:3])
        return CheckResult("asr-cache", True, names)
    return CheckResult(
        "asr-cache", None, f"未找到 Whisper 模型缓存（首次运行会下载 {model}）",
        fix="或离线预先放置 faster-whisper 模型到 HF 缓存目录",
    )


async def check_brain(config: Any) -> CheckResult:
    try:
        from vocalis.dvoice.assistant import DVoiceBrain

        brain = DVoiceBrain(config)
        b = config.brain
        if not b.enabled:
            return CheckResult(
                "brain", None, f"大脑未启用（backend={b.backend}）",
                fix="vocalis local-qwen 配置本地模型后启用",
            )
        ok = await asyncio.wait_for(brain.available(), timeout=6.0)
        endpoint = b.base_url or b.host or "-"
        detail = f"{b.backend} @ {endpoint} · model={b.model} · {'可用' if ok else '不可达'}"
        if ok:
            return CheckResult("brain", True, detail)
        return CheckResult(
            "brain", False, detail,
            fix="启动本地 llama.cpp/Ollama 服务，或运行 vocalis local-qwen --check",
        )
    except Exception as e:
        return CheckResult("brain", False, f"探测失败：{e}")


_SAPI_PROBE_CODE = (
    "import pythoncom, win32com.client;"
    "pythoncom.CoInitialize();"
    "v = win32com.client.Dispatch('SAPI.SpVoice');"
    "print(len(list(v.GetVoices())));"
    "pythoncom.CoUninitialize()"
)


def _probe_sapi_voices() -> int:
    """Count SAPI voices; raises RuntimeError when SAPI is unusable.

    The probe runs in a *subprocess* on purpose: SAPI COM can hard-crash
    (access violation) in restricted environments, and a native crash cannot
    be caught in-process.
    """
    try:
        proc = subprocess.run(  # noqa: S603
            [sys.executable, "-c", _SAPI_PROBE_CODE],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"SAPI probe failed to run: {e}") from e
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "SAPI probe failed").strip()[-200:])
    try:
        return int(proc.stdout.strip() or 0)
    except ValueError as e:
        raise RuntimeError(f"SAPI probe returned invalid output: {proc.stdout!r}") from e


def check_tts(config: Any, probe_sapi: bool = False) -> list[CheckResult]:
    results: list[CheckResult] = []
    engine = config.tts.engine
    try:
        import edge_tts  # noqa: F401

        results.append(CheckResult("tts-edge", True, "edge-tts 已安装" + (
            "（当前引擎）" if engine == "edge" else "（备用）"
        )))
    except ImportError:
        results.append(CheckResult(
            "tts-edge", None, "edge-tts 未安装",
            fix="pip install 'vocalis-voice-agent[voice]'",
        ))
    if os.name == "nt":
        try:
            import pythoncom  # noqa: F401
            import win32com.client  # noqa: F401

            pywin32_ok = True
        except ImportError:
            pywin32_ok = False
        if not pywin32_ok:
            results.append(CheckResult(
                "tts-sapi", None, "pywin32 未安装，无法使用 SAPI",
                fix="pip install pywin32",
            ))
        elif not probe_sapi:
            # Library/test default: importing SAPI COM in-process can hard-
            # crash restricted environments, so probing is opt-in via CLI.
            results.append(CheckResult(
                "tts-sapi", None,
                "pywin32 可用 · 未做 SAPI 语音探测（探测在受限环境可能触发 COM 崩溃）",
                fix="运行 vocalis doctor 启用子进程级 SAPI 探测",
            ))
        else:
            try:
                voices = _probe_sapi_voices()
                results.append(CheckResult(
                    "tts-sapi", True,
                    f"SAPI 可用 · {voices} 个系统语音"
                    + ("（当前引擎）" if engine == "sapi" else "（离线兜底）"),
                ))
            except Exception as e:
                results.append(CheckResult(
                    "tts-sapi", False, f"SAPI 不可用：{e}",
                    fix="受限账户可能禁止 COM；可继续用 --text-only 或 Edge-TTS",
                ))
    else:
        results.append(CheckResult("tts-sapi", None, "非 Windows，跳过 SAPI"))
    if engine == "sapi" and os.name != "nt":
        results.append(CheckResult(
            "tts-engine", False, f"config.tts.engine='sapi' 但当前是 {os.name}",
            fix="改回 tts.engine='edge'",
        ))
    return results


def check_network_boundary(config: Any) -> CheckResult:
    b = config.brain
    local_only = bool(b.local_only)
    sidecar = bool(getattr(getattr(config, "sidecar", None), "enabled", False))
    if local_only:
        return CheckResult(
            "network-boundary", True,
            "local_only=True · 大脑仅回环；声纹与 ASR 全本地",
            extra={"sidecar": sidecar},
        )
    return CheckResult(
        "network-boundary", None,
        "local_only=False · 允许云后端（OpenAI 兼容 / Edge-TTS 出网）",
        fix="如需严格离线：vocalis config 中 brain.local_only=true",
        extra={"sidecar": sidecar},
    )


def check_permissions() -> CheckResult:
    home = Path(os.environ.get("VOCALIS_HOME", Path.home() / ".vocalis"))
    try:
        home.mkdir(parents=True, exist_ok=True)
        probe = home / ".doctor-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return CheckResult("permissions", True, f"{home} 可写")
    except Exception as e:
        return CheckResult(
            "permissions", False, f"{home} 不可写：{e}",
            fix="检查目录权限或设置 VOCALIS_HOME 到可写路径",
        )


async def run_checks(config: Any, probe_sapi: bool = False) -> list[CheckResult]:
    """Run all diagnostics; never raises.

    ``probe_sapi`` runs the real SAPI voice count (in a subprocess). Off by
    default so library/test callers never risk a COM hard-crash.
    """
    results = [
        check_python(),
        CheckResult("vocalis", True, f"v{vocalis.__version__}"),
        check_config(),
        check_permissions(),
        check_audio(),
        check_voice_gate(config),
        check_asr_cache(config),
        await check_brain(config),
        *check_tts(config, probe_sapi=probe_sapi),
        check_network_boundary(config),
    ]
    return results
