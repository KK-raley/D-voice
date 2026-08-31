"""Vocalis command-line interface.

Usage:
    vocalis enroll --user alice        # register your voice (3+ utterances)
    vocalis gate --file cmd.wav        # verify a speaker before dispatch
    vocalis record-samples --out s/    # collect calibrate samples (self/impostor)
    vocalis calibrate --self-dir me/ --impostor-dir others/  # FAR/FRR threshold tuning (G7)
    vocalis doctor                     # local environment diagnosis (P0)
    vocalis speak "hello" --profile aria
    vocalis listen                     # wait for "hey D-VOICE" (Ctrl+C to stop)
    vocalis talk                       # authenticated voice conversation
    vocalis run "refactor the tests" --agent claude-code
    vocalis ask "what's the status?"
    vocalis agents                     # list connectors
    vocalis stress                     # record long-run standby metrics (P0)
    vocalis serve --port 8642          # start HUD backend
"""

from __future__ import annotations

import asyncio
import json
import os
import wave
from dataclasses import replace
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

import vocalis
from vocalis.agents.registry import build_default_registry
from vocalis.config import VocalisConfig
from vocalis.dvoice.assistant import DVoiceBrain
from vocalis.dvoice.commander import Commander
from vocalis.server.confirmations import ConfirmationService
from vocalis.voice.audio import load_wav, record, resample, save_wav
from vocalis.voice.calibrate import evaluate_thresholds
from vocalis.voice.gate import VoiceGate
from vocalis.voice.speaker import SpeakerEncoderError

app = typer.Typer(
    name="vocalis",
    help="Voice-first D-VOICE agent ecosystem.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


def _needs_voice_stack(*modules: str) -> None:
    import importlib

    missing = []
    for mod in modules:
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        console.print(
            f"[red]Missing voice dependencies: {', '.join(missing)}.[/]\n"
            "Install them with: pip install 'vocalis-voice-agent[voice]'"
        )
        raise typer.Exit(1)


# ----------------------------------------------------------------------
# Enrollment & gating
# ----------------------------------------------------------------------
@app.command()
def enroll(
    user: str = typer.Option(..., "--user", "-u", help="Profile name to create"),
    rounds: int = typer.Option(3, "--rounds", "-r", help="Number of calibration utterances"),
    seconds: float = typer.Option(3.5, "--seconds", "-s", help="Seconds per utterance"),
) -> None:
    """Register your voice so Vocalis only obeys you."""
    _needs_voice_stack("sounddevice")
    try:
        gate = VoiceGate()
    except SpeakerEncoderError as exc:
        console.print(f"[red]Voice backend unavailable:[/] {escape(str(exc))}")
        raise typer.Exit(1) from exc
    utterances = []
    console.print(
        Panel(f"Enrolling [bold]{user}[/] - speak naturally, {rounds} takes of {seconds}s")
    )
    for i in range(rounds):
        console.print(f"[cyan]Take {i + 1}/{rounds}[/] - press Enter, then speak...", end="")
        input()
        audio = record(seconds=seconds)
        utterances.append(audio)
        console.print("  [green]captured[/]")
    try:
        result = gate.enroll(user, utterances)
        console.print(
            Panel.fit(
                f"[green]Enrolled.[/] consistency={result['consistency']}",
                title=f"Voice profile: {user}",
            )
        )
    except Exception as e:
        console.print(f"[red]Enrollment failed:[/] {e}")
        raise typer.Exit(1) from e


@app.command()
def gate(
    file: Path = typer.Option(..., "--file", "-f", help="WAV file to verify"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Verify whether a recorded utterance belongs to an enrolled user."""
    audio, sr = load_wav(file)
    try:
        decision = VoiceGate().verify(audio, sample_rate=sr)
    except RuntimeError as e:
        console.print(f"[yellow]{e}[/]")
        raise typer.Exit(1) from e
    if json_out:
        console.print_json(json.dumps(decision.to_dict()))
        return
    style = "green" if decision.accepted else "red"
    console.print(
        Panel.fit(
            f"[{style}]{'ACCEPTED' if decision.accepted else 'REJECTED'}[/]\n"
            f"user: {decision.user or 'unknown'}\n"
            f"similarity: {decision.similarity:.3f} (threshold {decision.threshold:.2f})\n"
            f"sample rate: {sr}",
            title="VoiceGate",
        )
    )


# ----------------------------------------------------------------------
# Voiceprint threshold calibration (G7 FAR/FRR harness)
# ----------------------------------------------------------------------
def _parse_thresholds(raw: str | None) -> list[float]:
    """解析 --thresholds 逗号列表；缺省用 0.30-0.90 步长 0.05。"""
    if raw is None:
        return [round(0.30 + 0.05 * i, 2) for i in range(13)]
    try:
        values = [float(x) for x in raw.split(",") if x.strip()]
    except ValueError:
        console.print(
            f"[red]无效的 --thresholds：{escape(raw)}[/]"
            "（应为逗号分隔的小数，如 0.4,0.5,0.6）"
        )
        raise typer.Exit(1) from None
    if not values:
        console.print("[red]--thresholds 不能为空（如 0.4,0.5,0.6）[/]")
        raise typer.Exit(1)
    return values


def _load_wav_dir(directory: Path, flag: str) -> list:
    """读取目录下全部 WAV（统一重采样到 16k 单声道）。

    目录缺失、没有 WAV 文件或文件无法解析时给出友好错误并退出，
    而不是抛出裸异常。
    """
    if not directory.is_dir():
        console.print(f"[red]目录不存在（{flag}）：{directory}[/]")
        raise typer.Exit(1)
    files = sorted(directory.glob("*.wav"))
    if not files:
        console.print(
            f"[red]{flag} 目录中没有 WAV 文件：{directory}（需要 16 位 PCM）[/]"
        )
        raise typer.Exit(1)
    audios = []
    for path in files:
        try:
            audio, sample_rate = load_wav(path)
        except (ValueError, wave.Error) as e:
            console.print(f"[red]无法读取 {path.name}：{escape(str(e))}[/]")
            raise typer.Exit(1) from e
        audios.append(resample(audio, sample_rate))
    return audios


@app.command()
def calibrate(
    self_dir: Path = typer.Option(
        ..., "--self-dir", help="本人（已注册用户）的 WAV 音频目录"
    ),
    impostor_dir: Path = typer.Option(
        ..., "--impostor-dir", help="冒充者（其他说话人）的 WAV 音频目录"
    ),
    thresholds: str = typer.Option(
        None,
        "--thresholds",
        "-t",
        help="逗号分隔的候选阈值（默认 0.30-0.90，步长 0.05）",
    ),
    json_out: bool = typer.Option(False, "--json", help="输出 JSON 报告"),
) -> None:
    """校准声纹阈值：逐阈值评估 FAR/FRR 并推荐最优值（G7）。"""
    candidates = _parse_thresholds(thresholds)
    self_audios = _load_wav_dir(self_dir, "--self-dir")
    impostor_audios = _load_wav_dir(impostor_dir, "--impostor-dir")

    try:
        gate = VoiceGate()
        report = evaluate_thresholds(gate, self_audios, impostor_audios, candidates)
    except SpeakerEncoderError as e:
        console.print(f"[red]声纹后端不可用：[/]{escape(str(e))}")
        raise typer.Exit(1) from e
    except (RuntimeError, ValueError) as e:  # 未注册声纹 / 音频太短等
        console.print(f"[red]校准失败：[/]{escape(str(e))}")
        raise typer.Exit(1) from e

    if json_out:
        console.print_json(json.dumps(report.to_dict(), ensure_ascii=False))
        return

    table = Table(title="声纹阈值校准（G7）")
    table.add_column("threshold", style="cyan", justify="right")
    table.add_column("FAR 误接受率", justify="right")
    table.add_column("FRR 误拒绝率", justify="right")
    table.add_column("|FAR-FRR|", justify="right")
    table.add_column("")
    for row in report.results:
        table.add_row(
            f"{row.threshold:g}",
            f"{row.far * 100:.1f}%",
            f"{row.frr * 100:.1f}%",
            f"{row.eer_distance * 100:.1f}%",
            "[green]推荐[/]" if row is report.recommended else "",
        )
    console.print(table)
    console.print(Panel(report.summary(), title="校准报告"))


# ----------------------------------------------------------------------
# Voice out
# ----------------------------------------------------------------------
@app.command()
def speak(
    text: str = typer.Argument(..., help="Text to speak"),
    profile: str = typer.Option(None, "--profile", "-p", help="Voice profile name"),
) -> None:
    """Speak text with a tunable voice profile (Edge-TTS)."""
    from vocalis.voice.tts import TTSService

    result = asyncio.run(TTSService().speak(text, profile, play=True))
    if result.ok:
        console.print(f"[green]Spoken with profile '{profile or 'default'}'[/] ({result.engine})")
    else:
        console.print(f"[red]Speech failed:[/] {result.error}")
        raise typer.Exit(1)


# ----------------------------------------------------------------------
# Authenticated, always-on local voice butler
# ----------------------------------------------------------------------
async def _run_butler(config: VocalisConfig, profile: str | None,
                      min_pause: float, adaptive: bool, speak_replies: bool) -> None:
    from vocalis.voice.asr import Transcriber
    from vocalis.voice.standby import StandbySession, run_microphone
    from vocalis.voice.tts import TTSService

    # Load only the configured available encoder; there is no insecure fallback.
    gate = await asyncio.to_thread(VoiceGate, config)
    if not gate.profile_count():
        raise RuntimeError("No enrolled voice for this backend. Run vocalis enroll --user YOUR_NAME first.")
    registry = build_default_registry()
    brain = DVoiceBrain(config=config, registry=registry)
    # P0-2: the CLI butler must not bypass the high-risk confirmation gate.
    # Without a HUD card the console becomes the approval surface.
    confirmations = ConfirmationService()
    commander = Commander(registry, brain, confirmations=confirmations)
    session = StandbySession(config, gate, Transcriber(config.asr), commander)
    tts = TTSService(config) if speak_replies else None

    async def report(result):
        if result["kind"] in ("wake", "command", "sleep"):
            if result["text"]:
                console.print(f"[cyan]you:[/] {escape(result['text'])}")
            reply = result.get("reply", "")
            if reply:
                console.print(Panel(escape(reply), title="D-VOICE"))
                if tts:
                    spoken = await tts.speak(reply, profile, play=True)
                    if not spoken.ok:
                        console.print("[yellow]Voice output unavailable; reply shown above.[/]")
        elif result["kind"] == "confirmation":
            for action in result["confirmation"]["actions"]:
                console.print(
                    f"[yellow]high-risk action requires confirmation[/] "
                    f"{escape(action['agent'])}: {escape(action['instruction'])}"
                )
            try:
                approved = await asyncio.to_thread(
                    typer.confirm, "Approve the actions above?", default=False
                )
            except (typer.Abort, EOFError):
                approved = False
            if approved:
                plan = await confirmations.resolve(result["confirmation"]["id"], approved=True)
                if plan is not None:
                    executed = await commander.execute_plan(plan)
                    reply = executed.get("reply", "")
                    if reply:
                        console.print(Panel(escape(reply), title="D-VOICE"))
                        if tts:
                            await tts.speak(reply, profile, play=True)
        elif result["kind"] == "error":
            console.print("[red]Local voice processing failed; returned to standby. Check local ASR/speaker models.[/]")
        elif result["kind"] == "rejected":
            console.print(f"[dim]Ignored audio: {escape(result['reason'])}[/]")

    console.print(Panel(
        "Standby: local microphone + VAD + speaker verification + ASR only.\n"
        "No brain/agent calls while asleep. Say an enrolled voice's wake phrase,\n"
        "wait for the acknowledgement, then speak your command. Every turn is verified.\n"
        f"Sleep after {config.standby.idle_timeout_s:g}s idle or say 休眠 / go to sleep.\n"
        "Half duplex: microphone input during processing/playback is discarded. Ctrl+C to stop.",
        title="D-VOICE authenticated butler",
    ))
    await run_microphone(session, report, asyncio.Event(), min_pause_s=min_pause, adaptive=adaptive)


def _start_butler(config, profile=None, min_pause=0.8, adaptive=False, speak_replies=True):
    if not config.wake_word.enabled:
        console.print("[red]Wake detection is disabled. Enable wake_word.enabled before listening.[/]")
        raise typer.Exit(1)
    _needs_voice_stack("sounddevice", "faster_whisper")
    try:
        asyncio.run(_run_butler(config, profile, min_pause, adaptive, speak_replies))
    except KeyboardInterrupt:
        console.print("\n[dim]Microphone closed. D-VOICE stopped.[/]")
    except Exception as exc:
        console.print(f"[red]Cannot start authenticated listening:[/] {escape(str(exc))}")
        raise typer.Exit(1) from exc


@app.command()
def listen(
    phrase: list[str] = typer.Option(None, "--phrase", "-p", help="Repeatable wake phrase override"),
    speak_replies: bool = typer.Option(True, "--speak/--text-only", help="Speak accepted replies"),
) -> None:
    """Always-on butler: enrolled speaker + wake word, then verified commands."""
    config = VocalisConfig.load()
    if phrase:
        config.wake_word = replace(config.wake_word, phrases=list(phrase))
    _start_butler(config, speak_replies=speak_replies)


@app.command()
def talk(
    profile: str = typer.Option(None, "--profile", "-p", help="Voice profile for replies"),
    min_pause: float = typer.Option(0.8, "--min-pause", min=0.2, max=3.0,
                                   help="Silence (s) before completing a turn"),
    max_turn: float = typer.Option(15.0, "--max-turn", min=2.0, max=60.0,
                                  help="Reject longer turns (seconds)"),
    adaptive: bool = typer.Option(False, "--adaptive", help="Adaptive local VAD noise floor"),
    speak_replies: bool = typer.Option(True, "--speak/--text-only", help="Speak accepted replies"),
) -> None:
    """Secure voice conversation; same wake/speaker checks as listen."""
    config = VocalisConfig.load()
    config.standby = replace(config.standby, max_utterance_s=max_turn)
    _start_butler(config, profile, min_pause, adaptive, speak_replies)


# ----------------------------------------------------------------------
# Agents & commands
# ----------------------------------------------------------------------
@app.command("local-qwen")
def local_qwen(
    start: bool = typer.Option(False, "--start", help="Start local llama.cpp after configuring"),
    check: bool = typer.Option(False, "--check", help="Read-only health check; never save or start"),
    deployment_dir: Path = typer.Option(None, "--deployment-dir", help="Existing Qwen deployment folder"),
    model_file: str = typer.Option(None, "--model-file", help="GGUF path relative to deployment folder"),
    local_audio: bool = typer.Option(False, "--local-audio", help="Use Windows SAPI and disable TTS sidecar"),
) -> None:
    """Configure existing local Qwen (default), optionally start, or check without writes."""
    from vocalis.dvoice.local_qwen import (
        configure_local_qwen,
        ensure_local_qwen,
        inspect_local_qwen,
        probe_local_qwen,
    )

    if check and (start or local_audio):
        raise typer.BadParameter("--check cannot be combined with --start or --local-audio")
    try:
        # VocalisConfig.load() creates a config/home on first use, so a read-only
        # check must parse existing settings without invoking that helper.
        config_path = Path(os.environ.get("VOCALIS_HOME", Path.home() / ".vocalis")) / "config.toml"
        config = VocalisConfig()
        if config_path.is_file():
            try:
                import tomllib
            except ImportError:  # Python 3.10
                import tomli as tomllib
            config = VocalisConfig.from_dict(tomllib.loads(config_path.read_text(encoding="utf-8")))
        if check:
            if deployment_dir is not None:
                config.brain.deployment_dir = str(deployment_dir)
            if model_file is not None:
                config.brain.model_file = model_file
                config.brain.model = Path(model_file).name
            result = asyncio.run(probe_local_qwen(config.brain))
        else:
            configure_local_qwen(config, deployment_dir=deployment_dir, model_file=model_file)
            if local_audio:
                if os.name != "nt":
                    raise ValueError("--local-audio selects Windows SAPI; it requires Windows")
                config.tts.engine = "sapi"
                config.sidecar.enabled = False
            # Validate paths before writing the migrated configuration.
            # An unconfigured deployment_dir is allowed: the user may set it
            # later; starting/checking will report not_configured clearly.
            if config.brain.deployment_dir:
                result = inspect_local_qwen(config.brain)
            else:
                result = {}
            saved_path = config.save()
            if start:
                result = asyncio.run(ensure_local_qwen(config.brain))
            else:
                result.update(status="configured", available=None)
            result["config_path"] = str(saved_path)
            result["tts_engine"] = config.tts.engine
        console.print_json(json.dumps(result, ensure_ascii=False))
        if (start or check) and not result["available"]:
            raise typer.Exit(1)
    except (OSError, ValueError) as exc:
        console.print(f"[red]Local Qwen setup failed:[/] {escape(str(exc))}")
        raise typer.Exit(1) from exc


@app.command()
def agents() -> None:
    """List registered agent connectors."""
    registry = build_default_registry()
    table = Table(title="Vocalis Agents")
    table.add_column("name", style="cyan")
    table.add_column("status")
    table.add_column("description")
    for a in registry.list():
        icon = {"idle": "[green]●[/]", "busy": "[yellow]●[/]", "error": "[red]●[/]", "offline": "[dim]○[/]"}[
            a["status"]
        ]
        table.add_row(a["name"], f"{icon} {a['status']}", a["description"])
    console.print(table)


@app.command()
def run(
    instruction: str = typer.Argument(..., help="What the agent should do"),
    agent: str = typer.Option(None, "--agent", "-a", help="Connector name (default: echo)"),
    verify_voice: bool = typer.Option(False, "--verify", help="Require VoiceGate acceptance first"),
) -> None:
    """Dispatch a task to an agent with live progress narration."""
    if verify_voice:
        _needs_voice_stack("resemblyzer", "sounddevice")
        try:
            decision = VoiceGate().verify(record(seconds=4.0))
        except RuntimeError as e:
            console.print(f"[yellow]{e}[/]")
            raise typer.Exit(1) from e
        if not decision.accepted:
            console.print(f"[red]VoiceGate rejected input[/] (sim={decision.similarity:.2f})")
            raise typer.Exit(1)
        console.print(f"[green]Voice accepted:[/] {decision.user}")
    registry = build_default_registry()
    with console.status("[bold cyan]agent working...[/]"):
        record_result = asyncio.run(registry.dispatch(agent, instruction))
    if record_result.status.value == "completed":
        console.print(Panel(record_result.output[:2000], title=f"[green]{record_result.agent} - completed[/]"))
    else:
        console.print(Panel(record_result.error or "failed", title=f"[red]{record_result.agent} - failed[/]"))


@app.command()
def ask(
    question: str = typer.Argument(..., help="Question for the local D-VOICE brain"),
) -> None:
    """Ask the local small model anything."""
    registry = build_default_registry()
    brain = DVoiceBrain(registry=registry)
    answer = asyncio.run(brain.chat(question))
    console.print(Panel(answer, title="D-VOICE"))


@app.command()
def status() -> None:
    """Show live system status (agents, tasks)."""
    registry = build_default_registry()
    snap = registry.snapshot()
    console.print(
        Panel(
            "\n".join(
                f"[bold]{a['name']}[/] - {a['status']} - {a['description']}"
                for a in snap["agents"]
            )
            or "no agents",
            title=f"Vocalis v{vocalis.__version__}",
        )
    )
    commander = Commander(registry)
    console.print(Panel(asyncio.run(commander.execute("status"))["reply"], title="Report"))


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8642, "--port"),
    reload: bool = typer.Option(False, "--reload", help="Dev hot-reload"),
) -> None:
    """Start the HTTP/WS backend for the HUD."""
    console.print(f"[bold]Vocalis[/] v{vocalis.__version__} serving on http://{host}:{port}")
    import uvicorn

    uvicorn.run("vocalis.server.app:app", host=host, port=port, reload=reload)


@app.command("config")
def config_show() -> None:
    """Dump the effective configuration."""
    console.print_json(json.dumps(VocalisConfig.load().to_dict(), ensure_ascii=False))


# ----------------------------------------------------------------------
# P0-4 doctor / P0-1 sample collection / P0-3 stress metrics
# ----------------------------------------------------------------------
@app.command()
def doctor(json_out: bool = typer.Option(False, "--json", help="输出 JSON 诊断报告")) -> None:
    """Diagnose mic, voiceprint, ASR cache, brain, TTS, permissions & network."""
    from vocalis.doctor import run_checks

    config = VocalisConfig.load()
    results = asyncio.run(run_checks(config, probe_sapi=True))
    if json_out:
        console.print_json(json.dumps([r.to_dict() for r in results], ensure_ascii=False))
    else:
        table = Table(title=f"vocalis doctor · v{vocalis.__version__}")
        table.add_column("检查项", style="cyan")
        table.add_column("状态", justify="center")
        table.add_column("详情")
        table.add_column("修复建议", style="dim")
        failed = 0
        for r in results:
            mark = "[green]✓[/]" if r.ok else ("[yellow]![/]" if r.ok is None else "[red]✗[/]")
            if r.ok is False:
                failed += 1
            table.add_row(r.name, mark, r.detail, r.fix)
        console.print(table)
        if failed:
            console.print(f"[red]{failed} 项未通过[/] —— 按修复建议处理后重跑。")
            raise typer.Exit(1)
        console.print("[green]全部检查通过。[/]")


@app.command("record-samples")
def record_samples(
    out: Path = typer.Option(..., "--out", "-o", help="WAV 输出目录"),
    role: str = typer.Option("self", "--role", help="self=本人样本 / impostor=冒充者样本"),
    takes: int = typer.Option(8, "--takes", "-n", min=1, max=60),
    seconds: float = typer.Option(4.0, "--seconds", "-s", min=1.0, max=15.0),
) -> None:
    """采集声纹校准样本（供 vocalis calibrate 使用；含环境多样性指引）。"""
    _needs_voice_stack("sounddevice")
    if role not in ("self", "impostor"):
        console.print("[red]--role 只能是 self 或 impostor[/]")
        raise typer.Exit(1)
    scenarios = [
        "正常距离（30-50cm）安静环境",
        "稍远距离（1m 左右）",
        "正常语速说一段完整句子",
        "稍快语速",
        "轻声/低音量",
        "正常音量 + 背景音乐（电视/音箱小声播放）",
        "转头或侧对麦克风说话",
        "句尾自然拖长（模拟重放变化的口语习惯）",
    ]
    console.print(Panel(
        f"采集 [bold]{role}[/] 样本 {takes} 条 × {seconds}s -> {out}\n"
        "覆盖不同距离 / 音量 / 噪声，越接近真实使用越好。\n"
        "（重放测试可用手机先录下本人语音，对麦克风播放后录为 impostor 或 self 佐证样本）",
        title="声纹样本采集",
    ))
    saved: list[Path] = []
    for i in range(takes):
        tip = scenarios[i % len(scenarios)]
        console.print(f"[cyan]Take {i + 1}/{takes}[/] - {escape(tip)} · 按 Enter 开始...", end="")
        input()
        audio = record(seconds=seconds)
        path = save_wav(out / f"{role}_{i + 1:02d}.wav", audio)
        saved.append(path)
        console.print(f"  [green]saved[/] {path.name}")
    console.print(Panel(
        f"完成 {len(saved)} 条。\n下一步：\n"
        f"  vocalis calibrate --self-dir <本人目录> --impostor-dir <冒充者目录>",
        title="采集完成",
    ))


@app.command()
def stress(
    interval: float = typer.Option(60.0, "--interval", help="采样间隔（秒）"),
    duration: float = typer.Option(0.0, "--duration", help="运行时长（秒）；0=直到 Ctrl+C"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """记录长时待机指标（内存/CPU/线程/大脑可用性；不保存任何音频）。"""
    from vocalis.monitor.stress import StressRecorder, metrics_path, summarize

    async def _run() -> None:
        recorder = StressRecorder(interval_s=max(5.0, interval))
        console.print(
            Panel(
                f"采样间隔 {recorder.interval_s:g}s -> {metrics_path()}\n"
                "Ctrl+C 停止。8 小时验收建议：挂机后正常使用，结束后查看 vocalis stress-report。",
                title="Stress recording",
            )
        )
        await recorder.start()
        try:
            if duration > 0:
                await asyncio.sleep(duration)
            else:
                stop = asyncio.Event()
                loop = asyncio.get_running_loop()
                import signal

                for sig in (signal.SIGINT, signal.SIGTERM):
                    try:
                        loop.add_signal_handler(sig, stop.set)
                    except NotImplementedError:  # Windows SIGTERM
                        pass
                while not stop.is_set():
                    await asyncio.sleep(0.5)
        finally:
            await recorder.stop()
        report = summarize()
        if json_out:
            console.print_json(json.dumps(report, ensure_ascii=False))
        else:
            console.print(Panel(
                json.dumps(report, ensure_ascii=False, indent=2)[:1200],
                title="Stress summary",
            ))

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        console.print("\n[dim]Stress recording stopped.[/]")


@app.command("stress-report")
def stress_report(file: Path = typer.Option(None, "--file", help="metrics.jsonl 路径"),
                  json_out: bool = typer.Option(False, "--json")) -> None:
    """汇总 stress 指标文件：内存峰值 / CPU 均值 / 大脑在线率。"""
    from vocalis.monitor.stress import metrics_path, summarize

    report = summarize(file or metrics_path())
    if json_out:
        console.print_json(json.dumps(report, ensure_ascii=False))
        return
    if report.get("samples", 0) == 0:
        console.print("[yellow]没有采样数据。先运行 vocalis stress 挂机记录。[/]")
        return
    table = Table(title="8 小时待机压力汇总")
    table.add_column("指标", style="cyan")
    table.add_column("值", justify="right")
    table.add_row("samples", str(report["samples"]))
    table.add_row("duration (s)", f"{report.get('duration_s', 0):g}")
    for key in ("rss_mb", "cpu_percent"):
        s = report.get(key)
        if s:
            table.add_row(key, f"min {s['min']} · avg {s['avg']} · max {s['max']}")
    if report.get("threads_max") is not None:
        table.add_row("threads (max)", str(report["threads_max"]))
    if report.get("brain_uptime_percent") is not None:
        table.add_row("brain online %", str(report["brain_uptime_percent"]))
    console.print(table)


if __name__ == "__main__":
    app()
