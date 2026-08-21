"""Vocalis HTTP/WebSocket server.

Endpoints:
  GET  /api/health          - liveness + subsystem availability
  GET  /api/status          - live agents/tasks snapshot (monitor view)
  GET  /api/agents          - registered connectors
  GET  /api/voice/profiles  - TTS voice profiles
  POST /api/voice/profiles  - upsert a voice profile
  POST /api/command         - text command through the Commander pipeline
  POST /api/ask             - direct question to the Jarvis brain
  POST /api/speak           - server-side TTS of arbitrary text
  GET  /api/events/history  - recent event-bus history
  WS   /ws                  - live event stream for the HUD
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import vocalis
from vocalis.agents.registry import AgentRegistry, build_default_registry
from vocalis.config import VocalisConfig
from vocalis.jarvis.assistant import JarvisBrain
from vocalis.jarvis.commander import Commander
from vocalis.jarvis.monitor import TaskMonitor
from vocalis.notify.notifier import Notifier
from vocalis.server.events import EventBus, EventType, bus
from vocalis.voice.gate import VoiceGate
from vocalis.voice.tts import TTSService, VoiceProfile


class AppState:
    def __init__(self) -> None:
        self.config = VocalisConfig.load()
        self.event_bus = bus
        self.registry: AgentRegistry = build_default_registry(bus)
        self.tts = TTSService(self.config, bus)
        self.brain = JarvisBrain(self.config, self.registry, bus)
        self.notifier = Notifier(self.tts, bus)
        self.monitor = TaskMonitor(
            self.config,
            bus,
            on_completion=self.notifier.notify_task,
        )
        self.commander = Commander(self.registry, self.brain, bus)


state: AppState | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global state
    state = AppState()
    await state.monitor.start()
    await state.event_bus.publish(EventType.SYSTEM, message=f"Vocalis v{vocalis.__version__} online")
    yield
    await state.monitor.stop()


app = FastAPI(
    title="Vocalis",
    version=vocalis.__version__,
    description="Voice-first JARVIS-style agent ecosystem",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ----------------------------------------------------------------------
# REST
# ----------------------------------------------------------------------
@app.get("/api/health")
async def health() -> dict[str, Any]:
    assert state is not None
    return {
        "ok": True,
        "version": vocalis.__version__,
        "brain": await state.brain.available(),
        "agents": len(state.registry.connectors),
    }


@app.get("/api/status")
async def status() -> dict[str, Any]:
    assert state is not None
    return {
        **state.registry.snapshot(),
        "live": state.monitor.live_view(),
    }


@app.get("/api/agents")
async def agents() -> list[dict[str, Any]]:
    assert state is not None
    return state.registry.list()


@app.get("/api/voice/profiles")
async def voice_profiles() -> dict[str, Any]:
    assert state is not None
    return {name: p.to_dict() for name, p in state.tts.profiles().items()}


class ProfileBody(BaseModel):
    name: str
    voice: str
    rate: str = "+0%"
    pitch: str = "+0Hz"
    volume: str = "+0%"


@app.post("/api/voice/profiles")
async def upsert_profile(body: ProfileBody) -> dict[str, Any]:
    assert state is not None
    profile = VoiceProfile(
        name=body.name,
        voice=body.voice,
        rate=body.rate,
        pitch=body.pitch,
        volume=body.volume,
    )
    state.tts.upsert_profile(profile)
    return {"ok": True, "profile": profile.to_dict()}


class CommandBody(BaseModel):
    text: str
    speak: bool = True


@app.post("/api/command")
async def command(body: CommandBody) -> dict[str, Any]:
    assert state is not None
    result = await state.commander.execute(body.text)
    reply = result.get("reply") or _task_reply(result)
    if body.speak and reply:
        await state.tts.speak(reply)
    return {**result, "spoken": reply}


class AskBody(BaseModel):
    text: str


@app.post("/api/ask")
async def ask(body: AskBody) -> dict[str, Any]:
    assert state is not None
    snapshot = state.registry.snapshot()
    reply = await state.brain.chat(
        body.text,
        context={"active_tasks": len(snapshot["active_tasks"]), "agents": len(snapshot["agents"])},
    )
    await state.event_bus.publish(EventType.JARVIS_SAYING, text=reply)
    return {"reply": reply}


class SpeakBody(BaseModel):
    text: str
    profile: str | None = None


@app.post("/api/speak")
async def speak(body: SpeakBody) -> dict[str, Any]:
    assert state is not None
    result = await state.tts.speak(body.text, body.profile)
    return {"ok": result.ok, "engine": result.engine, "error": result.error}


@app.get("/api/events/history")
async def event_history() -> list[dict[str, Any]]:
    assert state is not None
    return [e.to_dict() for e in state.event_bus.history[-100:]]


def _task_reply(result: dict[str, Any]) -> str:
    if "task" in result:
        t = result["task"]
        return f"{t['agent']} finished: {t['instruction']}" if t["status"] == "completed" else f"{t['agent']} failed."
    if "tasks" in result:
        done = sum(1 for t in result["tasks"] if t["status"] == "completed")
        return f"All {done} of {len(result['tasks'])} tasks finished."
    return ""


# ----------------------------------------------------------------------
# WebSocket: live event stream
# ----------------------------------------------------------------------
@app.websocket("/ws")
async def ws_events(ws: WebSocket) -> None:
    assert state is not None
    await ws.accept()
    queue = state.event_bus.subscribe("*")
    try:
        hello = {"type": "system.ready", "data": {"version": vocalis.__version__}}
        await ws.send_json(hello)
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=15.0)
            except asyncio.TimeoutError:
                await ws.send_json({"type": "system.ping", "data": {}})
                continue
            await ws.send_json(event.to_dict())
    except WebSocketDisconnect:
        pass
    finally:
        state.event_bus.unsubscribe(queue)
