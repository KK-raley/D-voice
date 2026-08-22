"""F3 事件历史回放测试：WS /ws 连接后先收历史事件（replayed 标记）再收实时事件。

完全离线：用桩 state（只含 event_bus）避免构造完整 AppState；实时事件通过
WebSocketTestSession 自身的 portal 在应用事件循环内发布，与 WS 处理器同属
一个循环，绕开跨事件循环的 asyncio.Queue 唤醒问题。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import vocalis.server.app as app_module
from vocalis.server.events import Event, EventBus, EventType


class _StubAppState:
    """WS 处理器只用到 state.event_bus——提供它即可。"""

    def __init__(self, event_bus: EventBus) -> None:
        self.event_bus = event_bus


@pytest.fixture()
def test_bus(monkeypatch: pytest.MonkeyPatch) -> EventBus:
    """每个测试一个全新的 EventBus + 桩 state（不触碰全局单例）。"""
    bus = EventBus(history_size=256)
    monkeypatch.setattr(app_module, "state", _StubAppState(bus))
    return bus


def _fill_history(bus: EventBus, count: int) -> None:
    """直接写入 history（/api/events/history 读取的也是这份数据）。"""
    for i in range(count):
        bus.history.append(
            Event(type=EventType.TASK_PROGRESS, data={"seq": i}, ts=1000.0 + i)
        )


def _publish_live(ws, bus: EventBus, **data) -> None:
    """在应用的事件循环里发布一条实时事件（与 WS 处理器同循环）。"""

    async def _publish() -> None:
        await bus.publish(EventType.SYSTEM, **data)

    ws.portal.call(_publish)


def test_ws_replays_history_in_order_then_live(test_bus: EventBus) -> None:
    """连接后：hello -> 按时间顺序的历史事件（带 replayed 标记）-> 实时事件。"""
    _fill_history(test_bus, 3)
    with TestClient(app_module.app).websocket_connect("/ws") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "system.ready"
        assert "replayed" not in hello  # hello 不是回放消息

        # 先发布实时事件也无妨：订阅队列保证它在回放之后送达
        _publish_live(ws, test_bus, message="live-after-replay")

        for i in range(3):
            msg = ws.receive_json()
            assert msg["replayed"] is True
            assert msg["type"] == "task.progress"
            assert msg["data"]["seq"] == i  # 按时间顺序（旧 -> 新）

        live = ws.receive_json()
        assert live["type"] == "system"
        assert live["data"]["message"] == "live-after-replay"
        assert "replayed" not in live  # 实时事件不带回放标记


def test_ws_replay_limit_returns_most_recent(test_bus: EventBus) -> None:
    """?replay=N 只回放最近 N 条历史。"""
    _fill_history(test_bus, 5)
    with TestClient(app_module.app).websocket_connect("/ws?replay=2") as ws:
        assert ws.receive_json()["type"] == "system.ready"
        _publish_live(ws, test_bus, message="live")

        first = ws.receive_json()
        assert first["replayed"] is True
        assert first["data"]["seq"] == 3  # 最近 2 条 = seq 3、4
        second = ws.receive_json()
        assert second["replayed"] is True
        assert second["data"]["seq"] == 4

        live = ws.receive_json()
        assert live["data"]["message"] == "live"
        assert "replayed" not in live


def test_ws_replay_zero_disables_replay(test_bus: EventBus) -> None:
    """?replay=0 关闭回放：hello 之后直接进入实时转发。"""
    _fill_history(test_bus, 3)
    with TestClient(app_module.app).websocket_connect("/ws?replay=0") as ws:
        assert ws.receive_json()["type"] == "system.ready"
        _publish_live(ws, test_bus, message="live")
        live = ws.receive_json()
        assert live["type"] == "system"
        assert live["data"]["message"] == "live"
        assert "replayed" not in live


def test_ws_empty_history_goes_straight_to_live(test_bus: EventBus) -> None:
    """历史为空（默认回放全部）时：hello 之后直接是实时事件。"""
    with TestClient(app_module.app).websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "system.ready"
        _publish_live(ws, test_bus, message="live")
        live = ws.receive_json()
        assert live["data"]["message"] == "live"
        assert "replayed" not in live
