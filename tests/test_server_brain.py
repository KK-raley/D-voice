"""/api/brain 大脑配置读写与热切换测试（完全离线，桩 state）。"""

from __future__ import annotations

from dataclasses import asdict
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import vocalis.server.app as app_module
from vocalis.config import BrainConfig, VocalisConfig
from vocalis.server.events import EventBus


class _StubBrain:
    """记录重建次数；可用性按 backend 确定性返回（openai-compatible 视为在线）。"""

    instances: list[_StubBrain] = []

    def __init__(self, config: VocalisConfig, registry=None, event_bus=None) -> None:
        self.config = config
        self.registry = registry
        self.event_bus = event_bus
        _StubBrain.instances.append(self)

    async def available(self) -> bool:
        return self.config.brain.backend == "openai-compatible"


class _StubState:
    def __init__(self) -> None:
        self.config = VocalisConfig()
        self.event_bus = EventBus(history_size=16)
        self.registry = None
        self.brain = _StubBrain(self.config)
        self.commander = SimpleNamespace(brain=self.brain)
        self._health_cache: tuple[float, dict] = (123.0, {"stale": True})


@pytest.fixture()
def client(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> TestClient:
    """桩 state + 隔离的 VOCALIS_HOME（save() 写入 tmp，不碰用户真实配置）。"""
    _StubBrain.instances.clear()
    monkeypatch.setenv("VOCALIS_HOME", str(tmp_path))
    monkeypatch.setattr(app_module, "state", _StubState())
    monkeypatch.setattr(app_module, "DVoiceBrain", _StubBrain)
    # Do not enter the lifespan: that would replace the isolated stub with a
    # real AppState and start background monitors during endpoint unit tests.
    c = TestClient(app_module.app)
    try:
        yield c
    finally:
        c.close()


def test_get_brain_returns_config_and_probe(client: TestClient) -> None:
    r = client.get("/api/brain")
    assert r.status_code == 200
    body = r.json()
    assert body["backend"] == "local-qwen"
    assert body["model"] == "qwen3-4b-q4_k_m.gguf"
    assert body["available"] is False  # stub marks only compatible backend online
    assert body["enabled"] is True
    assert body["local_only"] is True
    assert body["base_url"] == "http://127.0.0.1:8080/v1"


def test_post_brain_switches_persists_and_rewires(client: TestClient) -> None:
    r = client.post(
        "/api/brain",
        json={
            "backend": "openai-compatible",
            "model": "deepseek-chat",
            "base_url": "https://api.deepseek.com/v1",
            "api_key_env": "DEEPSEEK_API_KEY",
            "local_only": False,  # cloud access requires an explicit opt-out
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["backend"] == "openai-compatible"
    assert body["model"] == "deepseek-chat"
    assert body["available"] is True  # 桩：openai-compatible 视为在线

    # 持久化到隔离的 VOCALIS_HOME
    saved = VocalisConfig.load()
    assert saved.brain.backend == "openai-compatible"
    assert saved.brain.model == "deepseek-chat"
    assert saved.brain.base_url == "https://api.deepseek.com/v1"
    assert saved.brain.api_key_env == "DEEPSEEK_API_KEY"

    # brain 实例被重建并重新接线到 Commander；健康缓存被清空
    st = app_module.state
    before = len(_StubBrain.instances)  # lifespan 构造真实 AppState 时可能已建桩
    r2 = client.post("/api/brain", json={"model": "deepseek-reasoner"})
    assert r2.status_code == 200
    assert len(_StubBrain.instances) == before + 1  # 恰好重建一次
    assert st.brain is _StubBrain.instances[-1]
    assert st.brain is st.commander.brain  # Commander 指向同一个新实例
    assert st._health_cache[0] == 0.0


def test_post_brain_rejects_unknown_backend(client: TestClient) -> None:
    r = client.post("/api/brain", json={"backend": "deepseek-special"})
    assert r.status_code == 422


def test_post_brain_empty_base_url_clears_it(client: TestClient) -> None:
    r = client.post("/api/brain", json={"backend": "ollama", "base_url": "  "})
    assert r.status_code == 200
    assert r.json()["base_url"] is None


def test_brain_config_defaults_to_local_qwen() -> None:
    """New installations use local Qwen and cannot silently call cloud APIs."""
    b = BrainConfig()
    assert b.backend == "local-qwen"
    assert b.local_only is True
    # No machine-specific default: must be configured explicitly.
    assert b.deployment_dir == ""
    assert b.api_key_env == "DVOICE_API_KEY"


@pytest.mark.parametrize("update", [
    {"backend": "unknown", "model": "must-not-be-saved"},
    {"backend": "openai-compatible", "model": "must-not-be-saved",
     "base_url": "https://api.example.com/v1"},
    {"base_url": "http://127.0.0.1.evil.example/v1"},
    {"base_url": "http://user:secret@127.0.0.1:8080/v1"},
    {"base_url": "http://127.0.0.1:8080/v1?redirect=remote"},
    {"base_url": "http://[invalid"},
    {"base_url": "http://127.0.0.1:invalid/v1"},
    {"api_key_env": "INVALID=NAME", "api_key": "example-secret"},
    {"api_key_env": "DVOICE_API_KEY", "api_key": "a\nINJECTED=value"},
])
def test_invalid_update_is_transactional(client: TestClient, update: dict) -> None:
    state = app_module.state
    before = asdict(state.config.brain)
    old_brain = state.brain
    state.config.save()
    response = client.post("/api/brain", json=update)
    assert response.status_code == 422
    assert asdict(state.config.brain) == before
    assert asdict(VocalisConfig.load().brain) == before
    assert state.brain is old_brain
    assert state.commander.brain is old_brain
    assert state._health_cache == (123.0, {"stale": True})


def test_local_qwen_cannot_opt_into_remote_endpoint(client: TestClient) -> None:
    response = client.post("/api/brain", json={
        "backend": "local-qwen", "local_only": False,
        "base_url": "https://api.example.com/v1",
    })
    assert response.status_code == 422
    assert app_module.state.config.brain.local_only is True


def test_post_brain_stores_api_key_in_secrets_env(client: TestClient, monkeypatch) -> None:
    """密钥写入受保护的 secrets.env 并注入 environ；绝不进 config.toml。"""
    import os

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    r = client.post(
        "/api/brain",
        json={"api_key_env": "DEEPSEEK_API_KEY", "api_key": "sk-test-secret-123"},
    )
    assert r.status_code == 200
    import vocalis.config as cfg_mod

    sec = cfg_mod.secrets_env_path()
    assert "DEEPSEEK_API_KEY=sk-test-secret-123" in sec.read_text(encoding="utf-8")
    assert os.environ["DEEPSEEK_API_KEY"] == "sk-test-secret-123"  # 热生效
    saved = VocalisConfig.load()
    # 密钥本身不出现在 config.toml（序列化里只有字段名）
    import tomli_w

    assert "sk-test-secret-123" not in tomli_w.dumps(saved.to_dict())


def test_post_brain_auto_corrects_key_typed_into_env_name(client: TestClient) -> None:
    """用户把 sk- 密钥误填进 api_key_env：自动纠正为密钥 + 标准变量名。"""
    r = client.post(
        "/api/brain",
        json={"api_key_env": "sk-61d36acdcf2047c884e15fb3ff195c70aa"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["api_key_env"] == "DEEPSEEK_API_KEY"
    import vocalis.config as cfg_mod

    assert "DEEPSEEK_API_KEY=sk-61d36acdcf2047c884e15fb3ff195c70aa" in (
        cfg_mod.secrets_env_path().read_text(encoding="utf-8")
    )
