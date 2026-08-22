"""Entry-point 第三方 agent connector 插件加载（Track C2）。

第三方包通过 ``pyproject.toml`` 声明 entry point 即可把自己的 agent
挂进 Vocalis 的默认注册表，无需改动宿主代码::

    [project.entry-points."vocalis.agents"]
    my-agent = "my_package.plugin:MyAgent"

契约（详见 docs/hooks.md）：

* entry point 加载结果必须是返回
  :class:`~vocalis.agents.base.AgentConnector` 实例的**可调用对象**
  （connector 类或工厂函数均可）；直接加载出实例也被宽容接受。
* 工厂签名若接受至少一个位置参数，则调用时传入 ``event_bus``，
  否则按无参调用（connector 自行回落到全局单例 bus）。
* 单个插件加载/实例化失败只记录 warning，不影响其余插件与宿主进程。
* 与已注册 connector 重名的插件被跳过，绝不覆盖内置实现。
"""

from __future__ import annotations

import inspect
import logging
from importlib.metadata import entry_points
from typing import Any

from vocalis.agents.base import AgentConnector
from vocalis.agents.registry import AgentRegistry
from vocalis.server.events import EventBus

logger = logging.getLogger("vocalis.agents.plugins")

#: 第三方 connector 插件声明所用的 entry point group。
ENTRY_POINT_GROUP = "vocalis.agents"


def _accepts_positional_arg(func: Any) -> bool:
    """工厂签名是否接受至少一个位置参数（用于决定是否传入 event_bus）。"""
    try:
        params = list(inspect.signature(func).parameters.values())
    except (TypeError, ValueError):  # 内建/C 扩展等拿不到签名：按无参调用
        return False
    return any(
        p.kind
        in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        )
        for p in params
    )


def _instantiate(loaded: Any, event_bus: EventBus | None) -> AgentConnector:
    """把 entry point 的加载结果变成 connector 实例（不做异常捕获）。"""
    if isinstance(loaded, AgentConnector):  # 宽容：加载结果已是实例
        return loaded
    if not callable(loaded):
        raise TypeError(
            f"entry point must be callable or an AgentConnector, got {type(loaded).__name__}"
        )
    factory = loaded
    connector = factory(event_bus) if _accepts_positional_arg(factory) else factory()
    if not isinstance(connector, AgentConnector):
        raise TypeError(
            f"entry point factory must return an AgentConnector, got {type(connector).__name__}"
        )
    return connector


def load_entry_point_connectors(
    registry: AgentRegistry, event_bus: EventBus | None = None
) -> list[AgentConnector]:
    """发现并注册 ``vocalis.agents`` entry point 插件到 ``registry``。

    ``event_bus`` 缺省时使用 registry 自身的 bus，保证插件与宿主共享
    同一条事件总线。任何单点失败（元数据损坏、加载异常、实例化异常、
    返回非法对象、与内置 agent 重名）都只记录 warning 并跳过。

    Returns:
        本次**新注册**的 connector 列表（被跳过的插件不在其中）。
    """
    event_bus = event_bus or registry.bus
    try:
        eps = entry_points(group=ENTRY_POINT_GROUP)
    except Exception:
        logger.warning("failed to enumerate %r entry points", ENTRY_POINT_GROUP, exc_info=True)
        return []

    registered: list[AgentConnector] = []
    for ep in eps:
        name = getattr(ep, "name", str(ep))
        try:
            connector = _instantiate(ep.load(), event_bus)
        except Exception:
            logger.warning("failed to load plugin %r", name, exc_info=True)
            continue
        if connector.name in registry.connectors:
            logger.warning(
                "plugin %r skipped: agent name %r already registered",
                name,
                connector.name,
            )
            continue
        registry.register(connector)
        registered.append(connector)
        logger.info("registered plugin agent %r (entry point %r)", connector.name, name)
    return registered
