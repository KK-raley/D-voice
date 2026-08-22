"""Vocalis HTTP/WebSocket server.

Endpoints:
  GET  /api/health          - liveness + subsystem availability (cached probe)
  GET  /api/status          - live agents/tasks snapshot (monitor view)
  GET  /api/agents          - registered connectors
  GET  /api/voices          - Edge-TTS voice catalog (locale/gender filters, cached)
  GET  /api/voice/profiles  - TTS voice profiles
  POST /api/voice/profiles  - upsert a voice profile
  GET  /api/voice/presets   - scenario presets (focus/evening/presentation)
  POST /api/voice/presets   - apply a preset (upsert profile + set default)
  POST /api/command         - text command through the Commander pipeline
  POST /api/ask             - direct question to the D-VOICE brain
  POST /api/speak           - synthesize text; returns audio/mpeg for browser playback
  GET  /api/events/history  - recent event-bus history
  WS   /ws                  - live event stream for the HUD
                              (replays bus history on connect, ?replay=N caps it)

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
from vocalis.voice.tts import TTSService, VoiceProfile, filter_voices, list_voices

_REQUIRED_TOKEN = os.environ.get("VOCALIS_TOKEN", "")


def require_token(
    x_vocalis_token: str | None = Header(default=None),
) -> None:
    """No-op unless VOCALIS_TOKEN is set; then enforce it on mutating calls."""
    if _REQUIRED_TOKEN and x_vocalis_token != _REQUIRED_TOKEN:
        raise HTTPException(status_code=401, detail="invalid or missing X-Vocalis-Token")


# Voice catalog cache: simple dict + timestamp so /api/voices does not hit
# the Edge endpoint on every request (TTL = 5 minutes).
_VOICE_CACHE_TTL = 300.0
_voice_cache: dict[str, Any] = {"ts": 0.0, "voices": []}


async def _cached_voices() -> list[dict[str, Any]]:
    if _voice_cache["voices"] and time.time() - _voice_cache["ts"] < _VOICE_CACHE_TTL:
        return _voice_cache["voices"]
    voices = await list_voices()
    _voice_cache["ts"] = time.time()
    _voice_cache["voices"] = voices
    return voices


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

    async def _narrate(self, text: str, agent: str | None = None) -> None:
        """Milestone narration: publish to HUD + speak locally (non-blocking).

        When ``agent`` is known the narration uses that agent's mapped
        voice (``config.tts.agent_voices``) so parallel tasks are
        distinguishable by ear. TaskMonitor's on_narration callback passes
        text only, which keeps the default voice - fully compatible.
        """
        await self.event_bus.publish(EventType.DVOICE_SAYING, text=text)
        profile = self.tts.profile_for_agent(agent) if agent else None
        await self.tts.speak(text, profile_name=profile, play=True)

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


@app.get("/api/voices")
async def voices(
    locale: str | None = Query(default=None),
    gender: str | None = Query(default=None),
) -> list[dict[str, Any]]:
    """Edge-TTS voice catalog (5-min cached), optionally filtered.

    ``locale`` is a prefix match ("zh" matches zh-CN / zh-TW); ``gender``
    is an exact match ("Female" / "Male"). Without filters the full
    catalog is returned.
    """
    return filter_voices(await _cached_voices(), locale, gender)


@app.get("/api/voice/presets")
async def voice_presets() -> dict[str, Any]:
    assert state is not None
    return state.config.tts.presets


class PresetBody(BaseModel):
    name: str


@app.post("/api/voice/presets", dependencies=[Depends(require_token)])
async def apply_preset(body: PresetBody) -> dict[str, Any]:
    """Apply a scenario preset: upsert it as a profile and make it default."""
    assert state is not None
    try:
        profile = state.tts.apply_preset(body.name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'")) from e
    return {
        "ok": True,
        "profile": profile.to_dict(),
        "default_profile": state.config.tts.default_profile,
    }


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
async def _replay_history(
    ws: WebSocket, event_bus: EventBus, replay: int | None
) -> set[str]:
    """F3：连接建立后按时间顺序（旧 -> 新）回放 ``bus.history``。

    回放消息 = 标准 Event.to_dict() + ``"replayed": true`` 附加字段——
    HUD 的 onmessage 只依赖 id/type 字段，附加字段天然兼容，无需改 UI。
    ``replay=None`` 回放全部历史；``replay=N`` 只回放最近 N 条（上限）；
    ``replay=0`` 关闭回放。

    返回已回放的事件 id 集合，供实时转发循环去重：订阅发生在快照之前，
    两个时刻之间发布的事件会同时出现在队列与历史里，必须只发一次。
    """
    history = list(event_bus.history)  # 快照，防止回放期间追加的实时事件混入
    if replay is not None:
        # 注意 [-0:] 等价于 [0:]（整段），所以 0 要单独处理
        history = history[-replay:] if replay > 0 else []
    sent: set[str] = set()
    for event in history:
        sent.add(event.id)
        await ws.send_json({**event.to_dict(), "replayed": True})
    return sent


@app.websocket("/ws")
async def ws_events(
    ws: WebSocket,
    token: str | None = Query(default=None),
    replay: int | None = Query(default=None, ge=0),
) -> None:
    """HUD 的事件流：连接后先回放历史事件，再转发实时事件。

    回放数量可配置：``?replay=N`` 只回放最近 N 条（上限），``?replay=0``
    关闭回放，缺省回放全部历史（见 :func:`_replay_history`）。
    """
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
        replayed_ids = await _replay_history(ws, state.event_bus, replay)
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=15.0)
            except asyncio.TimeoutError:
                await ws.send_json(Event(type="system.ping", data={}).to_dict())
                continue
            if event.id in replayed_ids:
                continue  # 已在回放中发送过（订阅与快照之间发布的事件）
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
