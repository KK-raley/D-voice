"""IndexTTS sidecar HTTP 服务（FastAPI，独立进程运行在 GPU 机器上）。

进程隔离的动机（IndexTTS-2.5 调研结论）：IndexTTS 依赖重（git clone + uv
安装、Python 3.10-3.11、CUDA、~10 GB 权重），不能 import 进主框架；
sidecar 独立进程承载引擎，主框架（可以是纯 CPU 笔记本）经
:class:`~vocalis.voice.backends.indextts_client.IndexTTSClientBackend`
以 HTTP 访问。

降级设计（关键）：
* 启动时探测 CUDA 与 indextts 包（见 :mod:`vocalis.voice.sidecar.engine`），
  二者任一缺失 -> ``engine="none"``；
* ``/health`` **永远返回 200**（服务活着），只是 ``engine`` 字段不同——
  主框架据此知道"服务可达但克隆不可用"，走 Edge-TTS 兜底；
* engine="none" 时合成端点返回 **503** + 明确错误信息（health 与合成
  分离：不能因为克隆不可用就把整个服务标记为不健康）。

安全设计：
* **token 鉴权**——``--token`` CLI 参数或 ``VOCALIS_SIDECAR_TOKEN`` 环境
  变量设置后，所有端点（含 /health）要求 ``Authorization: Bearer <token>``
  或 ``X-Sidecar-Token`` 头；未设置 token 时无鉴权运行（默认场景只监听
  127.0.0.1 回环），启动日志会提示。局域网/跨机部署务必设置 token。
* **请求体前置限制**——middleware 按 Content-Length 拒绝超限请求体
  （默认 25 MB，超限 413），不进 pydantic 校验、不落任何处理器；
  ``text`` 字段另有 max_length=2000（超限 422）。

启动：``python -m vocalis.voice.sidecar``（默认 127.0.0.1:8765）。
"""

from __future__ import annotations

import base64
import binascii
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from vocalis.config import home_dir
from vocalis.voice.backends.base import SynthesisOptions
from vocalis.voice.sidecar.engine import (
    SidecarState,
    SynthesisEngine,
    probe_state,
)
from vocalis.voice.sidecar.registry import VoiceRegistry

logger = logging.getLogger("vocalis.voice.sidecar.server")

#: 参考音频大小上限（base64 解码后）：几秒参考音频远小于此，防滥用。
MAX_REF_AUDIO_BYTES = 20 * 1024 * 1024

#: 请求体大小上限（Content-Length，含 base64 膨胀）：超限直接 413，
#: 不读 body、不进校验——注册参考音频（20 MB 上限）的 base64 膨胀
#: （x4/3）也被覆盖，其余请求远小于此。
MAX_BODY_BYTES = 25 * 1024 * 1024

#: 合成文本长度上限（pydantic max_length，超限 422）。
MAX_TEXT_LENGTH = 2000


class RegisterRequest(BaseModel):
    """注册克隆音色请求（参考音频经 base64 上行，避免 multipart 依赖）。"""

    name: str
    language: str = "zh"
    consent: str = ""  # 必填：非空同意声明，缺失返回 400（合规设计）
    audio_base64: str


class SynthesizeRequest(BaseModel):
    """合成请求（text/voice 必填；控制参数与 IndexTTS-2.5 能力对齐）。"""

    text: str = Field(min_length=1, max_length=MAX_TEXT_LENGTH)
    voice: str = Field(min_length=1)
    speed: float = 1.0
    emotion: str | None = None
    duration_factor: float | None = None


def _public(entry: dict) -> dict:
    """对外暴露的音色字段（不回显同意声明原文与内部文件路径）。"""
    return {
        "name": entry["name"],
        "language": entry["language"],
        "kind": entry["kind"],
        "created_at": entry["created_at"],
    }


def _check_token(request: Request, token: str) -> bool:
    """Bearer token 校验：Authorization: Bearer <t> 或 X-Sidecar-Token: <t>。"""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        if auth[7:].strip() == token:
            return True
    return request.headers.get("x-sidecar-token", "").strip() == token


def create_app(
    state: SidecarState | None = None,
    *,
    registry_path: str | Path | None = None,
    model_dir: str | Path | None = None,
    token: str | None = None,
    max_body_bytes: int = MAX_BODY_BYTES,
) -> FastAPI:
    """构造 sidecar FastAPI 应用。

    参数：
        state:         引擎状态；None 则启动时自动探测（无 GPU/无 indextts
                       -> engine="none" 降级模式，服务照常启动）。
        registry_path: 注册表数据目录（默认 ``~/.vocalis/sidecar``）。
        model_dir:     IndexTTS 权重目录（仅自动探测路径使用）。
        token:         鉴权 token；None 则无鉴权（仅建议本机回环监听场景，
                       CLI 层默认从 ``VOCALIS_SIDECAR_TOKEN`` 环境变量读取）。
                       设置后所有端点（含 /health）都要求 Bearer 鉴权。
        max_body_bytes:请求体大小上限（按 Content-Length 前置拒绝，超限 413）。
    """
    if state is None:
        state = probe_state(model_dir)
    registry = VoiceRegistry(registry_path or (home_dir() / "sidecar"))

    app = FastAPI(
        title="Vocalis IndexTTS Sidecar",
        version="0.1.0",
        description="进程隔离的 IndexTTS-2.5 语音克隆合成服务",
    )
    app.state.sidecar = state
    app.state.registry = registry

    @app.middleware("http")
    async def _guard(request: Request, call_next):  # noqa: ANN202 - starlette 协议
        """前置守卫：请求体大小限制 + token 鉴权（所有端点，含 /health）。"""
        # 1) 请求体前置限制：按 Content-Length 拒绝，不读 body 不进处理器
        raw_length = request.headers.get("content-length")
        if raw_length is not None:
            try:
                body_size = int(raw_length)
            except ValueError:
                body_size = -1
            if body_size > max_body_bytes:
                return JSONResponse(
                    status_code=413,
                    content={
                        "detail": (
                            f"请求体过大（{body_size} 字节，"
                            f"上限 {max_body_bytes}）"
                        )
                    },
                )
        # 2) token 鉴权：设置 token 后所有端点一律要求（简单一致，health 不豁免）
        if token is not None and not _check_token(request, token):
            return JSONResponse(
                status_code=401,
                content={"detail": "未授权：缺少或错误的 Bearer token"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)

    if token is None:
        logger.warning(
            "sidecar 未配置鉴权 token（--token / VOCALIS_SIDECAR_TOKEN）："
            "无鉴权运行，仅建议本机回环监听（127.0.0.1）场景使用"
        )

    # -- 内部守卫 ---------------------------------------------------------
    def _require_engine() -> SynthesisEngine:
        if state.engine is None:
            raise HTTPException(
                status_code=503,
                detail=(
                    "IndexTTS 引擎不可用（无 GPU 或 indextts 未安装）；"
                    "语音克隆合成已禁用——preset 音色请走主框架的 Edge-TTS"
                ),
            )
        return state.engine

    def _require_ref(voice: str) -> str:
        entry = registry.get(voice)
        if entry is None:
            raise HTTPException(
                status_code=404,
                detail=f"未注册的克隆音色 '{voice}'：请先 POST /voices/register",
            )
        try:
            return str(registry.ref_path(voice))
        except ValueError as e:
            # 注册表条目异常（路径逃逸等，理论上已被加载期校验拦截）
            raise HTTPException(status_code=404, detail=str(e)) from e

    def _opts(req: SynthesizeRequest) -> SynthesisOptions:
        return SynthesisOptions(
            speed=req.speed,
            emotion=req.emotion,
            duration_factor=req.duration_factor,
        )

    # -- 端点 ---------------------------------------------------------------
    @app.get("/health")
    async def health() -> dict:
        """存活探测：永远 200；engine 字段区分降级模式（主框架的降级依据）。"""
        return {
            "status": "ok",
            "engine": "indextts" if state.engine is not None else "none",
            "gpu": state.gpu,
            "voices": len(registry),
        }

    @app.post("/voices/register")
    async def register_voice(req: RegisterRequest) -> dict:
        """注册克隆音色：consent 必填（缺失 400），参考音频 base64 上行。"""
        if not req.consent.strip():
            raise HTTPException(
                status_code=400,
                detail=(
                    "consent 必填：注册克隆音色（提取音色特征）需要参考音频"
                    "所有者的明确同意声明（合规要求，无绕过路径）"
                ),
            )
        try:
            audio = base64.b64decode(req.audio_base64, validate=True)
        except (binascii.Error, ValueError) as e:
            raise HTTPException(status_code=400, detail=f"audio_base64 非法: {e}") from e
        if not audio:
            raise HTTPException(status_code=400, detail="参考音频为空")
        if len(audio) > MAX_REF_AUDIO_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"参考音频过大（{len(audio)} 字节，上限 {MAX_REF_AUDIO_BYTES}）",
            )
        try:
            entry = registry.add(req.name, req.language, req.consent, audio)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return _public(entry)

    @app.get("/voices")
    async def list_voices() -> list[dict]:
        """列出已注册的克隆音色（含引擎降级期间注册的——重启后即可用）。"""
        return [_public(entry) for entry in registry.list()]

    @app.post("/synthesize")
    async def synthesize(req: SynthesizeRequest) -> dict:
        """整段合成：返回 JSON（base64 WAV + 采样率 + 时长）。"""
        engine = _require_engine()
        ref = _require_ref(req.voice)
        try:
            out = await engine.synthesize(req.text, ref, _opts(req))
        except HTTPException:
            raise
        except Exception as e:  # 引擎内部错误（模型加载失败等）如实上报
            logger.exception("IndexTTS 合成失败")
            raise HTTPException(status_code=500, detail=f"IndexTTS 合成失败: {e}") from e
        return {
            "audio": base64.b64encode(out.wav).decode("ascii"),
            "sample_rate": out.sample_rate,
            "duration_s": out.duration_s,
            "format": "wav",
        }

    @app.post("/synthesize/stream")
    async def synthesize_stream(req: SynthesizeRequest) -> StreamingResponse:
        """段级流式合成：裸 PCM16 分块，采样率在 X-Sample-Rate 响应头。"""
        engine = _require_engine()
        ref = _require_ref(req.voice)
        return StreamingResponse(
            engine.stream(req.text, ref, _opts(req)),
            media_type="audio/pcm",
            headers={
                "X-Sample-Rate": str(engine.sample_rate),
                "X-Format": "pcm16",
            },
        )

    return app
