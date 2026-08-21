"""Vocalis HTTP/WebSocket server.

Endpoints:
  GET  /api/health          - liveness + subsystem availability (cached probe)
  GET  /api/status          - live agents/tasks snapshot (monitor view)
  GET  /api/agents          - registered connectors
  GET  /api/voice/profiles  - TTS voice profiles
  POST /api/voice/profiles  - upsert a voice profile
  POST /api/command         - text command through the Commander pipeline
  POST /api/ask             - direct question to the D-VOICE brain
  POST /api/speak           - synthesize text; returns audio/mpeg for browser playback
  GET  /api/events/history  - recent event-bus history
  WS   /ws                  - live event stream for the HUD

Security: CORS is restricted to the dev HUD origin; when the VOCALIS_TOKEN
environment variable is set, every mutating request must carry it in the
X-Vocalis-Token header (or ?token= for the WebSocket).
"""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import vocalis
from vocalis.agents.registry import AgentRegistry, build_default_registry
from vocalis.config import VocalisConfig
from vocalis.dvoice.assistant import DVoiceBrain
from vocalis.dvoice.commander import Commander
from vocalis.dvoice.monitor import TaskMonitor
from vocalis.notify.notifier import Notifier
from vocalis.server.events import Event, EventBus, EventType, bus
from vocalis.voice.tts import TTSService, VoiceProfile

_REQUIRED_TOKEN = os.environ.get("VOCALIS_TOKEN", "")


def require_token(
    x_vocalis_token: str | None = Header(default=None),
) -> None:
    """No-op unless VOCALIS_TOKEN is set; then enforce it on mutating calls."""
    if _REQUIRED_TOKEN and x_vocalis_token != _REQUIRED_TOKEN:
        raise HTTPException(status_code=401, detail="invalid or missing X-Vocalis-Token")


class AppState:
    def __init__(self) -> None:
        self.config = VocalisConfig.load()
        self.event_bus = bus
        self.registry: AgentRegistry = build_default_registry(bus)
        self.tts = TTSService(self.config, bus)
        self.brain = DVoiceBrain(self.config, self.registry, bus)
        self.notifier = Notifier(self.tts, bus)
        self.monitor = TaskMonitor(
            self.config,
            bus,
            on_narration=self._narrate,
            on_completion=self.notifier.notify_task,
        )
        self.commander = Commander(self.registry, self.brain, bus)
        self._health_cache: tuple[float, dict[str, Any]] = (0.0, {})

    async def _narrate(self, text: str) -> None:
        """Milestone narration: publish to HUD + speak locally (non-blocking)."""
        await self.event_bus.publish(EventType.DVOICE_SAYING, text=text)
        await self.tts.speak(text, play=True)

    async def health(self) -> dict[str, Any]:
        """Cached liveness probe (Ollama probing can be slow when down)."""
        now = time.time()
        if now - self._health_cache[0] < 30.0:
            return self._health_cache[1]
        try:
            brain_ok = await asyncio.wait_for(self.brain.available(), timeout=3.0)
        except asyncio.TimeoutError:
            brain_ok = False
        payload = {
            "ok": True,
            "version": vocalis.__version__,
            "brain": brain_ok,
            "agents": len(self.registry.connectors),
        }
        self._health_cache = (now, payload)
        return payload


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
    description="Voice-first D-VOICE agent ecosystem",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ----------------------------------------------------------------------
# REST
# ----------------------------------------------------------------------
@app.get("/api/health")
async def health() -> dict[str, Any]:
    assert state is not None
    return await state.health()


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


@app.post("/api/voice/profiles", dependencies=[Depends(require_token)])
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


@app.post("/api/command", dependencies=[Depends(require_token)])
async def command(body: CommandBody) -> dict[str, Any]:
    assert state is not None
    result = await state.commander.execute(body.text)
    reply = result.get("reply") or _task_reply(result)
    if body.speak and reply:
        await state.tts.speak(reply, play=True)
    return {**result, "spoken": reply}


class AskBody(BaseModel):
    text: str


@app.post("/api/ask", dependencies=[Depends(require_token)])
async def ask(body: AskBody) -> dict[str, Any]:
    assert state is not None
    snapshot = state.registry.snapshot()
    reply = await state.brain.chat(
        body.text,
        context={"active_tasks": len(snapshot["active_tasks"]), "agents": len(snapshot["agents"])},
    )
    await state.event_bus.publish(EventType.DVOICE_SAYING, text=reply)
    return {"reply": reply}


class SpeakBody(BaseModel):
    text: str
    profile: str | None = None


@app.post("/api/speak")
async def speak(body: SpeakBody) -> Response:
    """Synthesize speech and return the audio so any browser can play it."""
    assert state is not None
    result = await state.tts.synthesize(body.text, body.profile)
    if not result.ok or result.audio_bytes is None:
        raise HTTPException(status_code=502, detail=result.error or "synthesis failed")
    return Response(content=result.audio_bytes, media_type="audio/mpeg")


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
async def ws_events(
    ws: WebSocket,
    token: str | None = Query(default=None),
) -> None:
    if _REQUIRED_TOKEN and token != _REQUIRED_TOKEN:
        await ws.close(code=4401)
        return
    assert state is not None
    await ws.accept()
    queue = state.event_bus.subscribe("*")
    try:
        hello = Event(
            type="system.ready", data={"version": vocalis.__version__}
        ).to_dict()
        await ws.send_json(hello)
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=15.0)
            except asyncio.TimeoutError:
                await ws.send_json(Event(type="system.ping", data={}).to_dict())
                continue
            await ws.send_json(event.to_dict())
    except WebSocketDisconnect:
        pass
    finally:
        state.event_bus.unsubscribe(queue)


# ----------------------------------------------------------------------
# Serve the built HUD when available (dev uses Vite on :5173 instead).
# ----------------------------------------------------------------------
for _candidate in (Path(__file__).resolve().parents[2] / "ui" / "dist", Path("/app/hud")):
    if _candidate.is_dir():
        app.mount("/", StaticFiles(directory=str(_candidate), html=True), name="hud")
        break
