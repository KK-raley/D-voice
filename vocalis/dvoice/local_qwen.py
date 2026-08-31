"""Detect and explicitly start a local llama.cpp Qwen runtime, without downloads.

No models, transcripts or keys leave the machine. Health checks do not perform
inference. An existing service is reused; only processes started here are stopped
if startup fails. Deployment files are read-only and logs live in VOCALIS_HOME.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from vocalis.config import BrainConfig, VocalisConfig, home_dir

_start_lock = threading.Lock()
_processes: dict[str, subprocess.Popen] = {}


def validate_local_url(url: str) -> str:
    """Accept only literal loopback or localhost, never credentials or redirects."""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("Local Qwen needs an http(s) loopback URL")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("Local endpoint must not contain credentials, query or fragment")
    if parts.hostname.lower() != "localhost":
        try:
            local = ipaddress.ip_address(parts.hostname).is_loopback
        except ValueError:
            local = False
        if not local:
            raise ValueError("Cloud endpoints are disabled: use 127.0.0.1 or localhost")
    # Resolve localhost as a literal rather than trusting DNS or a hosts override.
    if parts.hostname.lower() == "localhost":
        port = f":{parts.port}" if parts.port else ""
        return f"{parts.scheme}://127.0.0.1{port}{parts.path}".rstrip("/")
    _ = parts.port  # reject malformed ports before any network request
    return url.rstrip("/")


def configure_local_qwen(
    config: VocalisConfig,
    deployment_dir: str | Path | None = None,
    model_file: str | None = None,
    base_url: str | None = None,
) -> BrainConfig:
    """Migrate in memory, preserving unrelated user settings; caller saves explicitly."""
    cfg = config.brain
    endpoint = validate_local_url(base_url or "http://127.0.0.1:8080/v1")
    cfg.backend = "local-qwen"
    cfg.enabled = True
    cfg.local_only = True
    cfg.base_url = endpoint
    cfg.deployment_dir = str(deployment_dir or cfg.deployment_dir or "")
    cfg.model_file = model_file or "models/qwen3-4b-q4_k_m.gguf"
    cfg.model = Path(cfg.model_file).name
    cfg.auto_start = False
    return cfg


def inspect_local_qwen(cfg: BrainConfig) -> dict:
    """Read-only file inventory, safe to call before a server is running."""
    if not cfg.deployment_dir:
        raise ValueError(
            "brain.deployment_dir is not configured - "
            "set it via `vocalis brain --deployment-dir` or config.toml"
        )
    root = Path(cfg.deployment_dir).expanduser().resolve()
    runtime = (root / cfg.runtime_file).resolve()
    model = (root / cfg.model_file).resolve()
    if not runtime.is_relative_to(root) or not model.is_relative_to(root):
        raise ValueError("Runtime and model paths must stay inside deployment_dir")
    endpoint = validate_local_url(cfg.base_url or "http://127.0.0.1:8080/v1")
    return {
        "backend": "local-qwen", "local_only": True, "base_url": endpoint,
        "deployment_dir": str(root), "runtime_path": str(runtime), "model_path": str(model),
        "runtime_exists": runtime.is_file(), "model_exists": model.is_file(),
        "model": cfg.model,
    }


async def probe_local_qwen(cfg: BrainConfig) -> dict:
    """Check the models endpoint only. Online means a loaded model, not rules."""
    if not cfg.deployment_dir:
        # Unconfigured installs get a structured status, not an exception,
        # so `vocalis local-qwen --check` stays JSON-diagnosable on first run.
        return {
            "backend": cfg.backend, "local_only": True, "base_url": cfg.base_url,
            "deployment_dir": "", "available": False, "status": "not_configured",
            "error": "brain.deployment_dir is not configured - "
                     "set it via `vocalis brain --deployment-dir` or config.toml",
        }
    result = inspect_local_qwen(cfg)
    result.update(available=False, status="offline", error=None)
    if not cfg.enabled:
        result["status"] = "disabled"
        return result
    try:
        async with httpx.AsyncClient(timeout=3.0, trust_env=False, follow_redirects=False) as client:
            response = await client.get(f"{result['base_url']}/models")
            response.raise_for_status()
            models = response.json().get("data", [])
        names = [str(entry.get("id", "")) for entry in models if isinstance(entry, dict)]
        result["loaded_models"] = names
        expected = cfg.model.casefold()
        if not any(name.replace("\\", "/").split("/")[-1].casefold() == expected for name in names):
            result.update(status="model_mismatch", error=f"Expected model {cfg.model}; loaded: {names}")
        else:
            result.update(available=True, status="online")
    except Exception as exc:
        result["error"] = str(exc)
    return result


async def ensure_local_qwen(cfg: BrainConfig, log_dir: str | Path | None = None) -> dict:
    """Explicitly start CPU Qwen and wait a bounded time; never download anything."""
    result = await probe_local_qwen(cfg)
    if result["available"] or result["status"] in {"disabled", "model_mismatch", "not_configured"}:
        return result
    if not result["runtime_exists"] or not result["model_exists"]:
        result.update(status="missing_files", error="Local Qwen runtime or GGUF model is missing")
        return result
    endpoint = urlsplit(result["base_url"])
    if endpoint.scheme != "http" or endpoint.path.rstrip("/") != "/v1":
        raise ValueError("Managed llama.cpp startup requires http://loopback:port/v1")
    if not 1 <= cfg.startup_timeout_s <= 600:
        raise ValueError("startup_timeout_s must be between 1 and 600")
    log_root = Path(log_dir) if log_dir is not None else home_dir() / "logs"
    log_root.mkdir(parents=True, exist_ok=True)
    log_path = log_root / "local-qwen.log"
    command = [
        result["runtime_path"], "-m", result["model_path"],
        "--host", endpoint.hostname or "127.0.0.1", "--port", str(endpoint.port or 80),
        "--alias", cfg.model, "-c", str(cfg.context_size), "--threads", str(cfg.threads),
        "-ngl", "0",
    ]
    key = result["base_url"]
    with _start_lock:
        process = _processes.get(key)
        owned = process is None or process.poll() is not None
        if owned:
            # Detach no console on Windows; shell=False keeps all paths literal.
            try:
                with log_path.open("ab") as stream:
                    process = subprocess.Popen(
                        command, cwd=log_root, stdin=subprocess.DEVNULL, stdout=stream,
                        stderr=subprocess.STDOUT, shell=False,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    )
            except OSError as exc:
                result.update(status="startup_failed", error=str(exc), log_path=str(log_path))
                return result
            _processes[key] = process
    deadline = time.monotonic() + cfg.startup_timeout_s
    ready = False
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                result.update(status="startup_failed", error=f"Runtime exited ({process.returncode})")
                break
            result = await probe_local_qwen(cfg)
            if result["available"]:
                ready = True
                result.update(started=owned, pid=process.pid, log_path=str(log_path))
                return result
            if result["status"] == "model_mismatch":
                break
            await asyncio.sleep(0.5)
        else:
            result.update(status="startup_timeout", error="Local Qwen startup timed out")
    finally:
        # Cancellation and interrupted startup must not leak a background server.
        if owned and not ready and process.poll() is None:
            process.terminate()
            try:
                await asyncio.to_thread(process.wait, timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait, timeout=5)
    result["log_path"] = str(log_path)
    return result
