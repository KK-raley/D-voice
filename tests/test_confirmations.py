"""P0-2 high-risk confirmation service + Commander guard wiring."""

from __future__ import annotations

import pytest

from vocalis.agents.registry import AgentRegistry
from vocalis.dvoice.assistant import DVoiceBrain
from vocalis.dvoice.commander import Commander
from vocalis.server.confirmations import ConfirmationService, is_risky
from vocalis.server.events import EventBus


# ----------------------------------------------------------------------
# is_risky
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "让 echo 删除 tests 目录",
        "帮我发布这个版本",
        "付款 200 元给商家",
        "push --force 到 main",
        "删除文件",
        "发送邮件给老板",
        "run rm -rf /tmp/x",
    ],
)
def test_is_risky_true(text):
    assert is_risky(text)


@pytest.mark.parametrize(
    "text",
    [
        "让 echo 跑个演示",
        "现在状态怎么样",
        "重构一下测试",
        "你好",
    ],
)
def test_is_risky_false(text):
    assert not is_risky(text)


# ----------------------------------------------------------------------
# ConfirmationService lifecycle
# ----------------------------------------------------------------------
async def test_confirmation_create_and_resolve():
    bus = EventBus()
    service = ConfirmationService(bus, ttl_s=60)
    plan = {
        "assignments": [{"agent": "echo", "instruction": "删除临时文件"}],
        "raw": "让 echo 删除临时文件",
    }
    record = await service.create(plan)
    assert service.pending()
    assert record["actions"][0]["agent"] == "echo"

    # deny: plan not returned
    resolved = await service.resolve(record["id"], approved=False)
    assert resolved is None
    assert not service.pending()

    # approve: returns the plan
    record2 = await service.create(plan)
    resolved2 = await service.resolve(record2["id"], approved=True)
    assert resolved2 == plan
    assert not service.pending()


async def test_confirmation_expiry():
    bus = EventBus()
    service = ConfirmationService(bus, ttl_s=0.01)
    record = await service.create({"assignments": [], "raw": "x"})
    import asyncio

    await asyncio.sleep(0.05)
    assert service.pending() == []
    assert await service.resolve(record["id"], approved=True) is None


async def test_confirmation_publishes_bus_events():
    bus = EventBus()
    q = bus.subscribe("confirm.*")
    service = ConfirmationService(bus)
    record = await service.create({"assignments": [], "raw": "r"})
    await service.resolve(record["id"], approved=True)
    types = [await q.get() for _ in range(2)]
    assert [t.type.value for t in types] == ["confirm.requested", "confirm.resolved"]
    assert types[1].data["approved"] is True


# ----------------------------------------------------------------------
# Commander integration: risky dispatch suspended
# ----------------------------------------------------------------------
def _commander(confirmations=None, bus=None):
    bus = bus or EventBus()
    registry = AgentRegistry(bus)
    brain = DVoiceBrain(config=None, registry=registry)
    brain.brain_cfg.enabled = False
    return Commander(registry, brain, bus, confirmations)


async def test_commander_suspends_risky_dispatch():
    service = ConfirmationService(EventBus())
    cmd = _commander(service)
    # register echo connector
    registry = cmd.registry
    from vocalis.agents.echo import EchoAgent

    registry.register(EchoAgent(registry.bus))

    result = await cmd.execute("让 echo 删除所有日志")
    assert result["kind"] == "confirmation"
    cid = result["confirmation"]["id"]
    assert service.pending()

    # approval path executes the plan
    executed = await cmd.execute_plan(await service.resolve(cid, approved=True))
    assert executed["kind"] == "task"
    assert executed["task"]["status"] == "completed"


async def test_commander_safe_dispatch_unaffected():
    cmd = _commander(ConfirmationService(EventBus()))
    from vocalis.agents.echo import EchoAgent

    cmd.registry.register(EchoAgent(cmd.registry.bus))
    result = await cmd.execute("让 echo 跑个演示")
    assert result["kind"] == "task"


async def test_commander_receipt_event_published():
    bus = EventBus()
    q = bus.subscribe("command.receipt")
    cmd = _commander(ConfirmationService(bus), bus=bus)
    result = await cmd.execute("今天天气怎么样")
    assert result["kind"] == "answer"
    event = await q.get()
    assert event.data["kind"] == "answer"
    assert event.data["transcript"] == "今天天气怎么样"
    assert event.data["local_llm"] is False
    assert "voiceprint" in event.data
