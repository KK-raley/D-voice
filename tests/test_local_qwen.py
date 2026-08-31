"""Privacy boundaries and real transport semantics for local Qwen (no model needed)."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from vocalis.config import BrainConfig, VocalisConfig
from vocalis.dvoice.assistant import DVoiceBrain
from vocalis.dvoice.local_qwen import (
    configure_local_qwen,
    ensure_local_qwen,
    inspect_local_qwen,
    probe_local_qwen,
    validate_local_url,
)


@pytest.mark.parametrize("url", [
    "https://api.deepseek.com/v1", "http://192.168.1.2/v1", "http://127.0.0.1.evil/v1",
    "http://user:secret@localhost/v1", "http://localhost/v1?key=secret", "file:///tmp/model",
])
def test_rejects_remote_or_credential_endpoints(url):
    with pytest.raises(ValueError):
        validate_local_url(url)


def test_loopback_and_migration_preserve_other_settings():
    assert validate_local_url("http://localhost:8080/v1/") == "http://127.0.0.1:8080/v1"
    assert validate_local_url("http://[::1]:8080/v1") == "http://[::1]:8080/v1"
    cfg = VocalisConfig()
    cfg.brain.base_url = "https://api.deepseek.com/v1"
    cfg.brain.backend = "openai-compatible"
    cfg.tts.default_profile = "orion"
    configure_local_qwen(cfg)
    assert cfg.brain.backend == "local-qwen"
    assert cfg.brain.local_only is True
    assert cfg.brain.base_url == "http://127.0.0.1:8080/v1"
    assert cfg.tts.default_profile == "orion"


def _mock_transport(monkeypatch, handler):
    original = httpx.AsyncClient
    seen = []

    def factory(*args, **kwargs):
        seen.append(kwargs)
        return original(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return seen


def test_qwen_request_uses_no_secret_no_proxy_and_no_thinking(monkeypatch):
    import json

    requests = []
    monkeypatch.setenv("DVOICE_API_KEY", "cloud-secret-do-not-send")

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "你好。"}}]})

    kwargs = _mock_transport(monkeypatch, handler)
    brain = DVoiceBrain(VocalisConfig())
    assert asyncio.run(brain.chat("你好")) == "你好。"
    assert brain.last_reply_source == "local-model"
    request = requests[0]
    assert str(request.url) == "http://127.0.0.1:8080/v1/chat/completions"
    assert "authorization" not in request.headers
    assert json.loads(request.content)["chat_template_kwargs"] == {"enable_thinking": False}
    assert kwargs[0]["trust_env"] is False
    assert kwargs[0]["follow_redirects"] is False


def test_disabled_brain_never_constructs_network_client(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Disabled model must never access the network")

    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    cfg = VocalisConfig()
    cfg.brain.enabled = False
    brain = DVoiceBrain(cfg)
    assert asyncio.run(brain.available()) is False
    assert "规则回复" in asyncio.run(brain.chat("你好"))
    assert brain.last_reply_source == "rules"


def test_old_remote_config_is_blocked_before_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Old remote configuration must not create a network client")

    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    cfg = VocalisConfig.from_dict({"brain": {
        "backend": "openai-compatible", "base_url": "https://api.deepseek.com/v1",
    }})
    brain = DVoiceBrain(cfg)
    assert asyncio.run(brain.available()) is False
    assert "规则回复" in asyncio.run(brain.chat("你好"))
    assert "Cloud endpoints are disabled" in brain.last_error


@pytest.mark.parametrize("code", [301, 401, 403, 404, 429, 503])
def test_probe_does_not_claim_http_errors_are_online(monkeypatch, code, tmp_path):
    _mock_transport(monkeypatch, lambda req: httpx.Response(code, json={}))
    status = asyncio.run(probe_local_qwen(BrainConfig(deployment_dir=str(tmp_path))))
    assert status["available"] is False


def test_probe_checks_loaded_model_without_inference(monkeypatch, tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"data": [{"id": "qwen3-4b-q4_k_m.gguf"}]})

    _mock_transport(monkeypatch, handler)
    cfg = BrainConfig(deployment_dir=str(tmp_path))
    assert asyncio.run(probe_local_qwen(cfg))["available"] is True
    assert len(requests) == 1 and requests[0].method == "GET"
    assert requests[0].url.path == "/v1/models"


def test_probe_without_deployment_dir_reports_not_configured():
    status = asyncio.run(probe_local_qwen(BrainConfig()))
    assert status["available"] is False
    assert status["status"] == "not_configured"
    assert "deployment_dir" in status["error"]


def test_mismatched_model_is_not_reused_or_replaced(monkeypatch, tmp_path):
    _mock_transport(monkeypatch, lambda req: httpx.Response(200, json={
        "data": [{"id": "a-different-model"}],
    }))

    def forbidden(*args, **kwargs):
        pytest.fail("Do not replace someone else's running model")

    monkeypatch.setattr("vocalis.dvoice.local_qwen.subprocess.Popen", forbidden)
    cfg = BrainConfig(deployment_dir=str(tmp_path))
    status = asyncio.run(ensure_local_qwen(cfg))
    assert status["status"] == "model_mismatch" and status["available"] is False


def test_missing_files_reported_without_spawn(monkeypatch, tmp_path):
    def unavailable(request):
        raise httpx.ConnectError("offline", request=request)

    _mock_transport(monkeypatch, unavailable)
    cfg = BrainConfig(deployment_dir=str(tmp_path))
    result = asyncio.run(ensure_local_qwen(cfg, log_dir=tmp_path / "logs"))
    assert result["status"] == "missing_files"
    assert not (tmp_path / "logs").exists()


def test_paths_must_remain_in_deployment(tmp_path):
    with pytest.raises(ValueError, match="inside deployment_dir"):
        inspect_local_qwen(BrainConfig(deployment_dir=str(tmp_path), model_file="../secret"))


def test_existing_ready_runtime_does_not_write_logs_or_spawn(monkeypatch, tmp_path):
    _mock_transport(monkeypatch, lambda req: httpx.Response(200, json={
        "data": [{"id": "qwen3-4b-q4_k_m.gguf"}],
    }))
    cfg = BrainConfig(deployment_dir=str(tmp_path))
    result = asyncio.run(ensure_local_qwen(cfg, log_dir=tmp_path / "logs"))
    assert result["available"] is True
    assert not (tmp_path / "logs").exists()


def test_redirect_never_sends_a_second_request(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(307, headers={"location": "https://example.com/collect"})

    _mock_transport(monkeypatch, handler)
    brain = DVoiceBrain(VocalisConfig())
    assert "规则回复" in asyncio.run(brain.chat("private speech"))
    assert len(requests) == 1


def _runtime_files(tmp_path):
    cfg = BrainConfig(deployment_dir=str(tmp_path))
    for relative in (cfg.model_file, cfg.runtime_file):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    return cfg


def test_startup_os_error_is_actionable(monkeypatch, tmp_path):
    def unavailable(request):
        raise httpx.ConnectError("offline", request=request)

    def broken(*args, **kwargs):
        raise OSError("invalid runtime executable")

    _mock_transport(monkeypatch, unavailable)
    monkeypatch.setattr("vocalis.dvoice.local_qwen.subprocess.Popen", broken)
    result = asyncio.run(ensure_local_qwen(_runtime_files(tmp_path), tmp_path / "logs"))
    assert result["status"] == "startup_failed"
    assert result["error"] == "invalid runtime executable"


def test_startup_cancellation_stops_only_owned_process(monkeypatch, tmp_path):
    import vocalis.dvoice.local_qwen as runtime

    cfg = _runtime_files(tmp_path)
    calls = 0

    async def probe(config):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise asyncio.CancelledError
        return {**inspect_local_qwen(config), "available": False, "status": "offline"}

    class Process:
        terminated = False
        returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = -1

        def wait(self, timeout):
            return self.returncode

    process = Process()
    monkeypatch.setattr(runtime, "_processes", {})
    monkeypatch.setattr(runtime, "probe_local_qwen", probe)
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda *a, **kw: process)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(ensure_local_qwen(cfg, tmp_path / "logs"))
    assert process.terminated is True


def test_cli_check_is_read_only_even_on_first_run(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from vocalis.cli import app

    config_home = tmp_path / "does-not-exist"
    monkeypatch.setenv("VOCALIS_HOME", str(config_home))
    _mock_transport(monkeypatch, lambda req: httpx.Response(503))
    result = CliRunner().invoke(app, ["local-qwen", "--check"])
    assert result.exit_code == 1
    assert '"available": false' in result.output
    assert not config_home.exists()


def test_cli_migration_preserves_non_brain_settings(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from vocalis.cli import app

    monkeypatch.setenv("VOCALIS_HOME", str(tmp_path))
    config = VocalisConfig()
    config.brain.backend = "openai-compatible"
    config.brain.base_url = "https://api.deepseek.com/v1"
    config.tts.default_profile = "orion"
    config.save()
    result = CliRunner().invoke(app, ["local-qwen", "--model-file", "models/qwen3-8b-q4_k_m.gguf"])
    assert result.exit_code == 0, result.output
    saved = VocalisConfig.load()
    assert saved.brain.backend == "local-qwen"
    assert saved.brain.model == "qwen3-8b-q4_k_m.gguf"
    assert saved.brain.local_only is True
    assert saved.tts.default_profile == "orion"


def test_cli_start_with_missing_files_fails_clearly(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from vocalis.cli import app

    monkeypatch.setenv("VOCALIS_HOME", str(tmp_path / "state"))
    _mock_transport(monkeypatch, lambda req: httpx.Response(503))
    result = CliRunner().invoke(app, ["local-qwen", "--start", "--deployment-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert '"status": "missing_files"' in result.output


def test_cli_check_rejects_mutating_flags(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from vocalis.cli import app

    config_home = tmp_path / "missing"
    monkeypatch.setenv("VOCALIS_HOME", str(config_home))
    result = CliRunner().invoke(app, ["local-qwen", "--check", "--start"])
    assert result.exit_code == 2
    assert not config_home.exists()
