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
  GET  /api/brain           - current D-VOICE brain config + availability probe
  POST /api/brain           - switch brain backend/model at runtime (persisted)
  POST /api/command         - text command through the Commander pipeline
  POST /api/ask             - direct question to the D-VOICE brain
  POST /api/speak           - synthesize text; returns audio/mpeg for browser playback
  POST /api/listen          - transcribe an uploaded audio clip (browser mic) via local ASR
  GET  /api/events/history  - recent event-bus history
  POST /api/vision/look     - screenshot + local OCR; optional brain question about screen
  GET  /api/vision/state    - ScreenWatcher state + observation history
  POST /api/vision/watch    - toggle independent screen monitoring (default off)
  WS   /ws                  - live event stream for the HUD
                              (replays bus history on connect, ?replay=N caps it)

Security: CORS is restricted to the dev HUD origin; when the VOCALIS_TOKEN
environment variable is set, every mutating request must carry it in the
X-Vocalis-Token header (or ?token= for the WebSocket).
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import vocalis
from vocalis.agents.registry import AgentRegistry, build_default_registry
from vocalis.config import VocalisConfig, load_secrets_env
from vocalis.dvoice.assistant import DVoiceBrain
from vocalis.dvoice.commander import Commander
from vocalis.dvoice.monitor import TaskMonitor
from vocalis.monitor.stress import StressRecorder, summarize
from vocalis.notify.notifier import Notifier
from vocalis.server.confirmations import ConfirmationService
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
        load_secrets_env()  # API keys from ~/.vocalis/secrets.env -> environ
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
        # P0-2: risky voice/text dispatches park here for HUD-card approval.
        self.confirmations = ConfirmationService(bus)
        self.commander = Commander(self.registry, self.brain, bus, self.confirmations)
        self.transcriber: Any = None  # lazy: built on first /api/listen call
        self.standby: Any = None
        # P0-3: long-run standby metrics (JSONL, no audio), started in lifespan.
        self.stress = StressRecorder(interval_s=60.0, brain_ok=self._brain_probe)
        self.voice_lock = asyncio.Lock()
        self.microphone_task: asyncio.Task | None = None
        self.microphone_stop = asyncio.Event()
        self.microphone_error: str | None = None
        from vocalis.vision.watcher import ScreenWatcher

        self.screen_watcher = ScreenWatcher(self.config, self.event_bus)
        self._health_cache: tuple[float, dict[str, Any]] = (0.0, {})

    def voice_session(self):
        if self.standby is None:
            from vocalis.voice.asr import Transcriber
            from vocalis.voice.gate import VoiceGate
            from vocalis.voice.standby import StandbySession

            self.transcriber = Transcriber(self.config.asr)
            self.standby = StandbySession(
                self.config, VoiceGate(self.config), self.transcriber, self.commander
            )
        return self.standby

    async def stop_microphone(self) -> None:
        self.microphone_stop.set()
        if self.microphone_task is not None:
            self.microphone_task.cancel()
            await asyncio.gather(self.microphone_task, return_exceptions=True)
            self.microphone_task = None
        if self.standby is not None:
            self.standby.sleep(reason="microphone_off")

    async def listen_microphone(self) -> None:
        from vocalis.voice.standby import run_microphone

        async def on_result(result):
            await self.event_bus.publish("voice.session", **result)
            if result.get("reply"):
                await self.tts.speak(result["reply"], play=True)

        try:
            await run_microphone(self.voice_session(), on_result, self.microphone_stop)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.microphone_error = str(exc)
            await self.event_bus.publish("voice.session", state="sleeping", reason="microphone_error")
        finally:
            if self.standby is not None:
                self.standby.sleep(reason="microphone_stopped")

    async def observe_screen(self):
        """截屏 + 本地 OCR（视觉能力的唯一入口，便于测试桩替换）。"""
        from vocalis.vision.screen import observe_screen

        return await observe_screen()

    async def _brain_probe(self) -> bool:
        """Throttled brain availability probe for the stress recorder."""
        try:
            return await asyncio.wait_for(self.brain.available(), timeout=4.0)
        except Exception:
            return False

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
    await state.stress.start()
    await state.event_bus.publish(EventType.SYSTEM, message=f"Vocalis v{vocalis.__version__} online")
    yield
    await state.stop_microphone()
    await state.screen_watcher.stop()
    await state.monitor.stop()
    await state.stress.stop()


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
    assert state is not None
    if state.config.tts.engine == "sapi":
        return []  # SAPI uses installed system voices; never fetch Edge's catalog.
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


def _brain_payload(available: bool) -> dict[str, Any]:
    assert state is not None
    b = state.config.brain
    return {
        "backend": b.backend,
        "model": b.model,
        "base_url": b.base_url,
        "api_key_env": b.api_key_env,
        "enabled": b.enabled,
        "available": available,
        "local_only": b.local_only,
        "deployment_dir": b.deployment_dir,
        "reply_source": getattr(state.brain, "last_reply_source", None),
        "last_error": getattr(state.brain, "last_error", None),
    }


@app.get("/api/brain", dependencies=[Depends(require_token)])
async def brain_info() -> dict[str, Any]:
    """Current D-VOICE brain configuration + live availability probe."""
    assert state is not None
    try:
        available = await asyncio.wait_for(state.brain.available(), timeout=3.0)
    except Exception:  # probe must never 500 the HUD
        available = False
    return _brain_payload(available)


class BrainBody(BaseModel):
    """Partial brain update; omitted fields keep their current value.

    ``api_key`` carries the secret itself: it is stored in the protected
    ``~/.vocalis/secrets.env`` (0600) and injected into os.environ — never
    into config.toml. A key mistakenly typed into ``api_key_env`` (the env
    var *name* field) is auto-corrected here.
    """

    backend: str | None = None  # "ollama" | "openai-compatible"
    model: str | None = None
    base_url: str | None = None
    api_key_env: str | None = None
    api_key: str | None = None
    enabled: bool | None = None
    local_only: bool | None = None


def _store_api_key(env_name: str, key: str) -> None:
    """Persist the key under ``env_name`` in secrets.env and set it live."""
    from vocalis.config import secrets_env_path

    path = secrets_env_path()
    lines: list[str] = []
    if path.exists():
        lines = [
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith(f"{env_name}=")
        ]
    lines.append(f"{env_name}={key}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    os.environ[env_name] = key  # hot effect: next chat request uses it


@app.post("/api/brain", dependencies=[Depends(require_token)])
async def update_brain(body: BrainBody) -> dict[str, Any]:
    """Switch the brain backend/model at runtime and persist the choice.

    Applies in place to ``state.config.brain`` (DVoiceBrain reads it live),
    rebuilds the brain instance to reset cached probes, re-wires the
    Commander, and invalidates the /api/health cache so the next probe
    reflects the new backend.
    """
    assert state is not None
    b = replace(state.config.brain)
    if body.backend is not None:
        if body.backend not in ("local-qwen", "ollama", "openai-compatible"):
            raise HTTPException(
                status_code=422, detail="backend must be local-qwen, ollama or openai-compatible"
            )
        b.backend = body.backend
    if body.model is not None and body.model.strip():
        b.model = body.model.strip()
    if body.base_url is not None:
        b.base_url = body.base_url.strip() or None
    if body.api_key_env is not None and body.api_key_env.strip():
        env_name = body.api_key_env.strip()
        if env_name.startswith("sk-") and len(env_name) > 20:
            # 密钥本身被误填进"环境变量名"输入框：自动纠正
            body.api_key = body.api_key or env_name
            env_name = "DEEPSEEK_API_KEY"
        b.api_key_env = env_name
    if body.enabled is not None:
        b.enabled = body.enabled
    if body.local_only is not None:
        b.local_only = body.local_only
    if b.backend == "local-qwen":
        b.local_only = True
        b.base_url = b.base_url or "http://127.0.0.1:8080/v1"
    if b.local_only:
        from vocalis.dvoice.local_qwen import validate_local_url
        endpoint = b.host if b.backend == "ollama" else b.base_url
        try:
            validate_local_url(endpoint or "")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if body.api_key is not None and body.api_key.strip():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", b.api_key_env):
            raise HTTPException(status_code=422, detail="invalid API key environment name")
        if any(c in body.api_key for c in "\r\n"):
            raise HTTPException(status_code=422, detail="API key must be a single line")
        _store_api_key(b.api_key_env, body.api_key.strip())
    state.config.brain = b
    state.config.save()
    rebuilt = DVoiceBrain(state.config, state.registry, state.event_bus)
    state.brain = rebuilt
    state.commander.brain = rebuilt
    state._health_cache = (0.0, {})
    try:
        available = await asyncio.wait_for(state.brain.available(), timeout=3.0)
    except Exception:
        available = False
    return {"ok": True, **_brain_payload(available)}


class CommandBody(BaseModel):
    text: str
    speak: bool = True
    user: str | None = None  # P0-7: pin dialogue history to one speaker


# 视觉意图：问"能不能看到屏幕/桌面"或要求看画面时，让 D-VOICE 亲眼看屏幕
# 再回答（本地 OCR），而不是让大脑凭空回答"看不到"。祈使派发语句除外。
VISION_INTENT_RE = re.compile(r"屏幕|桌面|画面|截图|看到我|看见我|监视")


@app.post("/api/command", dependencies=[Depends(require_token)])
async def command(body: CommandBody) -> dict[str, Any]:
    assert state is not None
    # P0-7: a text client may only pin a dialogue lane to a user the voice
    # session has actually verified; anything else stays anonymous so one
    # token holder cannot read another user's context.
    standby = getattr(state, "standby", None)
    verified = standby.snapshot().get("user") if standby is not None else None
    user = body.user if body.user and body.user == verified else None
    if not body.text.lower().startswith(("让", "@", "帮我", "run ", "execute ")) and (
        VISION_INTENT_RE.search(body.text)
    ):
        obs = await state.observe_screen()
        reply = await state.brain.chat(
            body.text, context={"screen": obs.digest()}, user=user
        )
        await state.event_bus.publish(EventType.DVOICE_SAYING, text=reply)
        return {"kind": "vision", "reply": reply, "digest": obs.digest()[:400]}
    result = await state.commander.execute(body.text, user=user)
    if result.get("kind") == "confirmation":
        await state.tts.speak(
            "这个请求涉及高风险操作，请在屏幕上确认。", play=True
        )
        return result
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


@app.post("/api/speak", dependencies=[Depends(require_token)])
async def speak(body: SpeakBody) -> Response:
    """Synthesize speech and return the audio so any browser can play it."""
    assert state is not None
    result = await state.tts.synthesize(body.text, body.profile)
    if not result.ok or result.audio_bytes is None:
        raise HTTPException(status_code=502, detail=result.error or "synthesis failed")
    media_type = "audio/wav" if result.audio_bytes.startswith(b"RIFF") else "audio/mpeg"
    return Response(content=result.audio_bytes, media_type=media_type)


@app.post("/api/listen", dependencies=[Depends(require_token)])
async def listen(request: Request) -> dict[str, Any]:
    """Transcribe an uploaded audio clip via local ASR (faster-whisper).

    The HUD records the microphone with MediaRecorder and PUTs the raw blob
    here (audio/webm;codecs=opus typically). The body is read raw — no
    multipart form — and decoded server-side by PyAV, so no browser-side
    conversion is needed. Returns {"text", "language"}; a silent/too-short
    clip yields empty text.
    """
    assert state is not None
    if state.microphone_task is not None and not state.microphone_task.done():
        raise HTTPException(status_code=409, detail="stop continuous microphone before uploading audio")
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > 5 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="audio upload exceeds 5 MiB")
    if not data:
        raise HTTPException(status_code=400, detail="empty audio upload")
    try:
        from vocalis.voice.asr import decode_audio
    except Exception as e:  # voice stack extra not installed
        raise HTTPException(status_code=503, detail=f"ASR unavailable: {e}") from e
    try:
        pcm, rate = await asyncio.to_thread(
            decode_audio, bytes(data), state.config.standby.max_utterance_s
        )
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"cannot decode audio: {e}") from e
    if state.voice_lock.locked():
        raise HTTPException(status_code=409, detail="voice processing busy; retry after current turn")
    async with state.voice_lock:
        if state.microphone_task is not None and not state.microphone_task.done():
            raise HTTPException(status_code=409, detail="continuous microphone started during upload")
        try:
            session = state.voice_session()
            return await session.process_audio(pcm, sample_rate=rate)
        except RuntimeError as e:
            raise HTTPException(status_code=503, detail=str(e)) from e


@app.get("/api/standby", dependencies=[Depends(require_token)])
async def standby_status() -> dict[str, Any]:
    assert state is not None
    mic = {"microphone_running": state.microphone_task is not None and not state.microphone_task.done(),
           "error": state.microphone_error}
    if state.standby is None:
        return {"state": "standby", "user": None, "reason": "not_started", **mic}
    return {**state.standby.snapshot(), **mic}


class MicrophoneBody(BaseModel):
    enabled: bool


@app.post("/api/standby/microphone", dependencies=[Depends(require_token)])
async def standby_microphone(body: MicrophoneBody) -> dict[str, Any]:
    assert state is not None
    if not body.enabled:
        await state.stop_microphone()
    elif state.microphone_task is None or state.microphone_task.done():
        if state.voice_lock.locked():
            raise HTTPException(status_code=409, detail="voice processing busy")
        try:
            session = state.voice_session()
            if session.gate.profile_count() == 0:
                raise RuntimeError("请先运行 vocalis enroll --user you 录入声纹")
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        state.microphone_error = None
        state.microphone_stop = asyncio.Event()
        state.microphone_task = asyncio.create_task(state.listen_microphone())
        await asyncio.sleep(0)
    return await standby_status()


@app.post("/api/standby/sleep", dependencies=[Depends(require_token)])
async def standby_sleep() -> dict[str, Any]:
    assert state is not None
    if state.voice_lock.locked():
        raise HTTPException(status_code=409, detail="wait for current voice turn to finish")
    await state.stop_microphone()
    if state.standby is not None:
        state.standby.sleep(reason="manual")
    await state.screen_watcher.stop()
    return await standby_status()


class VisionLookBody(BaseModel):
    question: str | None = None  # 提问则由大脑结合屏幕内容回答


@app.post("/api/vision/look", dependencies=[Depends(require_token)])
async def vision_look(body: VisionLookBody) -> dict[str, Any]:
    """亲眼看一眼屏幕：截屏 + 本地 OCR；带 question 时由大脑结合屏幕回答。"""
    assert state is not None
    obs = await state.observe_screen()
    reply = ""
    if body.question and body.question.strip():
        reply = await state.brain.chat(
            body.question.strip(), context={"screen": obs.digest()}
        )
        await state.event_bus.publish(EventType.DVOICE_SAYING, text=reply)
    return {
        "ok": True,
        "title": obs.title,
        "engine": obs.engine,
        "text": obs.text,
        "reply": reply,
    }


@app.get("/api/vision/state", dependencies=[Depends(require_token)])
async def vision_state() -> dict[str, Any]:
    """屏幕监管通道状态与历史观测。"""
    assert state is not None
    return state.screen_watcher.snapshot()


class VisionWatchBody(BaseModel):
    enabled: bool
    interval_s: float | None = None


@app.post("/api/vision/watch", dependencies=[Depends(require_token)])
async def vision_watch(body: VisionWatchBody) -> dict[str, Any]:
    """开关独立屏幕监管（不依赖 agent 上报的第二监管来源；默认关闭）。"""
    assert state is not None
    if body.enabled:
        await state.screen_watcher.start(body.interval_s)
    else:
        await state.screen_watcher.stop()
    return {"ok": True, **state.screen_watcher.snapshot()}


@app.get("/api/events/history", dependencies=[Depends(require_token)])
async def event_history() -> list[dict[str, Any]]:
    assert state is not None
    return [e.to_dict() for e in state.event_bus.history[-100:]]


# ----------------------------------------------------------------------
# P0-2 confirmations, P0-6 cancel, P0-3 metrics
# ----------------------------------------------------------------------
@app.get("/api/confirmations", dependencies=[Depends(require_token)])
async def confirmations_list() -> list[dict[str, Any]]:
    """Pending high-risk confirmations (HUD cards)."""
    assert state is not None
    return state.confirmations.pending()


class ConfirmBody(BaseModel):
    approved: bool


@app.post("/api/confirmations/{cid}", dependencies=[Depends(require_token)])
async def confirm_resolve(cid: str, body: ConfirmBody) -> dict[str, Any]:
    """Approve/deny a risky dispatch; approval executes the parked plan."""
    assert state is not None
    plan = await state.confirmations.resolve(cid, body.approved)
    if plan is None:
        if body.approved:
            raise HTTPException(status_code=404, detail="confirmation not found or expired")
        return {"ok": True, "approved": False, "executed": False}
    # Carry the verified identity into the executed plan so the audit
    # receipt shows who approved/issued the risky dispatch.
    result = await state.commander.execute_plan(
        plan, user=plan.get("user"), voiceprint=plan.get("voiceprint")
    )
    reply = result.get("reply") or _task_reply(result)
    return {**result, "approved": True, "executed": True, "spoken": reply}


@app.post("/api/tasks/{task_id}/cancel", dependencies=[Depends(require_token)])
async def cancel_task(task_id: str) -> dict[str, Any]:
    """Receipt-card cancel entry: abort a running agent dispatch."""
    assert state is not None
    cancelled = state.registry.cancel_task(task_id)
    if not cancelled:
        raise HTTPException(status_code=409, detail="task not running or already finished")
    return {"ok": True, "cancelled": task_id}


@app.get("/api/metrics", dependencies=[Depends(require_token)])
async def metrics() -> dict[str, Any]:
    """Latest long-run standby metrics + aggregate summary (P0-3)."""
    assert state is not None
    return {
        "recording": state.stress.running,
        "uptime_s": round(state.stress.uptime_s, 1),
        "last": state.stress.last,
        "summary": summarize(),
    }


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
