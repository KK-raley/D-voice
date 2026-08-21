"""Vocalis command-line interface.

Usage:
    vocalis enroll --user alice        # register your voice (3+ utterances)
    vocalis gate --file cmd.wav        # verify a speaker before dispatch
    vocalis speak "hello" --profile aria
    vocalis run "refactor the tests" --agent claude-code
    vocalis ask "what's the status?"
    vocalis agents                     # list connectors
    vocalis serve --port 8642          # start HUD backend
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

import vocalis
from vocalis.agents.registry import build_default_registry
from vocalis.config import VocalisConfig
from vocalis.dvoice.assistant import DVoiceBrain
from vocalis.dvoice.commander import Commander
from vocalis.voice.audio import load_wav, record
from vocalis.voice.gate import VoiceGate

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
