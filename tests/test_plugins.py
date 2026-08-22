"""Entry-point 插件机制测试（Track C2，完全离线）。

通过 monkeypatch 替换 plugins 模块命名空间里的 ``entry_points``，
模拟第三方发行版注册的假 entry point：正常插件、加载即抛异常的
插件、实例化时抛异常的插件、与内置 agent 重名的插件、返回非法
对象的插件等，验证成功注册、失败隔离与重名跳过三条契约。
"""

from __future__ import annotations

import vocalis.agents.plugins as plugins_mod
from vocalis.agents.base import AgentConnector
from vocalis.agents.echo import EchoAgent
from vocalis.agents.plugins import load_entry_point_connectors
from vocalis.agents.registry import AgentRegistry
from vocalis.server.events import EventBus


# ---------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------
class FakeEntryPoint:
    """模拟 ``importlib.metadata.EntryPoint``：只有 name + load()。"""

    def __init__(self, name: str, loaded):
        self.name = name
        self._loaded = loaded

    def load(self):
        if isinstance(self._loaded, Exception):
            raise self._loaded
        return self._loaded


def _agent_class(name: str) -> type[AgentConnector]:
    """动态构造一个名为 ``name`` 的最小可用 connector 类。"""

    class PluginAgent(AgentConnector):
        description = "fake entry-point plugin agent"
        capabilities = ("test",)

        async def stream_run(self, instruction, record, **kwargs):
            yield instruction

    PluginAgent.name = name
    return PluginAgent


def _patch_entry_points(monkeypatch, eps) -> None:
    """把 plugins 模块内的 entry_points 替换为返回 ``eps`` 的假实现。"""
    monkeypatch.setattr(plugins_mod, "entry_points", lambda group=None: eps)


# ---------------------------------------------------------------------
# 成功注册
# ---------------------------------------------------------------------
def test_plugin_class_registered(monkeypatch):
    registry = AgentRegistry(EventBus())
    _patch_entry_points(monkeypatch, [FakeEntryPoint("stub", _agent_class("stub-plugin"))])

    registered = load_entry_point_connectors(registry)

    assert "stub-plugin" in registry.connectors
    assert len(registered) == 1
    assert registered[0] is registry.connectors["stub-plugin"]
    assert isinstance(registered[0], AgentConnector)


def test_plugin_factory_receives_event_bus(monkeypatch):
    """工厂签名接受参数时，第一个参数应被传入 event_bus。"""
    bus = EventBus()
    seen: list[object] = []

    def factory(event_bus):
        seen.append(event_bus)
        return _agent_class("bus-plugin")(event_bus)

    registry = AgentRegistry(bus)
    _patch_entry_points(monkeypatch, [FakeEntryPoint("bus", factory)])

    load_entry_point_connectors(registry, bus)

    assert seen == [bus]
    assert registry.connectors["bus-plugin"].bus is bus


def test_plugin_no_arg_factory_supported(monkeypatch):
    """无参工厂也能用（connector 回落到全局单例 bus）。"""
    registry = AgentRegistry(EventBus())
    _patch_entry_points(monkeypatch, [FakeEntryPoint("zero", lambda: _agent_class("zero-arg")())])

    registered = load_entry_point_connectors(registry)

    assert [c.name for c in registered] == ["zero-arg"]


def test_plugin_instance_accepted_directly(monkeypatch):
    """宽容处理：entry point 直接加载出 AgentConnector 实例。"""
    registry = AgentRegistry(EventBus())
    instance = _agent_class("ready-made")()
    _patch_entry_points(monkeypatch, [FakeEntryPoint("ready", instance)])

    registered = load_entry_point_connectors(registry)

    assert registered == [instance]


def test_returns_only_newly_registered_connectors(monkeypatch):
    registry = AgentRegistry(EventBus())
    eps = [
        FakeEntryPoint("a", _agent_class("plugin-a")),
        FakeEntryPoint("b", _agent_class("plugin-b")),
    ]
    _patch_entry_points(monkeypatch, eps)

    registered = load_entry_point_connectors(registry)

    assert sorted(c.name for c in registered) == ["plugin-a", "plugin-b"]


# ---------------------------------------------------------------------
# 失败隔离
# ---------------------------------------------------------------------
def test_failing_plugin_does_not_block_others(monkeypatch):
    """一个插件 load() 抛异常，其余插件照常注册，函数本身不抛。"""
    registry = AgentRegistry(EventBus())
    eps = [
        FakeEntryPoint("broken", RuntimeError("distribution metadata broken")),
        FakeEntryPoint("good", _agent_class("good-plugin")),
    ]
    _patch_entry_points(monkeypatch, eps)

    registered = load_entry_point_connectors(registry)

    assert [c.name for c in registered] == ["good-plugin"]
    assert "good-plugin" in registry.connectors


def test_factory_exception_is_isolated(monkeypatch):
    """工厂在实例化阶段抛异常同样被隔离。"""
    registry = AgentRegistry(EventBus())

    def broken_factory():
        raise RuntimeError("plugin bug at construction")

    eps = [
        FakeEntryPoint("boom", broken_factory),
        FakeEntryPoint("good", _agent_class("survivor")),
    ]
    _patch_entry_points(monkeypatch, eps)

    registered = load_entry_point_connectors(registry)

    assert [c.name for c in registered] == ["survivor"]


def test_non_connector_return_skipped(monkeypatch):
    """工厂返回的不是 AgentConnector：记 warning 并跳过。"""
    registry = AgentRegistry(EventBus())
    _patch_entry_points(monkeypatch, [FakeEntryPoint("bad", lambda: "not-a-connector")])

    assert load_entry_point_connectors(registry) == []
    assert registry.connectors == {}


def test_entry_points_failure_swallowed(monkeypatch):
    """entry_points() 本身抛异常（损坏的元数据）也不应炸掉宿主。"""
    registry = AgentRegistry(EventBus())

    def explode(group=None):
        raise RuntimeError("corrupted site-packages metadata")

    monkeypatch.setattr(plugins_mod, "entry_points", explode)

    assert load_entry_point_connectors(registry) == []


def test_non_callable_entry_point_skipped(monkeypatch):
    """加载结果不可调用（如模块级字符串）：跳过。"""
    registry = AgentRegistry(EventBus())
    _patch_entry_points(monkeypatch, [FakeEntryPoint("str", "vocalis.agents:echo")])

    assert load_entry_point_connectors(registry) == []


# ---------------------------------------------------------------------
# 重名跳过
# ---------------------------------------------------------------------
def test_duplicate_name_skips_builtin(monkeypatch):
    """插件与已注册的内置 agent 重名时跳过，绝不覆盖内置实现。"""
    registry = AgentRegistry(EventBus())
    builtin = EchoAgent()
    registry.register(builtin)
    _patch_entry_points(monkeypatch, [FakeEntryPoint("evil", _agent_class("echo"))])

    registered = load_entry_point_connectors(registry)

    assert registered == []
    # echo 仍是原来的内置实例，未被插件替换
    assert registry.connectors["echo"] is builtin
    assert registry.connectors["echo"].description == builtin.description


def test_duplicate_name_skips_earlier_plugin(monkeypatch):
    """两个插件同名：先注册者保留，后者跳过。"""
    registry = AgentRegistry(EventBus())
    eps = [
        FakeEntryPoint("first", _agent_class("twin")),
        FakeEntryPoint("second", _agent_class("twin")),
    ]
    _patch_entry_points(monkeypatch, eps)

    registered = load_entry_point_connectors(registry)

    assert len(registered) == 1
    assert len([c for c in registry.connectors.values() if c.name == "twin"]) == 1


# ---------------------------------------------------------------------
# 集成：build_default_registry 末尾自动加载插件
# ---------------------------------------------------------------------
def test_build_default_registry_loads_plugins(monkeypatch, tmp_path):
    """build_default_registry() 应在末尾调用插件加载（离线：假 entry point）。"""
    from vocalis.agents.registry import build_default_registry

    monkeypatch.setenv("VOCALIS_HOME", str(tmp_path))
    _patch_entry_points(monkeypatch, [FakeEntryPoint("stub", _agent_class("ep-agent"))])

    registry = build_default_registry(EventBus(), config=None)

    assert "echo" in registry.connectors  # 内置 agent 仍在
    assert "ep-agent" in registry.connectors  # 插件已注册
