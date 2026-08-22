"""Vocalis command-line interface.

Usage:
    vocalis enroll --user alice        # register your voice (3+ utterances)
    vocalis gate --file cmd.wav        # verify a speaker before dispatch
    vocalis speak "hello" --profile aria
    vocalis listen                     # wait for "hey D-VOICE" (Ctrl+C to stop)
    vocalis run "refactor the tests" --agent claude-code
    vocalis ask "what's the status?"
    vocalis agents                     # list connectors
    vocalis serve --port 8642          # start HUD backend
"""

from __future__ import annotations

import asyncio
import importlib
import json
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
from vocalis.voice.audio import load_wav, record
from vocalis.voice.gate import VoiceGate
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
