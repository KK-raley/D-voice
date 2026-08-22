"""Vocalis command-line interface.

Usage:
    vocalis enroll --user alice        # register your voice (3+ utterances)
    vocalis gate --file cmd.wav        # verify a speaker before dispatch
    vocalis calibrate --self-dir me/ --impostor-dir others/  # FAR/FRR threshold tuning (G7)
    vocalis speak "hello" --profile aria
    vocalis listen                     # wait for "hey D-VOICE" (Ctrl+C to stop)
    vocalis talk                       # full-duplex voice chat (VAD/turns/barge-in)
    vocalis run "refactor the tests" --agent claude-code
    vocalis ask "what's the status?"
    vocalis agents                     # list connectors
    vocalis serve --port 8642          # start HUD backend
"""

from __future__ import annotations

import asyncio
import importlib
import json
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
from vocalis.config import VocalisConfig, WakeWordConfig
from vocalis.dvoice.assistant import DVoiceBrain
from vocalis.dvoice.commander import Commander
from vocalis.server.events import EventType, bus
from vocalis.voice.audio import load_wav, record, resample
from vocalis.voice.calibrate import evaluate_thresholds
from vocalis.voice.gate import VoiceGate
from vocalis.voice.speaker import SpeakerEncoderError
from vocalis.voice.wakeword import WakeHit, WakeWordDetector

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
    _needs_voice_stack("resemblyzer", "sounddevice")
    gate = VoiceGate()
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
    _needs_voice_stack("resemblyzer")
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
        raise typer.Exit(1)
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
# Wake-word listening
# ----------------------------------------------------------------------
def _importable(module: str) -> bool:
    try:
        importlib.import_module(module)
    except ImportError:
        return False
    return True


def _mic_chunks(sample_rate: int = 16000, block_s: float = 1.0):
    """Yield mono float32 blocks from the default mic (infinite generator)."""
    import numpy as np
    import sounddevice as sd

    block_len = int(sample_rate * block_s)
    with sd.InputStream(samplerate=sample_rate, channels=1, dtype="float32") as stream:
        while True:
            frame, _overflowed = stream.read(block_len)
            chunk = np.asarray(frame)
            yield chunk.mean(axis=1) if chunk.ndim > 1 else chunk


def _report_wake_hit(hit: WakeHit) -> None:
    console.print(
        f"[bold green]Wake word detected:[/] {escape(str(hit.phrase))} "
        f"(backend={hit.backend}, score={hit.score:.2f})"
    )
    try:
        asyncio.run(
            bus.publish(
                EventType.SYSTEM,
                message=f"wake word detected: {hit.phrase}",
                phrase=hit.phrase,
                backend=hit.backend,
                score=hit.score,
            )
        )
    except Exception:  # never kill the listen loop over event plumbing
        console.print("[yellow]warning:[/] failed to publish wake-word event")


def _listen_asr(detector: WakeWordDetector, asr_config, window_s: float = 3.0) -> None:
    """Transcribe rolling audio windows and text-match the wake phrases."""
    import numpy as np

    from vocalis.voice.asr import Transcriber

    console.print("[dim]loading faster-whisper model (first pass may take a moment)...[/]")
    transcriber = Transcriber(asr_config)
    blocks_per_window = max(1, int(round(window_s)))
    window: list[np.ndarray] = []
    for chunk in _mic_chunks():
        window.append(chunk)
        if len(window) < blocks_per_window:
            continue
        audio = np.concatenate(window)
        window.clear()
        if float(np.sqrt(np.mean(np.square(audio)))) < 0.005:
            continue  # silence: skip whisper entirely
        try:
            text = transcriber.transcribe(audio, sample_rate=16000).text
        except RuntimeError as e:  # model load failure etc.
            console.print(f"[red]ASR failed:[/] {escape(str(e))}")
            raise typer.Exit(1) from e
        if text:
            console.print(f"[dim]heard: {escape(text)}[/]")
            hit = detector.process_text(text)
            if hit.detected:
                _report_wake_hit(hit)


def _listen_openwakeword(detector: WakeWordDetector) -> None:
    """Stream 16 kHz chunks straight into the openwakeword model."""
    console.print("[dim]loading openwakeword model (may download on first run)...[/]")
    for chunk in _mic_chunks():
        hit = detector.process_audio(chunk, sample_rate=16000)
        if hit.detected:
            _report_wake_hit(hit)


@app.command()
def listen(
    phrase: list[str] = typer.Option(
        None,
        "--phrase",
        "-p",
        help="Wake phrase to listen for (repeatable; replaces the config phrases)",
    ),
) -> None:
    """Listen for the wake word ("hey D-VOICE") in a loop. Ctrl+C to stop."""
    config = VocalisConfig.load()
    ww_config = config.wake_word
    if phrase:
        ww_config = replace(ww_config, phrases=list(phrase))

    detector = WakeWordDetector(ww_config)
    backend = detector.resolve_backend()
    if backend == "none":
        console.print(
            f"[red]No usable wake-word backend (configured: {ww_config.backend!r}).[/]\n"
            "Set wake_word.backend = 'asr' or 'openwakeword' in ~/.vocalis/config.toml"
        )
        raise typer.Exit(1)

    missing = []
    if not _importable("sounddevice"):
        missing.append("sounddevice")
    if backend == "asr" and not _importable("faster_whisper"):
        missing.append("faster-whisper")
    if backend == "openwakeword" and not _importable("openwakeword"):
        missing.append("openwakeword")
    if missing:
        extras = "voice" if backend == "asr" else "voice,wakeword"
        console.print(
            f"[red]Missing dependencies: {', '.join(missing)}.[/]\n"
            + escape(f"Install them with: pip install 'vocalis-voice-agent[{extras}]'")
        )
        raise typer.Exit(1)

    lines = [f"backend: [cyan]{backend}[/]"]
    if backend == "openwakeword":
        lines.append(escape(f"model: {ww_config.model} (threshold {ww_config.threshold})"))
    else:
        lines.append(escape("phrases: " + ", ".join(ww_config.phrases)))
    lines.append(f"cooldown: {ww_config.cooldown_s}s")
    console.print(Panel("\n".join(lines), title="Vocalis listening - Ctrl+C to stop"))

    try:
        if backend == "openwakeword":
            _listen_openwakeword(detector)
        else:
            _listen_asr(detector, config.asr)
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped listening.[/]")
    except Exception as e:  # mic missing / PortAudio failure: friendly exit
        console.print(f"[red]Microphone error:[/] {escape(str(e))}")
        raise typer.Exit(1) from e


# ----------------------------------------------------------------------
# Realtime full-duplex conversation
# ----------------------------------------------------------------------
@app.command()
def talk(
    profile: str = typer.Option(None, "--profile", "-p", help="Voice profile for replies"),
    min_pause: float = typer.Option(
        0.8, "--min-pause", min=0.2, max=3.0, help="Silence (s) before D-VOICE answers"
    ),
    max_turn: float = typer.Option(
        30.0, "--max-turn", min=2.0, help="Force-cut a turn longer than this (s)"
    ),
    adaptive: bool = typer.Option(
        False, "--adaptive", help="Adaptive noise-floor VAD threshold (noisy rooms)"
    ),
) -> None:
    """Full-duplex voice chat: VAD chunking + turn detection + barge-in."""
    _needs_voice_stack("sounddevice", "faster_whisper")
    from collections import deque as _deque

    from vocalis.voice.asr import Transcriber
    from vocalis.voice.audio import mic_frames
    from vocalis.voice.realtime import EnergyVAD, RealtimeSession, TurnDetector
    from vocalis.voice.tts import InterruptiblePlayer, TTSService

    config = VocalisConfig.load()
    # Headless AppState (examples/04_full_dvoice.py without monitor/notifier).
    registry = build_default_registry()
    brain = DVoiceBrain(registry=registry)
    commander = Commander(registry, brain)
    transcriber = Transcriber(config.asr)
    tts = TTSService(config)
    player = InterruptiblePlayer()
    session = RealtimeSession(
        vad=EnergyVAD(adaptive=adaptive),
        turn=TurnDetector(min_pause_s=min_pause, max_turn_s=max_turn),
    )
    pending: _deque = _deque()
    mic = mic_frames()
    min_samples = int(0.3 * 16000)

    console.print(
        Panel(
            f"min pause: [cyan]{min_pause}s[/] | max turn: [cyan]{max_turn}s[/] | "
            f"adaptive VAD: {adaptive}\n"
            "Speak naturally - short thinking pauses will not cut you off.\n"
            "Talk over D-VOICE to interrupt its reply. Ctrl+C to quit.",
            title="D-VOICE talk",
        )
    )

    def _drain(events) -> None:
        for ev in events:
            if ev.kind == "barge_in":
                player.stop()
                console.print("[yellow](已打断)[/]")
            elif ev.kind in ("utterance_end", "turn_complete"):
                if ev.audio is None or ev.audio.size < min_samples:
                    continue
                if ev.kind == "turn_complete":
                    console.print("[dim](turn force-cut)[/]")
                pending.append(ev.audio)
                if player.playing:  # a finished new utterance wins the floor
                    player.stop()

    def _respond(audio) -> None:
        try:
            text = transcriber.transcribe(audio, sample_rate=16000).text.strip()
        except RuntimeError as e:
            console.print(f"[red]ASR failed:[/] {escape(str(e))}")
            return
        if not text:
            return
        console.print(f"[cyan]you:[/] {escape(text)}")
        try:
            reply = str(asyncio.run(commander.execute(text)).get("reply", "")).strip()
        except Exception as e:  # brain/agent failure must not kill the session
            reply = f"(command failed: {e})"
        console.print(Panel(reply[:800] or "(no reply)", title="D-VOICE"))
        result = asyncio.run(tts.synthesize(reply, profile))
        if not (result.ok and result.audio_path):
            console.print("[yellow](TTS unavailable - text only; "
                          "pip install edge-tts for voice replies)[/]")
            return
        # Full-duplex: keep feeding mic frames while the reply plays so a
        # barge-in (or a completed new utterance) stops playback instantly.
        session.bot_speaking = True
        player.play(result.audio_path)
        try:
            while player.playing:
                try:
                    frame = next(mic)
                except StopIteration:
                    break
                _drain(session.feed_frame(frame))
        finally:
            session.bot_speaking = False

    try:
        for frame in mic:
            _drain(session.feed_frame(frame))
            while pending:
                _respond(pending.popleft())
    except KeyboardInterrupt:
        console.print("\n[dim]Talk session ended.[/]")
    finally:
        player.stop()


# ----------------------------------------------------------------------
# Agents & commands
# ----------------------------------------------------------------------
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


if __name__ == "__main__":
    app()
